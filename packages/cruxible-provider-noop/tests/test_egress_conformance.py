"""The no-network egress-conformance lane.

Every test here runs with outbound sockets blocked and asserts that the
adapter's **declared** endpoints equal its **observed** ones. This is the lane
CI runs for every plane package: the point is not that the reference provider
happens to contact nothing, it is that a package cannot claim zero endpoints and
then quietly open one.
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

from .conftest import MARKER_ENVIRONMENT

pytestmark = pytest.mark.egress_conformance

BUDGETS = Budgets(wall_clock_seconds=30.0, output_bytes=4_000_000)
BACKENDS: tuple[BackendKind, ...] = ("local_env", "container")


@pytest.fixture(autouse=True)
def block_the_network() -> object:
    with no_network():
        yield


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
