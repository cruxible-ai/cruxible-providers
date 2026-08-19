"""The invocation protocol envelope: versioning and the additive region."""

from __future__ import annotations

import json

import pytest
from cruxible_provider_runtime.errors import RefusalCode, RefusalError
from cruxible_provider_runtime.protocol import (
    PROTOCOL_VERSION,
    Budgets,
    ProtocolVersion,
    ResultEnvelope,
    RunContext,
    SecretChannelSpec,
    parse_result_envelope,
    parse_run_context,
)

BUDGETS = Budgets(wall_clock_seconds=5.0, output_bytes=1024)


def _context(**overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "protocol_version": PROTOCOL_VERSION.render(),
        "run_id": "run-1",
        "interface_id": "test.slot",
        "interface_digest": "sha256:" + "1" * 64,
        "implementation_digest": "sha256:" + "2" * 64,
        "entrypoint": "pkg.mod:Object",
        "input": {"text": "x"},
        "input_bucket": "size=small",
        "budgets": {"wall_clock_seconds": 5.0, "output_bytes": 1024},
    }
    document.update(overrides)
    return document


def test_round_trip() -> None:
    context = parse_run_context(json.dumps(_context()).encode())
    assert context.run_id == "run-1"
    assert context.additive == {}


def test_unknown_top_level_field_refuses() -> None:
    with pytest.raises(RefusalError) as exc:
        parse_run_context(json.dumps(_context(surprise="value")).encode())
    assert exc.value.code is RefusalCode.UNKNOWN_RUN_CONTEXT_FIELD
    assert exc.value.refusal.detail["fields"] == ["surprise"]


def test_unknown_field_inside_the_additive_region_is_carried_not_refused() -> None:
    context = parse_run_context(json.dumps(_context(additive={"a_future_minor_field": 1})).encode())
    assert context.additive == {"a_future_minor_field": 1}


def test_malformed_version_refuses() -> None:
    with pytest.raises(RefusalError) as exc:
        parse_run_context(json.dumps(_context(protocol_version="one")).encode())
    assert exc.value.code is RefusalCode.UNSUPPORTED_PROTOCOL


def test_non_json_refuses() -> None:
    with pytest.raises(RefusalError) as exc:
        parse_run_context(b"\xff\xfe not json")
    assert exc.value.code is RefusalCode.PROVIDER_PROTOCOL_VIOLATION


def test_protocol_version_parsing() -> None:
    assert ProtocolVersion.parse("2.7") == ProtocolVersion(major=2, minor=7)
    assert PROTOCOL_VERSION.render() == "1.0"


def test_secret_channel_may_not_reuse_a_standard_stream() -> None:
    with pytest.raises(ValueError, match="standard stream"):
        SecretChannelSpec(fd=1)


def test_result_envelope_status_consistency() -> None:
    with pytest.raises(ValueError, match="ok result must carry output"):
        ResultEnvelope(protocol_version="1.0", run_id="r", status="ok")
    with pytest.raises(ValueError, match="refused result must carry a refusal"):
        ResultEnvelope(protocol_version="1.0", run_id="r", status="refused")
    with pytest.raises(ValueError, match="error result must carry an error"):
        ResultEnvelope(protocol_version="1.0", run_id="r", status="error")


def test_only_ok_carries_output() -> None:
    with pytest.raises(ValueError, match="only an ok result"):
        ResultEnvelope(
            protocol_version="1.0",
            run_id="r",
            status="error",
            output={"leaked": True},
            error={"kind": "X", "message": "y"},  # type: ignore[arg-type]
        )


def test_malformed_envelope_refuses() -> None:
    with pytest.raises(RefusalError) as exc:
        parse_result_envelope(b'{"protocol_version": "1.0"}')
    assert exc.value.code is RefusalCode.PROVIDER_PROTOCOL_VIOLATION


def test_run_context_is_frozen() -> None:
    context = RunContext.model_validate(_context())
    with pytest.raises(ValueError, match="frozen"):
        context.run_id = "other"  # type: ignore[misc]


def test_an_executor_side_refusal_renders_in_the_provider_result_shape() -> None:
    """So a caller reads one envelope shape regardless of who refused."""

    from cruxible_provider_runtime.errors import RefusalCode as _Code
    from cruxible_provider_runtime.errors import RefusalError
    from cruxible_provider_runtime.execute import refusal_envelope

    envelope = refusal_envelope(
        "run-9", RefusalError(_Code.BUDGET_WALL_CLOCK, "took too long", seconds=31)
    )
    assert envelope.status == "refused"
    assert envelope.run_id == "run-9"
    assert envelope.refusal is not None
    assert envelope.refusal.code is _Code.BUDGET_WALL_CLOCK
    assert envelope.refusal.detail["seconds"] == 31
    assert envelope.output is None
    assert envelope.protocol_version == PROTOCOL_VERSION.render()
