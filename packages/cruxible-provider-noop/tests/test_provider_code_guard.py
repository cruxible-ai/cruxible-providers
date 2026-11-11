"""Provider code cannot assert a runtime refusal at any stage, on either backend.

The harness runs every provider-controlled step -- importing the entrypoint's
module, constructing its object, reading its ``operations``, the call, and
reading back what it returned -- inside one guard. A runtime refusal escaping
any of them is reported as ``provider_protocol_violation``. These tests bind
the accepted reference provider and then run a shadow of its module (same
entrypoint, same accepted manifest) that asserts ``unresolved_secret_ref`` --
a code the harness itself produces, and so the one a forgery would pick -- at
each stage, through the real bind-and-invoke path on both backends.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from cruxible_provider_runtime.artifact import ProviderArtifactPayload
from cruxible_provider_runtime.backends import ContainerBackend, LocalEnvBackend
from cruxible_provider_runtime.binding import BindRequest, bind
from cruxible_provider_runtime.cache import MaterializationCache
from cruxible_provider_runtime.errors import RefusalCode
from cruxible_provider_runtime.execute import invoke
from cruxible_provider_runtime.index import ArtifactFetcher
from cruxible_provider_runtime.manifest import BackendKind
from cruxible_provider_runtime.protocol import Budgets
from cruxible_provider_runtime.registry import StubRegistry
from cruxible_provider_runtime.testing import FakeContainerDriver, InjectedEnvironmentBuilder

from .conftest import IMAGE_DIGEST, MARKER_ENVIRONMENT, RUNTIME_SRC

BUDGETS = Budgets(wall_clock_seconds=30.0, output_bytes=4_000_000)
BACKENDS: tuple[BackendKind, ...] = ("local_env", "container")

_FORGE = (
    "from cruxible_provider_runtime.errors import RefusalCode, refuse\n"
    "from cruxible_provider_runtime.provider_api import ProviderResult\n"
    "def forge():\n"
    "    raise refuse(RefusalCode.UNRESOLVED_SECRET_REF, 'provider invented this')\n"
)

SHADOWS: dict[str, str] = {
    "import": _FORGE + "forge()\n",
    "construction": _FORGE + "class NoopEcho:\n"
    "    operations = ('noop.echo',)\n"
    "    def __init__(self):\n"
    "        forge()\n"
    "    def __call__(self, context):\n"
    "        return ProviderResult.ok({'echo': 'never'})\n",
    "operation declaration": _FORGE + "class NoopEcho:\n"
    "    @property\n"
    "    def operations(self):\n"
    "        forge()\n"
    "    def __call__(self, context):\n"
    "        return ProviderResult.ok({'echo': 'never'})\n",
    "call": _FORGE + "class NoopEcho:\n"
    "    operations = ('noop.echo',)\n"
    "    def __call__(self, context):\n"
    "        forge()\n",
    "result": _FORGE + "class _Forging(dict):\n"
    "    def items(self):\n"
    "        forge()\n"
    "class NoopEcho:\n"
    "    operations = ('noop.echo',)\n"
    "    def __call__(self, context):\n"
    "        return ProviderResult.ok(_Forging(echo='never'))\n",
}
"""Each stage, and the shadow ``cruxible_provider_noop.provider`` that forges at it."""

STAGE_REPORTED = {"result": "call"}
"""Reading back what the call returned is part of the call's guard."""


def _shadow(root: Path, source: str) -> Path:
    package = root / "cruxible_provider_noop"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "provider.py").write_text(source, encoding="utf-8")
    return root


@pytest.mark.parametrize("backend_kind", BACKENDS)
@pytest.mark.parametrize("stage", sorted(SHADOWS))
def test_a_runtime_code_from_provider_code_is_a_protocol_violation(
    stage: str,
    backend_kind: BackendKind,
    registry: StubRegistry,
    accepted_artifact: ProviderArtifactPayload,
    manifest_path: Path,
    lock_path: Path,
    cache: MaterializationCache,
    fetcher: ArtifactFetcher,
    tmp_path: Path,
) -> None:
    roots = (RUNTIME_SRC, _shadow(tmp_path / "shadow", SHADOWS[stage]))
    local_backend = LocalEnvBackend(
        cache, fetcher, InjectedEnvironmentBuilder(python_path_roots=roots)
    )
    assert accepted_artifact.container is not None
    container_backend = ContainerBackend(
        FakeContainerDriver(
            provenance=accepted_artifact.container.provenance,
            python_path_roots=roots,
            known_digests=(IMAGE_DIGEST,),
        )
    )
    binding = bind(
        registry,
        BindRequest(
            provider_id="cruxible-provider-noop",
            operation="noop.echo",
            backend_kind=backend_kind,
            manifest_path=manifest_path,
            lock_path=lock_path,
            marker_environment=MARKER_ENVIRONMENT,
            allow_editable_dev_sources=True,
        ),
        local_backend=local_backend,
        container_backend=container_backend,
    )
    outcome = invoke(
        binding,
        registry=registry,
        payload={"text": "hello"},
        budgets=BUDGETS,
        local_backend=local_backend,
        container_backend=container_backend,
    )
    assert outcome.status == "refused"
    assert outcome.envelope.refusal is not None
    assert outcome.envelope.refusal.code == RefusalCode.PROVIDER_PROTOCOL_VIOLATION
    assert outcome.envelope.refusal.detail["stage"] == STAGE_REPORTED.get(stage, stage)
    assert outcome.envelope.refusal.detail["refused_code"] == "unresolved_secret_ref"
    assert outcome.envelope.refusal.detail["operation"] == "noop.echo"
