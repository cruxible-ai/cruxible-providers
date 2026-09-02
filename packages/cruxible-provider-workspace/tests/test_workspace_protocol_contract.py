"""The adapter against the provider-runtime protocol exactly as core mirrors it.

``fixtures/provider_runtime_contract_v1.json`` is a verbatim copy of the fixture
core pins in ``tests/test_guardrails/test_provider_runtime_contract_mirror.py``
(P2-B2, tip 61f68629; unchanged since review round 1). Three things are asserted
against it, in the direction core cannot assert them:

1. the copy is byte-identical to what core pins (its sha256 is a literal here);
2. this runtime's wire models reproduce the schemas core froze, so the envelope
   the child writes is the envelope core parses;
3. driving the child harness with core's run-context vector, adapted to this
   interface, yields an envelope of exactly the frozen shape.

**When B2 lands, re-verify the literal digest below against the landed tip
before core pins this package's digests.**
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from cruxible_provider_runtime import protocol
from cruxible_provider_runtime.egress import DYNAMIC_ENDPOINT_FORMS
from cruxible_provider_runtime.errors import ProviderErrorPayload, Refusal, RefusalCode
from cruxible_provider_runtime.protocol import PROTOCOL_VERSION, parse_result_envelope

from .conftest import RUNTIME_SRC, WORKSPACE_SRC

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "provider_runtime_contract_v1.json"

# tests/test_guardrails/test_provider_runtime_contract_mirror.py at core 61f68629:
# PROVIDER_RUNTIME_CONTRACT_FIXTURE_DIGEST.
CORE_PINNED_FIXTURE_DIGEST = (
    "sha256:56b1d2799515c84f3848d08c79b33a3297280ebef82232701612ccf3ce4488c7"
)
# provider_runtime_contract.PROVIDER_RUNTIME_CONTRACT_COMMIT at core 61f68629:
# the commit of THIS repository core mirrored the protocol from.
CORE_PINNED_PROVIDER_COMMIT = "389e9f44de56c1adebae731228cf4628c6fbeca8"

ENTRYPOINT = "cruxible_provider_workspace.file:WorkspaceFile"

_MODEL_MAP: dict[str, type[Any]] = {
    "Budgets": protocol.Budgets,
    "ProtocolVersion": protocol.ProtocolVersion,
    "ProviderErrorPayload": ProviderErrorPayload,
    "Refusal": Refusal,
    "ResultEnvelope": protocol.ResultEnvelope,
    "RunContext": protocol.RunContext,
    "SecretChannelSpec": protocol.SecretChannelSpec,
    "SecretRef": protocol.SecretRef,
    "Trace": protocol.Trace,
}


def _fixture() -> dict[str, Any]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def test_the_vendored_fixture_is_byte_identical_to_what_core_pins() -> None:
    raw = FIXTURE_PATH.read_bytes()
    assert "sha256:" + hashlib.sha256(raw).hexdigest() == CORE_PINNED_FIXTURE_DIGEST


def test_the_fixture_names_this_runtime_and_its_closed_vocabulary() -> None:
    fixture = _fixture()
    assert fixture["provider_commit"] == CORE_PINNED_PROVIDER_COMMIT
    assert fixture["protocol_version"] == PROTOCOL_VERSION.render()
    assert tuple(fixture["dynamic_endpoint_forms"]) == tuple(sorted(DYNAMIC_ENDPOINT_FORMS))
    assert fixture["refusal_codes"] == sorted(
        (code.value for code in RefusalCode), key=lambda value: value.encode("utf-8")
    )


@pytest.mark.parametrize("name", sorted(_MODEL_MAP))
def test_this_runtime_reproduces_the_schema_core_froze(name: str) -> None:
    assert _MODEL_MAP[name].model_json_schema() == _fixture()["schemas"][name]


def _workspace_context(fixture: dict[str, Any], data: bytes) -> dict[str, Any]:
    """Core's run-context vector, re-pointed at this interface and adapter."""

    context = dict(fixture["valid_vectors"]["run_context"])
    context.update(
        {
            "interface_id": "workspace.file",
            "entrypoint": ENTRYPOINT,
            "input_bucket": "content_kind=text;byte_size=tiny",
            "capture_contract": "workspace.file.capture.v1",
            "declared_endpoints": [],
            "secret_channel": None,
            "input": {
                "logical_source": "notes/readings.md",
                "commitment_digest": "sha256:" + "c0" * 32,
                "content_encoding": "base64",
                "bytes": base64.b64encode(data).decode("ascii"),
                "byte_length": len(data),
                "bytes_digest": "sha256:" + hashlib.sha256(data).hexdigest(),
            },
        }
    )
    return context


