"""Secret-delivery conformance: the credential must not survive anywhere.

The provider under test is running in ``leak`` mode, which deliberately writes
the credential into its output, its trace events, and its stderr. Redaction
happens in the harness on the way out, so a provider that *tries* to leak still
cannot — which is the only version of this property worth testing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from cruxible_provider_runtime.backends import ContainerBackend, LocalEnvBackend
from cruxible_provider_runtime.binding import BindRequest, Binding, bind
from cruxible_provider_runtime.cache import MaterializationCache
from cruxible_provider_runtime.execute import invoke
from cruxible_provider_runtime.manifest import BackendKind
from cruxible_provider_runtime.protocol import Budgets
from cruxible_provider_runtime.registry import StubRegistry
from cruxible_provider_runtime.secrets import REDACTION_PLACEHOLDER

from cruxible_provider_noop.provider import CREDENTIAL_REF

from .conftest import MARKER_ENVIRONMENT

DUMMY_CREDENTIAL = "dummy-credential-c0ffee-do-not-use"
BUDGETS = Budgets(wall_clock_seconds=30.0, output_bytes=4_000_000)
BACKENDS: tuple[BackendKind, ...] = ("local_env", "container")


@pytest.fixture()
def binding(
    request: pytest.FixtureRequest,
    registry: StubRegistry,
    manifest_path: Path,
    lock_path: Path,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> Binding:
    backend_kind: BackendKind = getattr(request, "param", "local_env")
    return bind(
        registry,
        BindRequest(
            provider_id="cruxible-provider-noop",
            interface_id="noop.echo",
            backend_kind=backend_kind,
            manifest_path=manifest_path,
            lock_path=lock_path,
            marker_environment=MARKER_ENVIRONMENT,
        ),
        local_backend=local_backend,
        container_backend=container_backend,
    )


@pytest.mark.parametrize("binding", BACKENDS, indirect=True)
def test_a_provider_that_tries_to_leak_still_cannot(
    binding: Binding,
    registry: StubRegistry,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> None:
    outcome = invoke(
        binding,
        registry=registry,
        payload={"text": "hello", "mode": "leak"},
        budgets=BUDGETS,
        secrets={CREDENTIAL_REF: DUMMY_CREDENTIAL},
        local_backend=local_backend,
        container_backend=container_backend,
    )
    envelope = json.loads(outcome.envelope.model_dump_json())
    assert DUMMY_CREDENTIAL not in json.dumps(envelope)
    assert DUMMY_CREDENTIAL not in outcome.stderr
    assert envelope["output"]["echo"] == REDACTION_PLACEHOLDER
    assert envelope["trace"]["events"][0]["credential"] == REDACTION_PLACEHOLDER


def test_the_credential_never_enters_the_serialised_run_context(
    binding: Binding,
    registry: StubRegistry,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Capture the exact bytes handed to the child and assert the credential is absent."""

    captured: list[bytes] = []
    original = LocalEnvBackend.invoke

    def spy(self: LocalEnvBackend, env_path: Path, **kwargs: object) -> object:
        captured.append(bytes(kwargs["stdin_bytes"]))  # type: ignore[arg-type]
        return original(self, env_path, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(LocalEnvBackend, "invoke", spy)
    invoke(
        binding,
        registry=registry,
        payload={"text": "hello", "mode": "credential"},
        budgets=BUDGETS,
        secrets={CREDENTIAL_REF: DUMMY_CREDENTIAL},
        local_backend=local_backend,
        container_backend=container_backend,
    )
    assert captured, "the local backend was not invoked"
    document = json.loads(captured[0])
    assert DUMMY_CREDENTIAL not in captured[0].decode()
    assert document["secret_channel"]["refs"] == [{"ref": CREDENTIAL_REF, "purpose": ""}]
    assert document["secret_channel"]["kind"] == "fd"


def test_the_credential_never_enters_argv_or_the_child_environment(
    binding: Binding,
    registry: StubRegistry,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cruxible_provider_runtime import backends as backends_module

    seen: list[tuple[list[str], dict[str, str]]] = []
    original = backends_module.run_with_budget

    def spy(argv: list[str], **kwargs: object) -> object:
        seen.append((list(argv), dict(kwargs["env"])))  # type: ignore[arg-type]
        return original(argv, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(backends_module, "run_with_budget", spy)
    invoke(
        binding,
        registry=registry,
        payload={"text": "hello", "mode": "credential"},
        budgets=BUDGETS,
        secrets={CREDENTIAL_REF: DUMMY_CREDENTIAL},
        local_backend=local_backend,
        container_backend=container_backend,
    )
    assert seen
    argv, env = seen[0]
    assert DUMMY_CREDENTIAL not in " ".join(argv)
    assert all(DUMMY_CREDENTIAL not in value for value in env.values())
    assert all(DUMMY_CREDENTIAL not in name for name in env)


def test_the_credential_never_reaches_the_cache_directory(
    binding: Binding,
    registry: StubRegistry,
    cache: MaterializationCache,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> None:
    invoke(
        binding,
        registry=registry,
        payload={"text": "hello", "mode": "credential"},
        budgets=BUDGETS,
        secrets={CREDENTIAL_REF: DUMMY_CREDENTIAL},
        local_backend=local_backend,
        container_backend=container_backend,
    )
    for path in cache.root.rglob("*"):
        if path.is_file():
            assert DUMMY_CREDENTIAL not in path.read_text(encoding="utf-8", errors="replace")


def test_the_credential_never_reaches_a_digest_preimage(
    binding: Binding, accepted_artifact: object
) -> None:
    rendered = json.dumps(
        {
            "snapshot": binding.snapshot(),
            "artifact": json.loads(accepted_artifact.model_dump_json()),  # type: ignore[attr-defined]
        }
    )
    assert DUMMY_CREDENTIAL not in rendered
