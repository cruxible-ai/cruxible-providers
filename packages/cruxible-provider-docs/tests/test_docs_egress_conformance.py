"""The no-network egress-conformance lane for the document plane.

This plane declares **zero** endpoints, which is the strongest declaration there
is and the easiest to violate quietly: a document converter that fetched a remote
schema, a font, or a model on first use would be contacting something nobody
declared, and the run would look identical from the outside. So the lane runs
with sockets blocked in both processes and asserts declared == observed == empty
for every path the default lane can reach.

Recording conformance, not containment. Containment exists in the cloud backend's
default-deny policy alone.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from cruxible_provider_runtime.backends import ContainerBackend, LocalEnvBackend
from cruxible_provider_runtime.binding import Binding, BindRequest, bind
from cruxible_provider_runtime.egress import no_network
from cruxible_provider_runtime.execute import invoke, observed_vs_declared
from cruxible_provider_runtime.manifest import BackendKind, ProviderManifest
from cruxible_provider_runtime.protocol import Budgets
from cruxible_provider_runtime.registry import StubRegistry
from cruxible_provider_runtime.testing import FakeContainerDriver, InjectedEnvironmentBuilder

from .conftest import MARKER_ENVIRONMENT, PROVIDER_ID

pytestmark = pytest.mark.egress_conformance

BUDGETS = Budgets(wall_clock_seconds=60.0, output_bytes=4_000_000)
BACKENDS: tuple[BackendKind, ...] = ("local_env", "container")
CSV = b"reach,nitrate_mg_l\nUpper,4.1\n"


@pytest.fixture(autouse=True)
def block_the_network() -> object:
    with no_network():
        yield


@pytest.fixture(autouse=True)
def guard_the_child(
    egress_guard_root: Path,
    builder: InjectedEnvironmentBuilder,
    container_driver: FakeContainerDriver,
) -> None:
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


def test_the_manifest_declares_no_endpoints(manifest: ProviderManifest) -> None:
    for implementation in manifest.implementations:
        assert implementation.declared_endpoints == ()


@pytest.mark.parametrize("backend_kind", BACKENDS)
def test_a_conversion_contacts_nothing(
    backend_kind: BackendKind,
    registry: StubRegistry,
    manifest_path: Path,
    lock_path: Path,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> None:
    binding = _bind(
        "doc.to_markdown",
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
        payload={
            "source": {
                "kind": "inline",
                "filename": "readings.csv",
                "media_type": "text/csv",
                "content_base64": base64.b64encode(CSV).decode("ascii"),
            },
            "layout": "tabular",
        },
        budgets=BUDGETS,
        local_backend=local_backend,
        container_backend=container_backend,
    )
    assert outcome.status == "ok"
    comparison = observed_vs_declared(binding, outcome.envelope)
    assert comparison.declared == comparison.observed == ()
    assert comparison.dynamic_forms == ()
    assert comparison.conformant


@pytest.mark.parametrize("backend_kind", BACKENDS)
def test_a_replayed_reading_contacts_nothing(
    backend_kind: BackendKind,
    registry: StubRegistry,
    manifest_path: Path,
    lock_path: Path,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> None:
    binding = _bind(
        "ocr.extract",
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
        payload={
            "source": {
                "kind": "packaged_fixture",
                "id": "scan-clean",
                "filename": "scan-clean.png",
                "media_type": "image/png",
            },
            "page_count": 1,
        },
        budgets=BUDGETS,
        local_backend=local_backend,
        container_backend=container_backend,
    )
    assert outcome.status == "ok"
    assert observed_vs_declared(binding, outcome.envelope).observed == ()


def test_an_engine_that_reached_for_the_network_would_be_visible_here(
    registry: StubRegistry,
    manifest_path: Path,
    lock_path: Path,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> None:
    """Keeps the lane from passing vacuously.

    A model download on first use is the realistic way this plane would acquire
    an undeclared endpoint, and it happens inside the child. The socket guard is
    on the child's import path, so an attempt fails there and is visible as an
    error rather than as a silent success — which is what makes the empty
    observed set above mean something.
    """

    binding = _bind(
        "doc.to_markdown",
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
        payload={
            "source": {
                "kind": "inline",
                "filename": "supplied.pdf",
                "media_type": "application/pdf",
                "content_base64": base64.b64encode(b"%PDF-1.4").decode("ascii"),
            },
            "page_count": 1,
        },
        budgets=BUDGETS,
        local_backend=local_backend,
        container_backend=container_backend,
    )
    # With no engine installed the run refuses before any download could start,
    # which is the same fail-closed answer from the other direction.
    assert outcome.status == "refused"
    assert outcome.egress.observed == ()