def _run_child(context: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Run the child harness the way core's fence wrapper does: module, entrypoint, stdio."""

    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": os.pathsep.join(str(root) for root in (RUNTIME_SRC, WORKSPACE_SRC)),
    }
    completed = subprocess.run(
        [sys.executable, "-m", "cruxible_provider_runtime.child", "--entrypoint", ENTRYPOINT],
        input=json.dumps(context, separators=(",", ":")).encode("utf-8"),
        capture_output=True,
        env=environment,
        check=True,
        timeout=60,
    )
    envelope = parse_result_envelope(completed.stdout)
    return json.loads(envelope.model_dump_json()), completed.stderr.decode("utf-8", "replace")


def _shape(document: dict[str, Any]) -> dict[str, Any]:
    """The envelope with its variable values erased: keys, and the types under them."""

    return {
        key: (_shape(value) if isinstance(value, dict) else type(value).__name__)
        for key, value in document.items()
    }


def test_an_ok_run_yields_the_frozen_ok_envelope_shape() -> None:
    fixture = _fixture()
    data = b"alpha\nbeta\n"
    envelope, _ = _run_child(_workspace_context(fixture, data))
    expected = fixture["valid_vectors"]["results"]["ok"]
    assert envelope["status"] == "ok"
    assert envelope["run_id"] == expected["run_id"]
    assert envelope["protocol_version"] == expected["protocol_version"]
    assert set(envelope) == set(expected)
    assert envelope["trace"]["endpoints_contacted"] == []
    assert envelope["trace"]["events"] == []
    assert envelope["refusal"] is None and envelope["error"] is None
    assert envelope["output"]["content"]["lines"] == ["alpha", "beta"]
    assert envelope["output"]["source"]["bytes_digest"] == (
        "sha256:" + hashlib.sha256(data).hexdigest()
    )


def test_a_declined_run_yields_the_frozen_refused_envelope_shape() -> None:
    fixture = _fixture()
    context = _workspace_context(fixture, b"alpha\n")
    context["input"]["bytes_digest"] = "sha256:" + "ab" * 32
    envelope, _ = _run_child(context)
    expected = fixture["valid_vectors"]["results"]["refused"]
    assert _shape({**envelope, "refusal": {**envelope["refusal"], "detail": {}}}) == _shape(
        {**expected, "refusal": {**expected["refusal"], "detail": {}}}
    )
    assert envelope["refusal"]["code"] == "provider_declined"
    assert envelope["refusal"]["code"] in fixture["refusal_codes"]


def test_every_refusal_the_adapter_can_emit_is_in_the_frozen_vocabulary() -> None:
    from cruxible_provider_workspace.interface import INTERFACE_PREIMAGE

    assert set(INTERFACE_PREIMAGE["refusals"]) <= set(_fixture()["refusal_codes"])


def test_a_field_outside_the_additive_region_refuses_in_the_child() -> None:
    """Core's invalid vector, through this child: the refusal code core expects."""

    fixture = _fixture()
    context = _workspace_context(fixture, b"alpha\n")
    context["not_additive"] = True
    envelope, _ = _run_child(context)
    assert envelope["status"] == "refused"
    assert envelope["refusal"]["code"] == "unknown_run_context_field"


def test_the_additive_region_is_carried_and_ignored() -> None:
    fixture = _fixture()
    context = _workspace_context(fixture, b"alpha\n")
    context["additive"] = {"future": {"kept": True}, "size_cap_bytes": 1_048_576}
    envelope, _ = _run_child(context)
    assert envelope["status"] == "ok"


def test_the_child_writes_nothing_but_the_envelope_on_stdout() -> None:
    fixture = _fixture()
    _, stderr = _run_child(_workspace_context(fixture, b"alpha\n"))
    assert stderr == ""


def test_the_receipt_fields_core_records_exist_on_an_outcome(
    registry: Any,
    manifest_path: Path,
    lock_path: Path,
    local_backend: Any,
    container_backend: Any,
) -> None:
    from cruxible_provider_runtime.binding import BindRequest, bind
    from cruxible_provider_runtime.execute import invoke
    from cruxible_provider_runtime.protocol import Budgets

    from .conftest import INTERFACE_ID, MARKER_ENVIRONMENT, PROVIDER_ID

    binding = bind(
        registry,
        BindRequest(
            provider_id=PROVIDER_ID,
            interface_id=INTERFACE_ID,
            backend_kind="local_env",
            manifest_path=manifest_path,
            lock_path=lock_path,
            marker_environment=MARKER_ENVIRONMENT,
            allow_editable_dev_sources=True,
        ),
        local_backend=local_backend,
        container_backend=container_backend,
    )
    data = b"alpha\n"
    outcome = invoke(
        binding,
        registry=registry,
        payload=_workspace_context(_fixture(), data)["input"],
        budgets=Budgets(wall_clock_seconds=30.0, output_bytes=8_000_000),
        local_backend=local_backend,
        container_backend=container_backend,
    )
    assert set(_fixture()["invocation_outcome_receipt_fields"]) <= set(outcome.receipt_fields())
