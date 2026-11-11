"""``noop.captures`` -- the reference list-of-Captures operation.

One call, N documents, N Captures (D-R37 a). The operation reads the recorded
files the run names from this distribution's ``recordings/`` directory and
returns each one, verbatim, as its own member of the ``captures`` out field
(``list<capture<*>>``): one document per Capture, never one payload split up,
and no member wrapping another. How each was obtained is its coordinate (the
file it is, and a request digest identifying that request), never inside the
bytes, and a member is identified by its coordinate, never by its index.

It is the smallest operation that exercises the list form, so a plane package
returning several documents from one call copies this shape: build each
member's ``(content, coordinate)`` and hand them to
``cruxible_provider_runtime.acquisition.external_capture_results``.

It contacts nothing and reads no clock -- the files are part of the
distribution and their observation time is fixed -- so it is ``effect: pure``
and two runs give byte-identical output.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cruxible_provider_runtime.acquisition import external_capture_results
from cruxible_provider_runtime.provider_api import ProviderResult, ProviderRunContext

__all__ = [
    "COORDINATE_TYPE",
    "MAX_FILES",
    "OPERATION",
    "PROVIDER_DECLINED",
    "RECORDED_AT",
    "RECORDINGS_DIR",
    "SELECTOR_TYPE",
    "SOURCE_IDENTITY",
    "NoopCaptures",
    "recorded_files",
]

OPERATION = "noop.captures"
PROVIDER_DECLINED = "provider_declined"
"""The refusal ``noop.captures`` declares (registration.json)."""

RECORDINGS_DIR = Path(__file__).resolve().parent / "recordings"
RECORDED_AT = datetime(2026, 11, 4, tzinfo=UTC)
"""When the packaged files were recorded: their observation time, on every run."""
SOURCE_IDENTITY = "noop.recorded"
COORDINATE_TYPE = "noop-recording-v1"
SELECTOR_TYPE = "whole-file-v1"
MAX_FILES = 16
"""The out Contract's ``max_items``: how many Captures one call may return."""


def recorded_files() -> dict[str, bytes]:
    """Every recorded file this distribution ships, by name."""

    return {path.name: path.read_bytes() for path in sorted(RECORDINGS_DIR.iterdir())}


def _coordinate(name: str) -> dict[str, Any]:
    return {
        "file": name,
        "request_digest": "sha256:" + hashlib.sha256(name.encode("utf-8")).hexdigest(),
    }


class NoopCaptures:
    """Returns each recorded file the run names as its own Capture."""

    operations = (OPERATION,)

    def __call__(self, context: ProviderRunContext) -> ProviderResult:
        files = context.input.get("files")
        known = recorded_files()
        if (
            not isinstance(files, list)
            or len(files) > MAX_FILES
            or not all(isinstance(name, str) for name in files)
        ):
            return ProviderResult.refused(
                PROVIDER_DECLINED, f"files must be a list of at most {MAX_FILES} file names"
            )
        for name in files:
            if name not in known:
                return ProviderResult.refused(
                    PROVIDER_DECLINED,
                    f"no recorded file is named {name!r}",
                    file=name,
                    known=sorted(known),
                )
        if len(set(files)) != len(files):
            # One request per member: the same file twice is one request named twice.
            return ProviderResult.refused(
                PROVIDER_DECLINED, "a file is named twice; each Capture is one request"
            )
        captures = external_capture_results(
            [(known[name], _coordinate(name)) for name in files],
            source_identity=SOURCE_IDENTITY,
            coordinate_type=COORDINATE_TYPE,
            observed_at=RECORDED_AT,
            selector_type=SELECTOR_TYPE,
        )
        return ProviderResult.ok(
            {"captures": captures}, metrics={"capture_count": float(len(captures))}
        )
