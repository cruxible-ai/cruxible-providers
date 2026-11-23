"""``CaptureView`` reads core's Capture wire; ``derived_capture`` writes it for a pure output."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from cruxible_provider_runtime import CaptureInputError, CaptureView, derived_capture
from cruxible_provider_runtime.acquisition import ACQUISITION_RESULT_TAG, external_capture_result
from cruxible_provider_runtime.capture_view import (
    DERIVED_COORDINATE_TYPE,
    DERIVED_SELECTOR_TYPE,
    DERIVED_SOURCE_IDENTITY,
    capture_body,
)

OBSERVED = datetime(2026, 11, 4, 8, 30, tzinfo=UTC)
COORDINATE = {"url": "https://example.test/a", "status": 200, "headers": ["a", "b"]}


def wire(content: bytes = b'{"findings": [1, 2, 3]}', **overrides: Any) -> dict[str, Any]:
    document = external_capture_result(
        content,
        source_identity="web.response",
        coordinate_type="http-response-v1",
        coordinate=copy.deepcopy(COORDINATE),
        observed_at=OBSERVED,
        selector_type="whole-response-v1",
    )
    document.update(overrides)
    return document


def core_wire(content: bytes = b"core bytes") -> dict[str, Any]:
    """The wire as core dumps it for a derive: every field, an effective time stated."""

    return {
        "tag": ACQUISITION_RESULT_TAG,
        "source_identity": "web.response",
        "coordinate_type": "http-response-v1",
        "coordinate": {"url": "https://example.test/b"},
        "selector_type": "whole-response-v1",
        "selector": {"part": "body"},
        "replayability": "exact",
        "content_encoding": "base64",
        "content_base64": base64.b64encode(content).decode("ascii"),
        "byte_length": len(content),
        "bytes_digest": "sha256:" + hashlib.sha256(content).hexdigest(),
        "observed_at": "2026-11-04T08:30:00Z",
        "source_effective_time": {
            "tag": "playbill-source-effective-time-v1",
            "effective_from": "2026-11-01T00:00:00Z",
        },
    }


# -- reading ---------------------------------------------------------------------------------


def test_a_view_exposes_the_bytes_and_the_metadata() -> None:
    view = CaptureView(wire())
    assert view.raw_bytes == b'{"findings": [1, 2, 3]}'
    assert view.text == '{"findings": [1, 2, 3]}'
    assert view.json() == {"findings": [1, 2, 3]}
    assert view.source_identity == "web.response"
    assert view.coordinate_type == "http-response-v1"
    assert view.coordinate == COORDINATE
    assert view.selector_type == "whole-response-v1"
    assert view.selector == {}
    assert view.replayability == "attested_only"
    assert view.byte_length == len(view.raw_bytes)
    assert view.bytes_digest == "sha256:" + hashlib.sha256(view.raw_bytes).hexdigest()
    assert view.observed_at == OBSERVED
    assert view.observed_at.utcoffset() == timedelta(0)
    assert view.source_effective_time is None
    assert view.raw == wire()


def test_a_view_reads_the_wire_core_dumps() -> None:
    view = CaptureView(core_wire())
    assert view.raw_bytes == b"core bytes"
    assert view.replayability == "exact"
    assert view.selector == {"part": "body"}
    assert view.source_effective_time == {
        "tag": "playbill-source-effective-time-v1",
        "effective_from": "2026-11-01T00:00:00Z",
    }


def test_a_view_is_read_only_and_holds_its_own_copy() -> None:
    source = wire()
    view = CaptureView(source)
    source["coordinate"]["url"] = "https://elsewhere.test/"
    source["content_base64"] = ""
    assert view.coordinate["url"] == "https://example.test/a"
    assert view.raw["content_base64"] != ""
    with pytest.raises(AttributeError, match="read-only"):
        view.source_identity = "other"  # type: ignore[misc]
    with pytest.raises(AttributeError, match="read-only"):
        del view.coordinate
    view.coordinate["url"] = "x"
    view.coordinate["headers"].append("c")
    assert view.coordinate == COORDINATE
    with pytest.raises(TypeError):
        view.raw["byte_length"] = 0  # type: ignore[index]
    view.raw["coordinate"]["url"] = "x"
    assert view.raw == wire()


def test_wire_returns_a_plain_mutable_copy_equal_to_what_arrived() -> None:
    view = CaptureView(wire())
    copied = view.wire()
    assert copied == wire()
    assert isinstance(copied["coordinate"]["headers"], list)
    copied["coordinate"]["url"] = "changed"
    assert view.coordinate["url"] == "https://example.test/a"
    assert CaptureView(view.wire()) == view
    assert CaptureView(wire(b"other")) != view


def test_text_and_json_fail_loudly_on_bytes_they_cannot_read() -> None:
    binary = CaptureView(wire(b"\xff\xfe\x00"))
    assert binary.raw_bytes == b"\xff\xfe\x00"
    with pytest.raises(CaptureInputError, match="not UTF-8"):
        _ = binary.text
    with pytest.raises(CaptureInputError, match="not one JSON document"):
        CaptureView(wire(b"<html></html>")).json()


def test_an_empty_capture_is_still_a_capture() -> None:
    view = CaptureView(wire(b""))
    assert (view.raw_bytes, view.text, view.byte_length) == (b"", "", 0)


REQUIRED = (
    "source_identity",
    "coordinate_type",
    "coordinate",
    "selector_type",
    "selector",
    "replayability",
    "content_base64",
    "byte_length",
    "bytes_digest",
    "observed_at",
)


@pytest.mark.parametrize("field", REQUIRED)
def test_a_wire_missing_a_required_field_is_refused(field: str) -> None:
    document = wire()
    del document[field]
    with pytest.raises(CaptureInputError):
        CaptureView(document)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"content_base64": "not base64!"}, "not base64"),
        ({"content_base64": "YQ"}, "not base64"),
        ({"byte_length": 3}, "byte_length"),
        ({"byte_length": True}, "byte_length"),
        ({"bytes_digest": "sha256:" + "0" * 64}, "bytes_digest"),
        ({"tag": "some-other-wire-v1"}, "unknown wire tag"),
        ({"content_encoding": "hex"}, "base64 transport"),
        ({"replayability": "maybe"}, "replayability"),
        ({"source_identity": "Web Response"}, "source_identity"),
        ({"coordinate_type": ""}, "coordinate type"),
        ({"selector_type": "Whole"}, "selector_type"),
        ({"observed_at": "2026-11-04T08:30:00"}, "no offset"),
        ({"observed_at": "yesterday"}, "does not parse"),
        ({"source_effective_time": "2026"}, "not an interval"),
        ({"source_effective_time": {"effective_from": "2026-11-01"}}, "no offset"),
    ],
)
def test_a_malformed_wire_is_refused(overrides: dict[str, Any], message: str) -> None:
    with pytest.raises(CaptureInputError, match=message):
        CaptureView(wire(**overrides))


def test_a_non_canonical_base64_spelling_is_refused() -> None:
    document = wire(b"ab")
    assert document["content_base64"] == "YWI="
    document["content_base64"] = "YWJ="  # decodes to the same bytes, spelled otherwise
    with pytest.raises(CaptureInputError, match="canonical base64"):
        CaptureView(document)


def test_a_non_mapping_is_refused() -> None:
    with pytest.raises(CaptureInputError, match="a record"):
        CaptureView(["not", "a", "wire"])  # type: ignore[arg-type]


# -- writing ---------------------------------------------------------------------------------


def _check_wire(document: dict[str, Any], content: bytes) -> None:
    assert document["content_base64"] == base64.b64encode(content).decode("ascii")
    assert base64.b64decode(document["content_base64"]) == content
    assert document["byte_length"] == len(content)
    assert document["bytes_digest"] == "sha256:" + hashlib.sha256(content).hexdigest()
    assert document["tag"] == ACQUISITION_RESULT_TAG
    assert document["content_encoding"] == "base64"
    assert CaptureView(document).raw_bytes == content


def test_derived_bytes_are_committed_verbatim() -> None:
    content = b"\x00\x01binary\xff"
    document = derived_capture(content, observed_at=OBSERVED)
    _check_wire(document, content)
    assert document == {
        "tag": ACQUISITION_RESULT_TAG,
        "source_identity": DERIVED_SOURCE_IDENTITY,
        "coordinate_type": DERIVED_COORDINATE_TYPE,
        "coordinate": {},
        "selector_type": DERIVED_SELECTOR_TYPE,
        "selector": {},
        "replayability": "attested_only",
        "content_encoding": "base64",
        "content_base64": base64.b64encode(content).decode("ascii"),
        "byte_length": len(content),
        "bytes_digest": "sha256:" + hashlib.sha256(content).hexdigest(),
        "observed_at": "2026-11-04T08:30:00Z",
    }
    _check_wire(derived_capture(bytearray(content), observed_at=OBSERVED), content)
    _check_wire(derived_capture(memoryview(content), observed_at=OBSERVED), content)


def test_derived_text_is_utf8() -> None:
    _check_wire(derived_capture("café ✓", observed_at=OBSERVED), "café ✓".encode())


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"b": 1, "a": [True, None, "é"]}, '{"a":[true,null,"é"],"b":1}'.encode()),
        ([3, 2.5, {"z": 0}], b'[3,2.5,{"z":0}]'),
        (7, b"7"),
    ],
)
def test_a_derived_json_value_is_canonical_json(body: object, expected: bytes) -> None:
    assert capture_body(body) == expected
    _check_wire(derived_capture(body, observed_at=OBSERVED), expected)


def test_a_body_that_is_not_json_is_refused() -> None:
    with pytest.raises(ValueError, match="bytes, str or one JSON value"):
        derived_capture({"when": OBSERVED}, observed_at=OBSERVED)
    with pytest.raises(ValueError, match="bytes, str or one JSON value"):
        derived_capture(float("nan"), observed_at=OBSERVED)


def test_a_derived_capture_never_reads_the_clock() -> None:
    with pytest.raises(ValueError, match="never reads the clock"):
        derived_capture(b"x")
    with pytest.raises(ValueError, match="timezone-aware"):
        derived_capture(b"x", observed_at=datetime(2026, 11, 4))


def test_derive_takes_its_source_observation_time() -> None:
    source = CaptureView(wire())
    document = source.derive({"n": 1}, coordinate={"derived_from": source.bytes_digest})
    assert document["observed_at"] == "2026-11-04T08:30:00Z"
    assert document["coordinate"] == {"derived_from": source.bytes_digest}
    assert document == derived_capture(
        {"n": 1}, derived_from=source, coordinate={"derived_from": source.bytes_digest}
    )


def test_derived_from_several_captures_takes_the_latest_observation() -> None:
    earlier = CaptureView(wire(b"one"))
    later = CaptureView(
        wire(b"two", observed_at="2026-11-05T01:00:00+02:00")  # 2026-11-04T23:00Z
    )
    document = derived_capture(b"joined", derived_from=[earlier, later])
    assert datetime.fromisoformat(document["observed_at"]) == datetime(
        2026, 11, 4, 23, 0, tzinfo=UTC
    )
    stated = datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=-5)))
    assert derived_capture(b"x", observed_at=stated, derived_from=[later])["observed_at"] == (
        "2026-01-01T00:00:00-05:00"
    )


def test_derived_from_names_views() -> None:
    with pytest.raises(TypeError, match="CaptureViews"):
        derived_capture(b"x", derived_from=[wire()])  # type: ignore[list-item]


@pytest.mark.parametrize(
    "labels",
    [
        {"source_identity": "Not Logical"},
        {"coordinate_type": "has space"},
        {"selector_type": ""},
    ],
)
def test_derived_source_labels_are_canonical_identifiers(labels: dict[str, str]) -> None:
    with pytest.raises(ValueError, match=r"identifier|logical name"):
        derived_capture(b"x", observed_at=OBSERVED, **labels)


def test_a_derived_capture_with_its_kinds_own_labels() -> None:
    document = derived_capture(
        "# rendered",
        observed_at=OBSERVED,
        source_identity="web.rendered",
        coordinate_type="http-response-v1",
        selector_type="whole-response-v1",
        coordinate={"url": "https://example.test/a"},
        selector={"part": "main"},
    )
    view = CaptureView(document)
    assert (view.source_identity, view.coordinate_type, view.selector_type) == (
        "web.rendered",
        "http-response-v1",
        "whole-response-v1",
    )
    assert view.selector == {"part": "main"}
    assert json.loads(json.dumps(document)) == document
