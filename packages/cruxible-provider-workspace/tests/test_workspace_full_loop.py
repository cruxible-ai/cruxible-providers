"""The built-in's full bind-and-invoke loop, on both backend kinds.

Every assertion here is about a rule the RP-0 contract states, exercised end to
end through a real child process: the adapter is imported, run, and read back
across a pipe -- the same harness core's fence wrapper runs.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from cruxible_provider_runtime.backends import ContainerBackend, LocalEnvBackend
from cruxible_provider_runtime.binding import Binding, BindRequest, bind
from cruxible_provider_runtime.canonical import canonical_json, sha256_hex
from cruxible_provider_runtime.digests import implementation_digest
from cruxible_provider_runtime.errors import RefusalCode, RefusalError
from cruxible_provider_runtime.execute import invoke
from cruxible_provider_runtime.manifest import BackendKind
from cruxible_provider_runtime.protocol import Budgets
from cruxible_provider_runtime.registry import StubRegistry
from cruxible_provider_workspace.fixtures import load_fixtures

from .conftest import DISTRIBUTION_SHA256, INTERFACE_ID, MARKER_ENVIRONMENT, PROVIDER_ID

BUDGETS = Budgets(wall_clock_seconds=30.0, output_bytes=8_000_000)
BACKENDS: tuple[BackendKind, ...] = ("local_env", "container")
COMMITMENT = "sha256:" + "c0" * 32


def payload(data: bytes, **overrides: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "logical_source": "notes/readings.md",
        "commitment_digest": COMMITMENT,
        "content_encoding": "base64",
        "bytes": base64.b64encode(data).decode("ascii"),
        "byte_length": len(data),
        "bytes_digest": "sha256:" + hashlib.sha256(data).hexdigest(),
    }
    document.update(overrides)
    return document


def _bind(
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
    return _bind(backend_kind, registry, manifest_path, lock_path, local_backend, container_backend)


@pytest.mark.parametrize("binding", BACKENDS, indirect=True)
def test_success_path(
    binding: Binding,
    registry: StubRegistry,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> None:
    data = b"alpha\nbeta\n"
    outcome = invoke(
        binding,
        registry=registry,
        payload=payload(data),
        budgets=BUDGETS,
        local_backend=local_backend,
        container_backend=container_backend,
    )
    assert outcome.status == "ok"
    assert outcome.input_bucket == "content_kind=text;byte_size=tiny"
    assert outcome.envelope.output is not None
    assert outcome.envelope.output["input_bucket"] == outcome.input_bucket
    assert outcome.envelope.output["source"]["bytes_digest"] == sha256_hex(data)
    assert outcome.envelope.output["content"]["lines"] == ["alpha", "beta"]
    assert outcome.egress.observed == ()
    assert outcome.envelope.trace.endpoints_contacted == []
    assert outcome.envelope.trace.events == []
    assert outcome.receipt_fields()["implementation_digest"] == binding.implementation_digest


@pytest.mark.parametrize("binding", BACKENDS, indirect=True)
def test_a_committed_fixture_reproduces_its_body_digest_through_the_child(
    binding: Binding,
    registry: StubRegistry,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> None:
    """The fixture's pinned body digest holds across the process boundary too."""

    fixture = load_fixtures()["workspace-file-text-small"]
    outcome = invoke(
        binding,
        registry=registry,
        payload=fixture.input,
        budgets=BUDGETS,
        local_backend=local_backend,
        container_backend=container_backend,
    )
    assert outcome.status == "ok"
    assert outcome.input_bucket == fixture.bucket_id
    assert sha256_hex(canonical_json(outcome.envelope.output)) == fixture.expect["output_digest"]


