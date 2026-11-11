"""The adapter against the provider-runtime protocol 2.0 contract core vendors.

``packages/cruxible-provider-runtime/tests/fixtures/provider_runtime_contract_v2.json``
is produced by ``scripts/export_runtime_contract.py`` from this runtime's own
models, and is the fixture core pins for its mirror. Driving the child harness
with its run-context vector, re-pointed at this operation, yields envelopes of
exactly the shapes the fixture's result vectors carry; its protocol-1.x vector
is refused the way core will see it refused.
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

from cruxible_provider_runtime.protocol import PROTOCOL_VERSION, parse_result_envelope
from cruxible_provider_workspace.file import PROVIDER_DECLINED

from .conftest import ENTRYPOINT, OPERATION, REPO_ROOT, RUNTIME_SRC, WORKSPACE_SRC

FIXTURE_PATH = (
    REPO_ROOT
    / "packages"
    / "cruxible-provider-runtime"
    / "tests"
    / "fixtures"
    / "provider_runtime_contract_v2.json"
)


def _fixture() -> dict[str, Any]:
    document: dict[str, Any] = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    return document


def test_the_fixture_is_this_runtimes_protocol() -> None:
    assert _fixture()["protocol_version"] == PROTOCOL_VERSION.render()


def _workspace_context(fixture: dict[str, Any], data: bytes) -> dict[str, Any]:
    """The contract's run-context vector, re-pointed at this operation and adapter."""

    context = dict(fixture["valid_vectors"]["run_context"])
    context.update(
        {
            "operation": OPERATION,
            "entrypoint": ENTRYPOINT,
            "capture_contract": None,
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
    expected = fixture["valid_vectors"]["results"]["refused_by_operation"]
    assert _shape({**envelope, "refusal": {**envelope["refusal"], "detail": {}}}) == _shape(
        {**expected, "refusal": {**expected["refusal"], "detail": {}}}
    )
    assert envelope["refusal"]["code"] == PROVIDER_DECLINED
    assert envelope["refusal"]["code"] not in fixture["runtime_refusal_codes"]


def test_a_protocol_1_context_is_refused_typed_under_its_run() -> None:
    fixture = _fixture()
    context = dict(fixture["invalid_vectors"]["context_protocol_1"])
    context.update(
        {
            "interface_id": OPERATION,
            "entrypoint": ENTRYPOINT,
            "declared_endpoints": [],
            "secret_channel": None,
            "input": _workspace_context(fixture, b"alpha\n")["input"],
        }
    )
    envelope, _ = _run_child(context)
    assert envelope["status"] == "refused"
    assert envelope["run_id"] == context["run_id"]
    assert envelope["refusal"]["code"] == "unsupported_protocol"


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

    from .conftest import MARKER_ENVIRONMENT, PROVIDER_ID

    binding = bind(
        registry,
        BindRequest(
            provider_id=PROVIDER_ID,
            operation=OPERATION,
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
