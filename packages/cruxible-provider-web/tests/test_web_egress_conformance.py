"""The no-network egress-conformance lane for the web plane.

Every test here runs with outbound sockets blocked in **both** processes — the
executor's and the provider child's — and asserts that what the adapter declared
and what it was observed to request line up.

The plane is the first one where the two declaration forms meet:

* ``search.web`` declares a concrete origin, so the lane asserts observed ⊆
  declared, and that an origin outside the declaration refuses;
* ``web.fetch`` declares the experimental ``dynamic:target-from-run-input``
  form, so there is no list to be inside of and the lane asserts the other
  half — that the request was **recorded**, and that the receipt says the
  declaration was dynamic rather than leaving an empty ``undeclared`` set to be
  misread as an allowlist that held.

What this lane tests is recording conformance. It does not demonstrate
containment: containment exists in the cloud backend's default-deny policy
alone, and a local provider runs with the operator's privileges.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from cruxible_provider_runtime.backends import ContainerBackend, LocalEnvBackend
from cruxible_provider_runtime.binding import Binding, BindRequest, bind
from cruxible_provider_runtime.egress import DYNAMIC_TARGET_FROM_RUN_INPUT, no_network
from cruxible_provider_runtime.errors import RefusalCode
from cruxible_provider_runtime.execute import invoke, observed_vs_declared
from cruxible_provider_runtime.manifest import BackendKind, ProviderManifest
from cruxible_provider_runtime.protocol import Budgets
from cruxible_provider_runtime.registry import StubRegistry
from cruxible_provider_runtime.testing import FakeContainerDriver, InjectedEnvironmentBuilder

from .conftest import MARKER_ENVIRONMENT, PROVIDER_ID

pytestmark = pytest.mark.egress_conformance

BUDGETS = Budgets(wall_clock_seconds=60.0, output_bytes=4_000_000)
BACKENDS: tuple[BackendKind, ...] = ("local_env", "container")
ARTICLE_URL = "https://fixture.invalid/articles/tide-gauge-recalibration"


@pytest.fixture(autouse=True)
def block_the_network() -> object:
    """Guard the executor process. The child gets its own, below."""

    with no_network():
        yield


@pytest.fixture(autouse=True)
def guard_the_child(
    egress_guard_root: Path,
    builder: InjectedEnvironmentBuilder,
    container_driver: FakeContainerDriver,
) -> None:
    """Put the socket guard on the provider child's import path, in both backends."""

    builder.python_path_roots = (egress_guard_root, *builder.python_path_roots)
    container_driver.python_path_roots = (egress_guard_root, *container_driver.python_path_roots)


def _bind(
    interface_id: str,
    backend_kind: BackendKind,
    registry: StubRegistry,
    manifest_path: Path,
    lock_path: Path,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> Binding:
    return bind(
        registry,
        BindRequest(
            provider_id=PROVIDER_ID,
            interface_id=interface_id,
            backend_kind=backend_kind,
            manifest_path=manifest_path,
            lock_path=lock_path,
            marker_environment=MARKER_ENVIRONMENT,
            allow_editable_dev_sources=True,
        ),
        local_backend=local_backend,
        container_backend=container_backend,
    )


def test_the_manifest_declares_what_each_implementation_actually_needs(
    manifest: ProviderManifest,
) -> None:
    assert manifest.implementation("web.fetch").declared_endpoints == (
        DYNAMIC_TARGET_FROM_RUN_INPUT,
    )
    assert manifest.implementation("search.web").declared_endpoints == ("https://fixture.invalid",)


@pytest.mark.parametrize("backend_kind", BACKENDS)
def test_search_observed_is_inside_declared(
    backend_kind: BackendKind,
    registry: StubRegistry,
    manifest_path: Path,
    lock_path: Path,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> None:
    binding = _bind(
        "search.web",
        backend_kind,
        registry,
        manifest_path,
        lock_path,
        local_backend,
        container_backend,
    )
    outcome = invoke(
        binding,
        registry=registry,
        payload={"query": "tide gauge recalibration"},
        coordinates={"instance_url": "https://fixture.invalid"},
        budgets=BUDGETS,
        local_backend=local_backend,
        container_backend=container_backend,
    )
    comparison = observed_vs_declared(binding, outcome.envelope)
    assert comparison.observed == ("https://fixture.invalid",)
    assert comparison.undeclared == ()
    assert comparison.dynamic_forms == ()
    assert comparison.conformant


@pytest.mark.parametrize("backend_kind", BACKENDS)
def test_fetch_records_its_target_under_a_dynamic_declaration(
    backend_kind: BackendKind,
    registry: StubRegistry,
    manifest_path: Path,
    lock_path: Path,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> None:
    binding = _bind(
        "web.fetch",
        backend_kind,
        registry,
        manifest_path,
        lock_path,
        local_backend,
        container_backend,
    )
    outcome = invoke(
        binding,
        registry=registry,
        payload={"url": ARTICLE_URL},
        budgets=BUDGETS,
        local_backend=local_backend,
        container_backend=container_backend,
    )
    comparison = observed_vs_declared(binding, outcome.envelope)
    assert comparison.observed == ("https://fixture.invalid",)
    assert comparison.dynamic_forms == (DYNAMIC_TARGET_FROM_RUN_INPUT,)
    assert comparison.conformant


def test_the_lane_would_notice_an_unrecorded_socket(
    registry: StubRegistry,
    manifest_path: Path,
    lock_path: Path,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> None:
    """Keeps the lane from passing vacuously.

    The adapter is asked for a host with no packaged recording behind it, so the
    real transport is selected and a socket is attempted from inside the child.
    The guard on the child's import path stops it, and the message proves the
    guard is in the process that runs provider code rather than in the one
    running the test.
    """

    binding = _bind(
        "web.fetch",
        "local_env",
        registry,
        manifest_path,
        lock_path,
        local_backend,
        container_backend,
    )
    outcome = invoke(
        binding,
        registry=registry,
        payload={"url": "https://not-a-fixture.invalid/page"},
        budgets=BUDGETS,
        local_backend=local_backend,
        container_backend=container_backend,
    )
    assert outcome.status == "error"
    assert outcome.envelope.error is not None
    assert "egress-conformance lane" in outcome.envelope.error.message
    # And the attempt was recorded, which is the property the lane is about.
    assert outcome.egress.observed == ("https://not-a-fixture.invalid",)


def test_an_instance_outside_the_declaration_refuses_before_the_request(
    registry: StubRegistry,
    manifest_path: Path,
    lock_path: Path,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> None:
    binding = _bind(
        "search.web",
        "local_env",
        registry,
        manifest_path,
        lock_path,
        local_backend,
        container_backend,
    )
    outcome = invoke(
        binding,
        registry=registry,
        payload={"query": "tide gauge"},
        coordinates={"instance_url": "https://elsewhere.example"},
        budgets=BUDGETS,
        local_backend=local_backend,
        container_backend=container_backend,
    )
    assert outcome.status == "refused"
    assert outcome.envelope.refusal is not None
    assert outcome.envelope.refusal.code is RefusalCode.UNDECLARED_EGRESS
    assert outcome.egress.observed == ()
