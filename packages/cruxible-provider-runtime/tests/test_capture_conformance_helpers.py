"""The Capture-conformance helpers every plane package's suite reads outputs with."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from cruxible_provider_runtime.acquisition import (
    external_capture_result,
    external_capture_results,
)
from cruxible_provider_runtime.testing import assert_payload_verbatim, captured_members

OBSERVED = datetime(2026, 11, 4, tzinfo=UTC)
KINDS = {"source_identity": "test.source", "coordinate_type": "test-v1", "selector_type": "all-v1"}
ONE = {"fields": {"capture": {"type": {"kind": "capture", "capture_kind": "*"}}}}
#: An out Contract that spells the wire as its own fields: no Capture field, no Capture.
SPELLED = {"fields": {"content_base64": {"type": {"kind": "string"}}}}
LISTED = {
    "fields": {
        "captures": {
            "type": {
                "kind": "list",
                "max_items": 4,
                "item": {"kind": "capture", "capture_kind": "*"},
            }
        },
        "count": {"type": {"kind": "int"}},
    }
}


def _wire(content: bytes, coordinate: object) -> dict[str, object]:
    return external_capture_result(content, coordinate=coordinate, observed_at=OBSERVED, **KINDS)


def test_a_capture_field_is_one_capture() -> None:
    (member,) = captured_members({"capture": _wire(b"body", {"n": 1})}, ONE)
    assert (member.at, member.stored, member.coordinate) == ("capture", b"body", {"n": 1})


def test_a_capture_is_never_the_whole_output() -> None:
    assert captured_members(_wire(b"body", {"n": 1}), SPELLED) == []


def test_a_list_field_is_one_capture_per_member_in_order() -> None:
    output = {
        "captures": external_capture_results(
            [(b"one", {"n": 1}), (b"two", {"n": 2})], observed_at=OBSERVED, **KINDS
        ),
        "count": 2,
    }
    members = captured_members(output, LISTED)
    assert [(item.at, item.stored) for item in members] == [
        ("captures/0", b"one"),
        ("captures/1", b"two"),
    ]
    assert_payload_verbatim(members)


def test_a_wrapped_payload_or_a_shared_coordinate_fails() -> None:
    wrapped = json.dumps({"retrieved": {"body_base64": "eA=="}}).encode()
    with pytest.raises(AssertionError, match="wrapper keys"):
        assert_payload_verbatim(captured_members({"capture": _wire(wrapped, {"n": 1})}, ONE))
    shared = {"captures": [_wire(b"one", {"n": 1}), _wire(b"two", {"n": 1})], "count": 2}
    with pytest.raises(AssertionError, match="share one coordinate"):
        assert_payload_verbatim(captured_members(shared, LISTED))
