"""The no-network egress-conformance lane, for all seven implementations.

Every test here runs with outbound sockets blocked in **both** processes — the
executor's and the provider child's — and asserts that each adapter's
**declared** endpoints equal its **observed** ones. For this plane both sides are
empty, and that is the whole claim: these are pure computations, and a package
cannot declare zero endpoints and then quietly open one.

The lane matters more here than it looks. This plane pulls in DuckDB, and a
database engine is exactly the kind of dependency that could grow a telemetry
ping or a remote-extension fetch in a minor release without anyone reading the
changelog. A lane that runs the real engines with sockets blocked is what turns
that from a thing to worry about into a thing that fails the build.

What this lane does and does not claim, kept aligned with the honest-boundary
law: it tests **recording conformance** (declared equals observed). It does not
demonstrate **containment**, which exists only in the cloud backend's
default-deny network policy. A local provider runs with the operator's
privileges and can go around any in-interpreter guard.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from cruxible_provider_quant.interfaces import INTERFACE_IDS
from cruxible_provider_runtime.backends import ContainerBackend, LocalEnvBackend
from cruxible_provider_runtime.egress import no_network
from cruxible_provider_runtime.execute import invoke, observed_vs_declared
from cruxible_provider_runtime.manifest import ProviderManifest
from cruxible_provider_runtime.registry import StubRegistry
from cruxible_provider_runtime.testing import FakeContainerDriver, InjectedEnvironmentBuilder

from .conftest import BUDGETS, bind_interface
from .fixtures import FIXTURES
from .test_full_loop import REPRESENTATIVE

pytestmark = pytest.mark.egress_conformance

CASES = [
    pytest.param(fixture, id=fixture.fixture_id)
    for fixture in FIXTURES
    if fixture.fixture_id in REPRESENTATIVE.values()
]


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


def test_every_implementation_declares_zero_endpoints(manifest: ProviderManifest) -> None:
    assert {impl.interface_id for impl in manifest.implementations} == set(INTERFACE_IDS)
    for implementation in manifest.implementations:
        assert implementation.declared_endpoints == ()


@pytest.mark.parametrize("fixture", CASES)
def test_declared_equals_observed_with_sockets_blocked(
    fixture: object,
    registry: StubRegistry,
    manifest_path: Path,
    lock_path: Path,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> None:
    binding = bind_interface(
        fixture.interface_id,  # type: ignore[attr-defined]
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
        payload=fixture.payload,  # type: ignore[attr-defined]
        budgets=BUDGETS,
        local_backend=local_backend,
        container_backend=container_backend,
    )
    assert outcome.status == "ok", outcome.envelope.model_dump()
    comparison = observed_vs_declared(binding, outcome.envelope)
    assert comparison.declared == comparison.observed == ()
    assert comparison.conformant


def test_the_engines_run_to_completion_with_no_socket_available(
    registry: StubRegistry,
    manifest_path: Path,
    lock_path: Path,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> None:
    """The DuckDB case, run rather than assumed.

    splink stands up an in-process DuckDB. If that ever grew a network step, this
    would stop passing — which is the point of running the real engine in the
    lane instead of asserting an empty list against a stub.
    """

    fixture = next(f for f in FIXTURES if f.fixture_id == "quant-linkage-weak-blocking")
    binding = bind_interface(
        "match.record",
        "container",
        registry,
        manifest_path,
        lock_path,
        local_backend,
        container_backend,
    )
    outcome = invoke(
        binding,
        registry=registry,
        payload=fixture.payload,
        budgets=BUDGETS,
        local_backend=local_backend,
        container_backend=container_backend,
    )
    assert outcome.status == "ok", outcome.envelope.model_dump()
    assert outcome.envelope.trace.endpoints_contacted == []
    assert outcome.envelope.output is not None
    assert outcome.envelope.output["pairs"]
