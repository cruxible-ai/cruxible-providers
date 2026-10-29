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


class Forger:
    """Raises the runtime's own codes, as if it were the runtime."""

    operations = ("sample.forge",)

    def __call__(self, context):
        from cruxible_provider_runtime.errors import RefusalCode, refuse

        raise refuse(RefusalCode(context.input["code"]), "provider invented this")


class ConstructionForger:
    operations = ("sample.forge",)

    def __init__(self):
        from cruxible_provider_runtime.errors import RefusalCode, refuse

        raise refuse(RefusalCode.UNRESOLVED_SECRET_REF, "provider invented this")

    def __call__(self, context):
        return ProviderResult.ok({})


class DeclarationForger:
    @property
    def operations(self):
        from cruxible_provider_runtime.errors import RefusalCode, refuse

        raise refuse(RefusalCode.UNRESOLVED_SECRET_REF, "provider invented this")

    def __call__(self, context):
        return ProviderResult.ok({})


class _ForgingMapping(dict):
    def items(self):
        from cruxible_provider_runtime.errors import RefusalCode, refuse

        raise refuse(RefusalCode.UNRESOLVED_SECRET_REF, "provider invented this")


class ResultForger:
    """Returns an output whose own methods assert a runtime code once read."""

    operations = ("sample.forge",)

    def __call__(self, context):
        return ProviderResult.ok(_ForgingMapping(value=1))


class _ForgingException(Exception):
    def __str__(self):
        from cruxible_provider_runtime.errors import RefusalCode, refuse

        raise refuse(RefusalCode.UNRESOLVED_SECRET_REF, "provider invented this")


class DescriptionForger:
    """Fails with an exception whose description asserts a runtime code."""

    operations = ("sample.forge",)

    def __call__(self, context):
        raise _ForgingException()


class EarlyDecliner:
    operations = ("sample.forge",)

    def __init__(self):
        from cruxible_provider_runtime.errors import decline

        raise decline("too_long", "declined before running")

    def __call__(self, context):
        return ProviderResult.ok({})


class NonFinite:
    operations = ("sample.nan",)

    def __call__(self, context):
        return ProviderResult.ok({"statistic": float("nan")})


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


_IMPORT_FORGER = """
from cruxible_provider_runtime.errors import RefusalCode, refuse

raise refuse(RefusalCode.UNRESOLVED_SECRET_REF, "provider invented this at import")
"""


@pytest.fixture()
def provider_root(tmp_path: Path) -> Path:
    (tmp_path / "child_sample_providers.py").write_text(_PROVIDERS, encoding="utf-8")
    (tmp_path / "child_import_forger.py").write_text(_IMPORT_FORGER, encoding="utf-8")
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


@pytest.mark.parametrize(
    "code",
    [
        RefusalCode.BUDGET_WALL_CLOCK,
        RefusalCode.CACHE_INTEGRITY,
        RefusalCode.UNRESOLVED_SECRET_REF,
        RefusalCode.UNDECLARED_EGRESS,
        RefusalCode.ENVIRONMENT_DIVERGENCE,
    ],
)
def test_provider_code_cannot_raise_a_runtime_refusal(
    provider_root: Path, code: RefusalCode
) -> None:
    """Only runtime machinery produces runtime codes; a raised one is a protocol violation."""

    document = _context("sample.forge", "Forger", input={"code": code.value})
    envelope = _run(provider_root, document)
    assert envelope.status == "refused"
    assert envelope.refusal is not None
    assert envelope.refusal.code == RefusalCode.PROVIDER_PROTOCOL_VIOLATION
    assert envelope.refusal.detail["refused_code"] == code.value
    assert envelope.refusal.detail["operation"] == "sample.forge"


@pytest.mark.parametrize(
    ("entrypoint", "stage"),
    [
        ("child_import_forger:Anything", "import"),
        ("child_sample_providers:ConstructionForger", "construction"),
        ("child_sample_providers:DeclarationForger", "operation declaration"),
        ("child_sample_providers:ResultForger", "call"),
    ],
)
def test_a_harness_code_from_provider_code_at_any_stage_is_a_protocol_violation(
    provider_root: Path, entrypoint: str, stage: str
) -> None:
    """``unresolved_secret_ref`` is one the harness produces itself, so it is the
    code a forgery would choose; provider code asserting it -- importing,
    constructing, declaring, or in what it returns -- is reported as what it is."""

    document = _context("sample.forge", "Forger")
    document["entrypoint"] = entrypoint
    envelope = _run(provider_root, document)
    assert envelope.status == "refused"
    assert envelope.refusal is not None
    assert envelope.refusal.code == RefusalCode.PROVIDER_PROTOCOL_VIOLATION
    assert envelope.refusal.detail["stage"] == stage
    assert envelope.refusal.detail["refused_code"] == "unresolved_secret_ref"
    assert envelope.refusal.detail["entrypoint"] == entrypoint


def test_an_exception_whose_description_asserts_a_runtime_code_is_still_a_failure(
    provider_root: Path,
) -> None:
    """Describing a provider's exception runs its code too; the assertion does not escape."""

    envelope = _run(provider_root, _context("sample.forge", "DescriptionForger"))
    assert envelope.status == "error"
    assert envelope.refusal is None
    assert envelope.error is not None
    assert envelope.error.kind == "_ForgingException"
    assert envelope.error.message == "the provider raised an exception it would not describe"


def test_a_decline_before_the_operation_runs_is_a_protocol_violation(provider_root: Path) -> None:
    envelope = _run(provider_root, _context("sample.forge", "EarlyDecliner"))
    assert envelope.status == "refused"
    assert envelope.refusal is not None
    assert envelope.refusal.code == RefusalCode.PROVIDER_PROTOCOL_VIOLATION
    assert envelope.refusal.detail == {
        "stage": "construction",
        "entrypoint": "child_sample_providers:EarlyDecliner",
        "operation": "sample.forge",
        "refused_code": "too_long",
    }


def test_the_harness_still_observes_a_non_finite_output(provider_root: Path) -> None:
    """Copying the result out of provider code does not hide what the harness checks."""

    envelope = _run(provider_root, _context("sample.nan", "NonFinite"))
    assert envelope.status == "refused"
    assert envelope.refusal is not None
    assert envelope.refusal.code == RefusalCode.NON_FINITE_OUTPUT
    assert envelope.refusal.detail["paths"] == ["statistic"]


def test_the_harness_still_observes_a_missing_secret_ref(provider_root: Path) -> None:
    """A secret channel short of a ref it names is the harness's own observation."""

    read_fd, write_fd = os.pipe()
    os.write(write_fd, json.dumps({"present": "value"}).encode("utf-8"))
    os.close(write_fd)
    document = _context(
        "sample.echo",
        "Echo",
        secret_channel={"kind": "fd", "fd": read_fd, "refs": [{"ref": "absent"}]},
    )
    try:
        completed = subprocess.run(
            [sys.executable, "-m", CHILD_MODULE, "--entrypoint", document["entrypoint"]],
            input=json.dumps(document).encode("utf-8"),
            capture_output=True,
            env={
                **os.environ,
                "PYTHONPATH": os.pathsep.join((str(RUNTIME_SRC), str(provider_root))),
            },
            pass_fds=(read_fd,),
            check=True,
            timeout=60,
        )
    finally:
        os.close(read_fd)
    envelope = parse_result_envelope(completed.stdout)
    assert envelope.status == "refused"
    assert envelope.refusal is not None
    assert envelope.refusal.code == RefusalCode.UNRESOLVED_SECRET_REF
    assert envelope.refusal.detail["refs"] == ["absent"]
