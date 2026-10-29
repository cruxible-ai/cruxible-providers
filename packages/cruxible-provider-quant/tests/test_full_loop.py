"""The bind-and-invoke loop for all seven implementations, on both backend kinds.

One representative fixture per operation, run end to end through a real child
process: the provider module is imported in the child, the engine runs for real,
and the typed result comes back across a pipe. The other fixtures and the
numerical assertions run in process — see ``run_in_process`` in ``conftest`` for
why — but every implementation is proved to survive the protocol here.

The cheapest fixture per operation is chosen deliberately. Fourteen child
processes each importing a numerical engine is the expensive part of this
suite, and repeating it for every fixture would buy coverage of the runtime, not
of this package.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from cruxible_provider_quant import OPERATIONS
from cruxible_provider_runtime.backends import ContainerBackend, LocalEnvBackend
from cruxible_provider_runtime.binding import Binding
from cruxible_provider_runtime.execute import invoke
from cruxible_provider_runtime.manifest import BackendKind
from cruxible_provider_runtime.registry import StubRegistry

from .conftest import BACKENDS, BUDGETS, bind_operation
from .fixtures import FIXTURES

REPRESENTATIVE = {
    "calc.calibrate": "quant-calibrate-balanced",
    "calc.reduce": "quant-reduce-small",
    "match.record": "quant-linkage-weak-blocking",
    "score.rank": "quant-rank-small",
    "stat.test": "quant-stat-location-independent",
    "ts.anomaly": "quant-anomaly-rates",
    "ts.forecast": "quant-forecast-medium-continuous",
}

CASES = [
    pytest.param(fixture, backend, id=f"{fixture.fixture_id}-{backend}")
    for fixture in FIXTURES
    if fixture.fixture_id in REPRESENTATIVE.values()
    for backend in BACKENDS
]


def test_every_operation_has_a_representative() -> None:
    """A new implementation must not slip in without a full-loop case."""

    assert set(REPRESENTATIVE) == set(OPERATIONS)
    assert {fixture.fixture_id for fixture in FIXTURES} >= set(REPRESENTATIVE.values())


@pytest.mark.parametrize(("fixture", "backend"), CASES)
def test_success_path_on_both_backends(
    fixture: object,
    backend: BackendKind,
    registry: StubRegistry,
    manifest_path: Path,
    lock_path: Path,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> None:
    operation = fixture.operation  # type: ignore[attr-defined]
    binding = bind_operation(
        operation,
        backend,
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
    assert outcome.operation == operation
    assert outcome.operation_signature_digest == binding.operation_signature_digest
    assert outcome.envelope.output is not None
    # Pure computation: zero declared endpoints, zero observed.
    assert outcome.egress.observed == ()
    assert outcome.egress.declared == ()
    assert outcome.receipt_fields()["implementation_digest"] == binding.implementation_digest


def test_a_chatty_engine_does_not_corrupt_the_result_envelope(
    registry: StubRegistry,
    manifest_path: Path,
    lock_path: Path,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> None:
    """splink prints timing lines. The envelope still parses, over a real pipe.

    This is the assertion the descriptor-level redirect exists for. The child
    harness writes its envelope to fd 1 and a single line ahead of it is a
    ``provider_protocol_violation``; ``contextlib.redirect_stdout`` would not
    have caught it, because DuckDB writes through the C runtime.
    """

    fixture = next(f for f in FIXTURES if f.fixture_id == "quant-linkage-weak-blocking")
    binding = bind_operation(
        "match.record",
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
        payload=fixture.payload,
        budgets=BUDGETS,
        local_backend=local_backend,
        container_backend=container_backend,
    )
    assert outcome.status == "ok"
    assert outcome.envelope.output is not None
    assert "Blocking time" not in str(outcome.envelope.output)
    # Diverted, not discarded: the engine's chatter is on the stream the executor
    # captures for exhaust.
    assert "Predict time" in outcome.stderr


def test_the_implementation_digest_is_identical_across_backends(
    registry: StubRegistry,
    manifest_path: Path,
    lock_path: Path,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> None:
    """A backend switch must never split earned track record."""

    for operation in OPERATIONS:
        local = bind_operation(
            operation,
            "local_env",
            registry,
            manifest_path,
            lock_path,
            local_backend,
            container_backend,
        )
        container = bind_operation(
            operation,
            "container",
            registry,
            manifest_path,
            lock_path,
            local_backend,
            container_backend,
        )
        assert local.implementation_digest == container.implementation_digest
        assert local.materialization_digest != container.materialization_digest


def test_each_operation_gets_its_own_implementation_digest(
    registry: StubRegistry,
    manifest_path: Path,
    lock_path: Path,
    local_backend: LocalEnvBackend,
) -> None:
    """Seven implementations in one distribution are seven track records.

    They share a lock and therefore a materialization digest, which is right —
    they are one environment. They must not share an implementation digest, or
    a forecaster's track record would absorb a reducer's.
    """

    bindings: dict[str, Binding] = {
        operation: bind_operation(
            operation, "local_env", registry, manifest_path, lock_path, local_backend
        )
        for operation in OPERATIONS
    }
    implementations = {b.implementation_digest for b in bindings.values()}
    materializations = {b.materialization_digest for b in bindings.values()}
    assert len(implementations) == len(OPERATIONS)
    assert len(materializations) == 1


def test_the_binding_snapshot_carries_all_three_identity_levels(
    registry: StubRegistry,
    manifest_path: Path,
    lock_path: Path,
    local_backend: LocalEnvBackend,
) -> None:
    binding = bind_operation(
        "calc.reduce", "local_env", registry, manifest_path, lock_path, local_backend
    )
    snapshot = binding.snapshot()
    assert snapshot["implementation_digest"] == binding.implementation_digest
    assert snapshot["materialization_digest"] == binding.materialization_digest
    assert snapshot["protocol_version"] == "2.0"
    assert snapshot["operation"] == "calc.reduce"
    assert snapshot["operation_signature_digest"] == binding.operation_signature_digest
    assert snapshot["build_digest"] == binding.build_digest
    # This suite binds with the dev escape hatch, because the package depends on
    # the runtime by path. The snapshot says so rather than staying silent.
    assert snapshot["dev_sources_permitted"] is True
