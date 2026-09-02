"""The adapter in-process: every declaration checked, every text feature reported."""

from __future__ import annotations

import base64
import hashlib
from typing import Any

import pytest
from cruxible_provider_runtime.egress import EgressRecorder
from cruxible_provider_runtime.errors import RefusalCode
from cruxible_provider_runtime.protocol import Budgets
from cruxible_provider_runtime.provider_api import ProviderResult, ProviderRunContext
from cruxible_provider_workspace.file import INPUT_FIELDS, WorkspaceFile, structure_bytes
from cruxible_provider_workspace.interface import (
    INTERFACE_DIGEST,
    INTERFACE_ID,
    VOCABULARY,
    classify,
)

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


def run(document: dict[str, Any], *, secrets: dict[str, str] | None = None) -> ProviderResult:
    assignment = classify(document)
    bucket = VOCABULARY.bucket_id(assignment) if assignment is not None else "unmeasured"
    context = ProviderRunContext(
        run_id="run-adapter",
        interface_id=INTERFACE_ID,
        interface_digest=INTERFACE_DIGEST,
        implementation_digest="sha256:" + "11" * 32,
        input_bucket=bucket,
        input=document,
        coordinates={},
        budgets=Budgets(wall_clock_seconds=5.0, output_bytes=8_000_000),
        declared_endpoints=(),
        capture_contract="workspace.file.capture.v1",
        secrets=secrets or {},
        egress=EgressRecorder(),
    )
    return WorkspaceFile()(context)


# -- the success path ------------------------------------------------------


def test_text_body_carries_text_lines_and_the_verified_source_digest() -> None:
    data = b"alpha\nbeta\r\ngamma"
    result = run(payload(data))
    assert result.status == "ok"
    assert result.output is not None
    assert result.output["source"]["bytes_digest"] == "sha256:" + hashlib.sha256(data).hexdigest()
    assert result.output["source"]["commitment_digest"] == COMMITMENT
    assert result.output["source"]["logical_source"] == "notes/readings.md"
    content = result.output["content"]
    assert content["kind"] == "text"
    assert content["encoding"] == "utf-8"
    assert content["text"] == "alpha\nbeta\r\ngamma"
    assert content["lines"] == ["alpha", "beta", "gamma"]
    assert content["line_count"] == 3
    assert content["newline"] == "mixed"
    assert content["trailing_newline"] is False
    assert content["bom"] is False
    assert result.metrics == {"byte_length": 17.0, "line_count": 3.0, "character_count": 17.0}


def test_bytes_body_carries_the_canonical_base64_and_nothing_decoded() -> None:
    data = b"\x00\x01\x02\xff"
    result = run(payload(data))
    assert result.status == "ok"
    assert result.output is not None
    assert result.output["content"] == {
        "kind": "bytes",
        "encoding": "base64",
        "byte_length": 4,
        "bytes": base64.b64encode(data).decode("ascii"),
    }
    assert result.metrics == {"byte_length": 4.0}


def test_an_empty_file_is_text_with_no_lines() -> None:
    result = run(payload(b""))
    assert result.status == "ok"
    assert result.output is not None
    assert result.output["content"] == {
        "kind": "text",
        "encoding": "utf-8",
        "bom": False,
        "newline": "none",
        "trailing_newline": False,
        "line_count": 0,
        "character_count": 0,
        "text": "",
        "lines": [],
    }


@pytest.mark.parametrize(
    ("data", "newline", "trailing", "lines"),
    [
        (b"a\nb\n", "lf", True, ["a", "b"]),
        (b"a\r\nb\r\n", "crlf", True, ["a", "b"]),
        (b"a\rb\r", "cr", True, ["a\rb\r"]),
        (b"a\nb", "lf", False, ["a", "b"]),
        (b"a\r\nb\nc", "mixed", False, ["a", "b", "c"]),
        (b"no newline at all", "none", False, ["no newline at all"]),
        (b"\n\n", "lf", True, ["", ""]),
        (
            b"\x0cform feed stays\x0binside a line\n",
            "lf",
            True,
            ["\x0cform feed stays\x0binside a line"],
        ),
    ],
)
def test_newline_style_and_line_view(
    data: bytes, newline: str, trailing: bool, lines: list[str]
) -> None:
    content = structure_bytes(data)
    assert content["newline"] == newline
    assert content["trailing_newline"] is trailing
    assert content["lines"] == lines
    assert content["line_count"] == len(lines)


def test_the_bom_is_reported_and_kept() -> None:
    content = structure_bytes("\ufeffhello\n".encode())
    assert content["bom"] is True
    assert content["text"].startswith("\ufeff")
    assert content["lines"] == ["\ufeffhello"]
    assert content["character_count"] == 7


