"""The no-network egress-conformance lane.

Every test here runs with outbound sockets blocked — in **both** processes, the
executor's and the provider child's — and asserts that the adapter's **declared**
endpoints equal its **observed** ones. This is the lane CI runs for every plane
package: the point is not that the reference provider happens to contact
nothing, it is that a package cannot claim zero endpoints and then quietly open
one.

The two-process part is the correction that matters. The in-process guard only
ever covered the pytest interpreter, while provider code runs in a child the
patch never reached — so the lane was asserting a property about the wrong
process. The child now gets the guard through a ``sitecustomize`` on its path.

What this lane does and does not claim, kept aligned with the honest-boundary
law: it tests **recording conformance** (declared equals observed). It does not
demonstrate **containment**, which exists only in the cloud backend's
default-deny network policy. A local provider runs with the operator's
privileges and can go around any in-interpreter guard; the guard's job is to
make an unrecorded socket fail loudly during a conformance run, not to stop a
determined one.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from cruxible_provider_runtime.backends import ContainerBackend, LocalEnvBackend
from cruxible_provider_runtime.binding import Binding, BindRequest, bind
from cruxible_provider_runtime.egress import compare_egress, no_network
from cruxible_provider_runtime.execute import invoke, observed_vs_declared
from cruxible_provider_runtime.manifest import BackendKind, load_manifest
from cruxible_provider_runtime.protocol import Budgets
from cruxible_provider_runtime.registry import StubRegistry
from cruxible_provider_runtime.testing import FakeContainerDriver, InjectedEnvironmentBuilder

from .conftest import MARKER_ENVIRONMENT

pytestmark = pytest.mark.egress_conformance

BUDGETS = Budgets(wall_clock_seconds=30.0, output_bytes=4_000_000)
BACKENDS: tuple[BackendKind, ...] = ("local_env", "container")


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
            provider_id="cruxible-provider-noop",
            interface_id="noop.echo",
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
    assert manifest.implementation("noop.echo").declared_endpoints == ()


@pytest.mark.parametrize("binding", BACKENDS, indirect=True)
def test_declared_equals_observed(
    binding: Binding,
    registry: StubRegistry,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> None:
    outcome = invoke(
        binding,
        registry=registry,
        payload={"text": "hello", "mode": "echo"},
        budgets=BUDGETS,
        local_backend=local_backend,
        container_backend=container_backend,
    )
    comparison = observed_vs_declared(binding, outcome.envelope)
    assert comparison.declared == comparison.observed == ()
    assert comparison.conformant


def test_the_comparison_helper_reports_both_directions() -> None:
    comparison = compare_egress(["https://declared.example"], ["https://observed.example"])
    assert comparison.undeclared == ("https://observed.example",)
    assert comparison.unused == ("https://declared.example",)
    assert not comparison.conformant


@pytest.mark.parametrize("binding", BACKENDS, indirect=True)
def test_the_guard_actually_reaches_the_provider_child(
    binding: Binding,
    registry: StubRegistry,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> None:
    """Prove the child is guarded, rather than assuming it inherited the patch.

    The provider opens a socket from inside its own process. Without the
    injected ``sitecustomize`` the attempt would succeed (or fail on DNS, which
    is not the same thing), and the lane's premise would be false.
    """

    outcome = invoke(
        binding,
        registry=registry,
        payload={"text": "hello", "mode": "connect"},
        budgets=BUDGETS,
        local_backend=local_backend,
        container_backend=container_backend,
    )
    assert outcome.status == "error"
    assert outcome.envelope.error is not None
    assert "egress-conformance lane" in outcome.envelope.error.message


def test_the_guard_module_is_importable_on_its_own(egress_guard_root: Path) -> None:
    assert (egress_guard_root / "sitecustomize.py").is_file()
    assert "socket" in (egress_guard_root / "sitecustomize.py").read_text(encoding="utf-8")
