"""Construct Core's shared acquisition wire result without minting a Capture."""

from __future__ import annotations

import base64
import hashlib
from datetime import datetime
from typing import Any

ACQUISITION_RESULT_TAG = "playbill-provider-result-to-external-capture-v1"


def external_capture_result(
    content: bytes,
    *,
    source_identity: str,
    coordinate_type: str,
    coordinate: object,
    observed_at: datetime,
    selector_type: str,
    selector: object = None,
) -> dict[str, Any]:
    """Commit the exact material supplied; Core verifies and retains these bytes.

    A Procedure Source consumes a canonical JSON bundle; put non-JSON origin
    bytes inside that bundle (for example as base64), alongside extraction.
    Source identity and coordinate/selector names must match its CaptureContract.

    External observations are attested-only: retaining a response does not make
    a live endpoint replayable. This helper is wire construction, not authority.
    """
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("observed_at must be timezone-aware")
    return {
        "tag": ACQUISITION_RESULT_TAG,
        "source_identity": source_identity,
        "coordinate_type": coordinate_type,
        "coordinate": coordinate,
        "selector_type": selector_type,
        "selector": {} if selector is None else selector,
        "replayability": "attested_only",
        "content_encoding": "base64",
        "content_base64": base64.b64encode(content).decode("ascii"),
        "byte_length": len(content),
        "bytes_digest": "sha256:" + hashlib.sha256(content).hexdigest(),
        "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
        "source_effective_time": None,
    }