def test_non_ascii_text_survives_exactly() -> None:
    text = "reach \u2014 nitrate 4.1 mg/l ünïcode ✓\n"
    content = structure_bytes(text.encode("utf-8"))
    assert content["text"] == text
    assert content["character_count"] == len(text)


# -- checked declarations --------------------------------------------------


def test_a_length_that_disagrees_refuses_mismatched_lengths() -> None:
    result = run(payload(b"hello", byte_length=4))
    assert result.status == "refused"
    assert result.refusal is not None
    assert result.refusal.code is RefusalCode.MISMATCHED_LENGTHS
    assert result.refusal.detail == {"declared": 4, "decoded": 5}


def test_a_digest_that_disagrees_refuses_and_names_both() -> None:
    wrong = "sha256:" + "ab" * 32
    result = run(payload(b"hello", bytes_digest=wrong))
    assert result.status == "refused"
    assert result.refusal is not None
    assert result.refusal.code is RefusalCode.PROVIDER_DECLINED
    assert result.refusal.detail["declared"] == wrong
    assert result.refusal.detail["computed"] == "sha256:" + hashlib.sha256(b"hello").hexdigest()


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"content_encoding": "hex"}, "content_encoding"),
        ({"content_encoding": None}, "content_encoding"),
        ({"bytes": 12}, "bytes"),
        ({"bytes": "not base64!"}, "bytes"),
        ({"bytes": "aGVsbG8=\n"}, "bytes"),
        ({"byte_length": -1}, "byte_length"),
        ({"byte_length": True}, "byte_length"),
        ({"byte_length": "5"}, "byte_length"),
        ({"bytes_digest": "5d41402abc4b2a76b9719d911017c592"}, "bytes_digest"),
        ({"bytes_digest": "SHA256:" + "AB" * 32}, "bytes_digest"),
        ({"commitment_digest": "commitment"}, "commitment_digest"),
        ({"logical_source": ""}, "logical_source"),
        ({"logical_source": "with\x00nul"}, "logical_source"),
        ({"logical_source": 7}, "logical_source"),
    ],
)
def test_a_malformed_field_refuses_invalid_parameter_naming_the_field(
    overrides: dict[str, Any], field: str
) -> None:
    result = run(payload(b"hello", **overrides))
    assert result.status == "refused"
    assert result.refusal is not None
    assert result.refusal.code is RefusalCode.INVALID_PARAMETER
    assert result.refusal.detail["field"] == field


def test_a_missing_field_refuses_and_lists_what_is_missing() -> None:
    document = payload(b"hello")
    del document["bytes_digest"]
    del document["commitment_digest"]
    result = run(document)
    assert result.status == "refused"
    assert result.refusal is not None
    assert result.refusal.code is RefusalCode.INVALID_PARAMETER
    assert result.refusal.detail["missing"] == ["commitment_digest", "bytes_digest"]


def test_an_undeclared_field_refuses_rather_than_being_ignored() -> None:
    result = run(payload(b"hello", host_path="/Users/someone/workspace/readings.md"))
    assert result.status == "refused"
    assert result.refusal is not None
    assert result.refusal.code is RefusalCode.INVALID_PARAMETER
    assert result.refusal.detail["unknown"] == ["host_path"]
    assert result.refusal.detail["declared"] == list(INPUT_FIELDS)


def test_the_shape_is_checked_before_anything_is_decoded() -> None:
    """A malformed declaration refuses on the declaration, not on the payload."""

    result = run(payload(b"hello", bytes="!!!", byte_length=-1))
    assert result.refusal is not None
    assert result.refusal.detail["field"] == "byte_length"


# -- no secrets, no world -------------------------------------------------


def test_a_delivered_secret_never_reaches_the_body() -> None:
    secret = "dummy-credential-c0ffee-do-not-use"
    result = run(payload(b"hello"), secrets={"workspace.unused": secret})
    assert result.status == "ok"
    from cruxible_provider_runtime.canonical import canonical_json

    assert secret.encode() not in canonical_json(result.output)
    assert secret.encode() not in canonical_json(result.metrics)


def test_an_invocation_opens_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Runtime purity: ``open`` and ``os.open`` raise for the duration of a call."""

    import builtins
    import io
    import os

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"the adapter opened something: {args!r}")

    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(io, "open", forbidden)
    monkeypatch.setattr(os, "open", forbidden)
    result = run(payload(b"line one\nline two\n"))
    assert result.status == "ok"
