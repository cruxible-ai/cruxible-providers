"""The child harness: dispatch by entrypoint, checked against the run's operation.

Each test runs the real harness in a subprocess over a provider module written
for it, so what is asserted is what an executor reads back across the pipe.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from cruxible_provider_runtime.backends import CHILD_MODULE
from cruxible_provider_runtime.errors import RefusalCode
from cruxible_provider_runtime.protocol import (
    PROTOCOL_VERSION,
    Budgets,
    ResultEnvelope,
    RunContext,
    parse_result_envelope,
)

RUNTIME_SRC = Path(__file__).resolve().parents[1] / "src"

_PROVIDERS = '''
from cruxible_provider_runtime.errors import decline
from cruxible_provider_runtime.provider_api import ProviderResult


class Cases:
    """One entrypoint serving two operations, told apart by the run's operation."""

    operations = ("sample.lower", "sample.upper")

    def __call__(self, context):
        text = context.input["text"]
        if context.operation == "sample.upper":
            return ProviderResult.ok({"text": text.upper(), "operation": context.operation})
        return ProviderResult.ok({"text": text.lower(), "operation": context.operation})


class Silent:
    """Declares nothing it serves."""

    def __call__(self, context):
        return ProviderResult.ok({})


class Deep:
    operations = ("sample.deep",)

    def __call__(self, context):
        self._check(context.input)
        return ProviderResult.ok({})

    def _check(self, value):
        raise decline("too_long", "the text is too long", limit=3)


class Echo:
    operations = ("sample.echo",)

    def __call__(self, context):
        return ProviderResult.ok(
            {
                "operation": context.operation,
                "operation_signature_digest": context.operation_signature_digest,
                "build_digest": context.build_digest,
                "implementation_digest": context.implementation_digest,
            }
        )
'''

SIGNATURE = "sha256:" + "51" * 32
BUILD = "sha256:" + "b1" * 32
IMPLEMENTATION = "sha256:" + "1a" * 32


@pytest.fixture()
def provider_root(tmp_path: Path) -> Path:
    (tmp_path / "child_sample_providers.py").write_text(_PROVIDERS, encoding="utf-8")
    return tmp_path


def _context(operation: str, entrypoint: str, **overrides: Any) -> dict[str, Any]:
    document: dict[str, Any] = RunContext(
        protocol_version=PROTOCOL_VERSION.render(),
        run_id="run-child",
        operation=operation,
        operation_signature_digest=SIGNATURE,
        build_digest=BUILD,
        implementation_digest=IMPLEMENTATION,
        entrypoint=f"child_sample_providers:{entrypoint}",
        input={"text": "MiXeD"},
        budgets=Budgets(wall_clock_seconds=30.0, output_bytes=65536),
    ).model_dump(mode="json")
    document.update(overrides)
    return document


def _run(
    root: Path, document: dict[str, Any], *, argv_entrypoint: str | None = None
) -> ResultEnvelope:
    entrypoint = argv_entrypoint or str(document.get("entrypoint", ""))
    completed = subprocess.run(
        [sys.executable, "-m", CHILD_MODULE, "--entrypoint", entrypoint],
        input=json.dumps(document).encode("utf-8"),
        capture_output=True,
        env={**os.environ, "PYTHONPATH": os.pathsep.join((str(RUNTIME_SRC), str(root)))},
        check=True,
        timeout=60,
    )
    return parse_result_envelope(completed.stdout)


@pytest.mark.parametrize(
    ("operation", "expected"), [("sample.upper", "MIXED"), ("sample.lower", "mixed")]
)
def test_one_entrypoint_serves_each_operation_it_declares(
    provider_root: Path, operation: str, expected: str
) -> None:
    envelope = _run(provider_root, _context(operation, "Cases"))
    assert envelope.status == "ok", envelope
    assert envelope.output == {"text": expected, "operation": operation}
    assert envelope.protocol_version == PROTOCOL_VERSION.render()


def test_the_provider_reads_its_operation_signature_and_build(provider_root: Path) -> None:
    envelope = _run(provider_root, _context("sample.echo", "Echo"))
    assert envelope.output == {
        "operation": "sample.echo",
        "operation_signature_digest": SIGNATURE,
        "build_digest": BUILD,
        "implementation_digest": IMPLEMENTATION,
    }


def test_an_operation_the_entrypoint_does_not_declare_refuses(provider_root: Path) -> None:
    envelope = _run(provider_root, _context("sample.title", "Cases"))
    assert envelope.status == "refused"
    assert envelope.refusal is not None
    assert envelope.refusal.code == RefusalCode.UNDECLARED_OPERATION
    assert envelope.refusal.detail["declared"] == ["sample.lower", "sample.upper"]
    assert envelope.run_id == "run-child"


def test_an_entrypoint_that_declares_no_operations_is_a_protocol_violation(
    provider_root: Path,
) -> None:
    """Absent is not "serves everything"."""

    envelope = _run(provider_root, _context("sample.echo", "Silent"))
    assert envelope.status == "refused"
    assert envelope.refusal is not None
    assert envelope.refusal.code == RefusalCode.PROVIDER_PROTOCOL_VIOLATION


def test_the_harness_argument_must_agree_with_the_context(provider_root: Path) -> None:
    envelope = _run(
        provider_root,
        _context("sample.upper", "Cases"),
        argv_entrypoint="child_sample_providers:Echo",
    )
    assert envelope.status == "refused"
    assert envelope.refusal is not None
    assert envelope.refusal.code == RefusalCode.PROVIDER_PROTOCOL_VIOLATION
    assert envelope.refusal.detail["argv_entrypoint"] == "child_sample_providers:Echo"


def test_a_decline_raised_from_depth_is_the_operations_refusal(provider_root: Path) -> None:
    envelope = _run(provider_root, _context("sample.deep", "Deep"))
    assert envelope.status == "refused"
    assert envelope.refusal is not None
    assert envelope.refusal.code == "too_long"
    assert envelope.refusal.detail == {"limit": 3}
    assert not envelope.refusal.is_runtime


def test_a_protocol_1_context_is_refused_under_its_own_run(provider_root: Path) -> None:
    """A 1.x executor's context is answered, typed, under the run it asked about."""

    document = {
        "protocol_version": "1.0",
        "run_id": "run-from-a-1x-executor",
        "interface_id": "sample.upper",
        "interface_digest": SIGNATURE,
        "implementation_digest": IMPLEMENTATION,
        "entrypoint": "child_sample_providers:Cases",
        "input": {"text": "MiXeD"},
        "input_bucket": "legacy=none",
        "budgets": {"wall_clock_seconds": 30.0, "output_bytes": 65536},
        "additive": {"operation": "sample.upper"},
    }
    envelope = _run(provider_root, document)
    assert envelope.status == "refused"
    assert envelope.run_id == "run-from-a-1x-executor"
    assert envelope.refusal is not None
    assert envelope.refusal.code == RefusalCode.UNSUPPORTED_PROTOCOL
    assert envelope.refusal.detail == {"supported_major": 2, "requested": "1.0"}