@pytest.mark.parametrize("binding", BACKENDS, indirect=True)
def test_typed_refusal_path(
    binding: Binding,
    registry: StubRegistry,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> None:
    outcome = invoke(
        binding,
        registry=registry,
        payload=payload(b"hello", bytes_digest="sha256:" + "ab" * 32),
        budgets=BUDGETS,
        local_backend=local_backend,
        container_backend=container_backend,
    )
    assert outcome.status == "refused"
    assert outcome.envelope.refusal is not None
    assert outcome.envelope.refusal.code is RefusalCode.PROVIDER_DECLINED
    assert outcome.envelope.output is None


@pytest.mark.parametrize("binding", BACKENDS, indirect=True)
def test_a_length_mismatch_is_a_typed_refusal_across_the_boundary(
    binding: Binding,
    registry: StubRegistry,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> None:
    outcome = invoke(
        binding,
        registry=registry,
        payload=payload(b"hello", byte_length=6),
        budgets=BUDGETS,
        local_backend=local_backend,
        container_backend=container_backend,
    )
    assert outcome.status == "refused"
    assert outcome.envelope.refusal is not None
    assert outcome.envelope.refusal.code is RefusalCode.MISMATCHED_LENGTHS


@pytest.mark.parametrize("binding", BACKENDS, indirect=True)
def test_a_large_file_refuses_as_unclaimed_before_any_process_starts(
    binding: Binding,
    registry: StubRegistry,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
    builder: object,
    container_driver: object,
) -> None:
    with pytest.raises(RefusalError) as exc:
        invoke(
            binding,
            registry=registry,
            payload=payload(b"x" * 1_100_000),
            budgets=BUDGETS,
            local_backend=local_backend,
            container_backend=container_backend,
        )
    assert exc.value.code is RefusalCode.UNCLAIMED_BUCKET
    assert exc.value.refusal.detail["bucket"] == "content_kind=text;byte_size=large"
    assert container_driver.runs == []  # type: ignore[attr-defined]


def test_an_undecodable_payload_refuses_as_unclassified(
    binding: Binding,
    registry: StubRegistry,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> None:
    with pytest.raises(RefusalError) as exc:
        invoke(
            binding,
            registry=registry,
            payload=payload(b"hello", bytes="this is not base64"),
            budgets=BUDGETS,
            local_backend=local_backend,
            container_backend=container_backend,
        )
    assert exc.value.code is RefusalCode.UNCLASSIFIED_INPUT


def test_binary_input_classifies_into_its_own_bucket(
    binding: Binding,
    registry: StubRegistry,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> None:
    outcome = invoke(
        binding,
        registry=registry,
        payload=payload(b"\x89PNG\r\n\x1a\n\x00"),
        budgets=BUDGETS,
        local_backend=local_backend,
        container_backend=container_backend,
    )
    assert outcome.input_bucket == "content_kind=binary;byte_size=tiny"
    assert outcome.envelope.output is not None
    assert outcome.envelope.output["content"]["kind"] == "bytes"


def test_a_delivered_secret_never_appears_in_the_envelope(
    binding: Binding,
    registry: StubRegistry,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> None:
    """The adapter takes no secret; one delivered anyway must leave no trace."""

    secret = "dummy-credential-c0ffee-do-not-use"
    outcome = invoke(
        binding,
        registry=registry,
        payload=payload(b"hello\n"),
        budgets=BUDGETS,
        secrets={"workspace.unused": secret},
        local_backend=local_backend,
        container_backend=container_backend,
    )
    assert outcome.status == "ok"
    assert secret not in outcome.envelope.model_dump_json()
    assert secret not in outcome.stderr


def test_implementation_digest_is_identical_across_backends(
    registry: StubRegistry,
    manifest_path: Path,
    lock_path: Path,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> None:
    """A backend switch must never split earned track record."""

    local = _bind("local_env", registry, manifest_path, lock_path, local_backend, container_backend)
    container = _bind(
        "container", registry, manifest_path, lock_path, local_backend, container_backend
    )
    assert local.implementation_digest == container.implementation_digest
    assert local.materialization_digest != container.materialization_digest


def test_implementation_digest_matches_the_documented_preimage(binding: Binding) -> None:
    assert binding.implementation_digest == implementation_digest(
        interface_id=INTERFACE_ID,
        interface_digest=binding.interface_digest,
        entrypoint="cruxible_provider_workspace.file:WorkspaceFile",
        distribution_sha256=DISTRIBUTION_SHA256,
    )


def test_binding_snapshot_carries_all_three_identity_levels(binding: Binding) -> None:
    snapshot = binding.snapshot()
    assert snapshot["implementation_digest"] == binding.implementation_digest
    assert snapshot["materialization_digest"] == binding.materialization_digest
    assert snapshot["protocol_version"] == "1.0"
    assert snapshot["dev_sources_permitted"] is True


def test_the_receipt_carries_the_fields_core_records(
    binding: Binding,
    registry: StubRegistry,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> None:
    outcome = invoke(
        binding,
        registry=registry,
        payload=payload(b"hello\n"),
        budgets=BUDGETS,
        local_backend=local_backend,
        container_backend=container_backend,
    )
    receipt = outcome.receipt_fields()
    assert receipt["materialization_digest"] == binding.materialization_digest
    assert receipt["input_bucket"] == "content_kind=text;byte_size=tiny"
    assert receipt["status"] == "ok"
    assert json.dumps(receipt)
