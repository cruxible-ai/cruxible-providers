"""The shared acquisition result: exact material, and no key for an unstated window."""

from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime, timedelta, timezone

import pytest
from cruxible_provider_runtime.acquisition import ACQUISITION_RESULT_TAG, external_capture_result

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
