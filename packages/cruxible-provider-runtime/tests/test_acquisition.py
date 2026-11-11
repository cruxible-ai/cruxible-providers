"""The shared Capture wire: exact material out (one or a list), exact material back in."""

from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime, timedelta, timezone

import pytest
from cruxible_provider_runtime.acquisition import (
    ACQUISITION_RESULT_TAG,
    CaptureInputError,
    capture_input,
    capture_observed_at,
    external_capture_result,
    external_capture_results,
)

OBSERVED = datetime(2026, 10, 30, 9, 15, 0, 123456, tzinfo=UTC)


def _result(**overrides: object) -> dict[str, object]:
    arguments: dict[str, object] = {
        "source_identity": "web.response",
        "coordinate_type": "http-response-v1",
        "coordinate": {"request_digest": "sha256:" + "a" * 64, "status": 200},
        "observed_at": OBSERVED,
        "selector_type": "whole-response-v1",
    }
    arguments.update(overrides)
    return external_capture_result(b"\x00exact bytes\xff", **arguments)  # type: ignore[arg-type]


def test_an_unstated_effective_time_is_an_absent_key_never_null() -> None:
    result = _result()
    assert "source_effective_time" not in result
    assert None not in result.values()


def test_the_material_and_its_coordinates_are_carried_exactly() -> None:
    content = b"\x00exact bytes\xff"
    result = _result(selector={"part": "body"})
    assert result == {
        "tag": ACQUISITION_RESULT_TAG,
        "source_identity": "web.response",
        "coordinate_type": "http-response-v1",
        "coordinate": {"request_digest": "sha256:" + "a" * 64, "status": 200},
        "selector_type": "whole-response-v1",
        "selector": {"part": "body"},
        "replayability": "attested_only",
        "content_encoding": "base64",
        "content_base64": base64.b64encode(content).decode("ascii"),
        "byte_length": len(content),
        "bytes_digest": "sha256:" + hashlib.sha256(content).hexdigest(),
        "observed_at": "2026-10-30T09:15:00.123456Z",
    }


def test_no_selector_is_the_empty_selector() -> None:
    assert _result()["selector"] == {}


def test_observed_at_is_utc_z_or_its_stated_offset() -> None:
    assert _result()["observed_at"] == "2026-10-30T09:15:00.123456Z"
    offset = OBSERVED.astimezone(timezone(timedelta(hours=2)))
    assert _result(observed_at=offset)["observed_at"] == "2026-10-30T11:15:00.123456+02:00"


def test_a_naive_observation_time_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        _result(observed_at=OBSERVED.replace(tzinfo=None))


# -- list outputs ---------------------------------------------------------------------


def _members(*names: str) -> list[tuple[bytes, object]]:
    return [(f"document {name}".encode(), {"file": name}) for name in names]


def test_each_list_member_is_its_own_capture_wire_in_order() -> None:
    results = external_capture_results(
        _members("a", "b", "c"),
        source_identity="noop.recorded",
        coordinate_type="noop-recording-v1",
        observed_at=OBSERVED,
        selector_type="whole-file-v1",
    )
    assert [item["coordinate"] for item in results] == [{"file": "a"}, {"file": "b"}, {"file": "c"}]
    for (content, _coordinate), item in zip(_members("a", "b", "c"), results, strict=True):
        assert base64.b64decode(item["content_base64"]) == content
        assert item["bytes_digest"] == "sha256:" + hashlib.sha256(content).hexdigest()
        assert item["tag"] == ACQUISITION_RESULT_TAG


def test_an_empty_list_is_an_empty_list() -> None:
    assert (
        external_capture_results(
            [],
            source_identity="noop.recorded",
            coordinate_type="noop-recording-v1",
            observed_at=OBSERVED,
            selector_type="whole-file-v1",
        )
        == []
    )


def test_two_members_with_one_coordinate_are_refused() -> None:
    with pytest.raises(ValueError, match="repeats an earlier member's coordinate"):
        external_capture_results(
            [(b"one", {"file": "a"}), (b"two", {"file": "a"})],
            source_identity="noop.recorded",
            coordinate_type="noop-recording-v1",
            observed_at=OBSERVED,
            selector_type="whole-file-v1",
        )


# -- capture inputs -------------------------------------------------------------------


def test_a_capture_input_reads_back_the_exact_bytes_and_coordinate() -> None:
    wire = _result()
    read = capture_input(wire)
    assert read == (
        b"\x00exact bytes\xff",
        {"request_digest": "sha256:" + "a" * 64, "status": 200},
        "http-response-v1",
    )
    content, coordinate, coordinate_type = read
    assert (read.content, read.coordinate, read.coordinate_type) == (
        content,
        coordinate,
        coordinate_type,
    )
    assert capture_observed_at(wire) == OBSERVED


def test_core_s_wire_without_the_default_keys_reads_the_same() -> None:
    """Core may leave the defaulted tag and encoding out; the bytes still verify."""

    wire = {
        key: value for key, value in _result().items() if key not in {"tag", "content_encoding"}
    }
    assert capture_input(wire).content == b"\x00exact bytes\xff"


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"content_base64": "not base64!"}, "not base64"),
        ({"content_base64": "AA"}, "not base64"),
        ({"byte_length": 3}, "byte_length"),
        ({"byte_length": True}, "byte_length"),
        ({"bytes_digest": "sha256:" + "0" * 64}, "bytes_digest"),
        ({"content_encoding": "utf-8"}, "base64 transport"),
        ({"tag": "another-wire"}, "wire tag"),
        ({"coordinate_type": ""}, "coordinate type"),
    ],
    ids=["base64", "padding", "length", "bool-length", "digest", "encoding", "tag", "type"],
)
def test_a_capture_input_that_does_not_verify_is_refused(
    change: dict[str, object], problem: str
) -> None:
    with pytest.raises(CaptureInputError, match=problem):
        capture_input({**_result(), **change})


def test_a_capture_input_is_a_record_with_content_and_a_coordinate() -> None:
    with pytest.raises(CaptureInputError):
        capture_input("sha256:" + "a" * 64)
    wire = _result()
    del wire["coordinate"]
    with pytest.raises(CaptureInputError, match="no coordinate"):
        capture_input(wire)
    with pytest.raises(CaptureInputError, match="no observation time"):
        capture_observed_at({**_result(), "observed_at": None})
    with pytest.raises(CaptureInputError, match="no offset"):
        capture_observed_at({**_result(), "observed_at": "2026-10-30T09:15:00"})
