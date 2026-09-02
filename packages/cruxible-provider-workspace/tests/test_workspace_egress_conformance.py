"""The no-network egress-conformance lane for the built-in.

Every test here runs with outbound sockets blocked in **both** processes -- the
executor's and the adapter child's -- and asserts that declared equals observed
equals empty. For a pure adapter that is the whole egress story: there is
nothing to declare and nothing must be observed.

What this lane claims, kept aligned with the honest-boundary law: recording
conformance, not containment. Containment exists in the cloud backend's
default-deny policy alone.
"""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path

import pytest
from cruxible_provider_runtime.backends import ContainerBackend, LocalEnvBackend
from cruxible_provider_runtime.binding import Binding, BindRequest, bind
from cruxible_provider_runtime.egress import no_network
from cruxible_provider_runtime.execute import invoke, observed_vs_declared
from cruxible_provider_runtime.manifest import BackendKind, load_manifest
from cruxible_provider_runtime.protocol import Budgets
from cruxible_provider_runtime.registry import StubRegistry
from cruxible_provider_runtime.testing import FakeContainerDriver, InjectedEnvironmentBuilder

from .conftest import INTERFACE_ID, MARKER_ENVIRONMENT, PROVIDER_ID

pytestmark = pytest.mark.egress_conformance

BUDGETS = Budgets(wall_clock_seconds=30.0, output_bytes=8_000_000)
BACKENDS: tuple[BackendKind, ...] = ("local_env", "container")
DATA = b"reach,nitrate_mg_l\nUpper,4.1\n"
PAYLOAD = {
    "logical_source": "data/reach-readings.csv",
    "commitment_digest": "sha256:" + "c0" * 32,
    "content_encoding": "base64",
    "bytes": base64.b64encode(DATA).decode("ascii"),
    "byte_length": len(DATA),
    "bytes_digest": "sha256:" + hashlib.sha256(DATA).hexdigest(),
}


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
    container_driver.python_path_roots = (
        egress_guard_root,
        *container_driver.python_path_roots,
    )


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
            provider_id=PROVIDER_ID,
            interface_id=INTERFACE_ID,
            backend_kind=backend_kind,
            manifest_path=manifest_path,
            lock_path=lock_path,
            marker_environment=MARKER_ENVIRONMENT,
            allow_editable_dev_sources=True,
        ),
        local_backend=local_backend,
        container_backend=container_backend,
    )


def test_the_manifest_declares_zero_endpoints(manifest_path: Path) -> None:
    manifest = load_manifest(manifest_path)
    assert manifest.implementation(INTERFACE_ID).declared_endpoints == ()


@pytest.mark.parametrize("binding", BACKENDS, indirect=True)
def test_declared_equals_observed_equals_empty(
    binding: Binding,
    registry: StubRegistry,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> None:
    outcome = invoke(
        binding,
        registry=registry,
        payload=PAYLOAD,
        budgets=BUDGETS,
        local_backend=local_backend,
        container_backend=container_backend,
    )
    assert outcome.status == "ok"
    comparison = observed_vs_declared(binding, outcome.envelope)
    assert comparison.declared == comparison.observed == ()
    assert comparison.dynamic_forms == ()
    assert comparison.conformant


def test_the_guard_is_on_the_child_path(egress_guard_root: Path) -> None:
    """The lane's premise, checked: the child interpreter carries the guard."""

    assert (egress_guard_root / "sitecustomize.py").is_file()
