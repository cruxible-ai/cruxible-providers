"""Packaged documents, recorded engine responses, and bucket fixtures.

Every claimed bucket needs a conformance fixture that passes, and a fixture for
an engine adapter needs an engine. The base install deliberately has none, so
what ships here is a *replay*: a document that really is in this distribution,
and the output an engine produces for it.

Three things keep replay from becoming fabrication.

**A recorded response is served only for a packaged document.** The run input's
``source.kind`` is either ``inline`` — a document the caller supplied, which only
a real engine ever converts — or ``packaged_fixture``, which can name nothing but
a document shipped inside this distribution. A caller's document can therefore
never be answered from a recording, whatever they put in the input.

**The replay is labelled.** The engine reported in the output is
``recorded:<id>``, never ``docling`` or ``paddleocr``, and the run's trace
carries an event saying no engine ran.

**The recording is checkable, and checked.** Each recorded response carries the
transcription it claims and the engine it claims it for; the engine-marked lane
runs that engine over the same shipped bytes and asserts it reproduces the
recording. A recording that has drifted away from what the engine does fails
there — which is the whole reason the lane exists.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "DOCUMENTS_DIR",
    "FIXTURES_DIR",
    "RECORDINGS_DIR",
    "BucketFixture",
    "PackagedDocument",
    "Recording",
    "load_document",
    "load_fixtures",
    "load_recordings",
]

PACKAGE_ROOT = Path(__file__).resolve().parent
RECORDINGS_DIR = PACKAGE_ROOT / "recordings"
DOCUMENTS_DIR = PACKAGE_ROOT / "documents"
FIXTURES_DIR = PACKAGE_ROOT / "fixtures"


class PackagedDocument(BaseModel):
    """A document shipped inside the distribution, plus its identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    filename: str
    media_type: str
    path: str
    """Path of the bytes, relative to the package's ``documents`` directory."""

    sha256: str

    def read(self) -> bytes:
        data = (DOCUMENTS_DIR / self.path).read_bytes()
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        if digest != self.sha256:
            raise ValueError(
                f"packaged document {self.path!r} does not match its recorded digest; "
                "the recording describes bytes that are no longer there"
            )
        return data


class Recording(BaseModel):
    """One engine response, recorded against one packaged document."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    note: str
    interface_id: str
    engine: str
    """The engine this response is recorded for. The engine lane re-runs it."""

    recorded_from: str
    """How the response was obtained, stated plainly rather than implied."""

    document: PackagedDocument
    output: dict[str, Any]


class BucketFixture(BaseModel):
    """One claimed bucket, and what a run in it must produce."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    note: str
    interface_id: str
    bucket_selector: str
    bucket_id: str
    recording_id: str | None = None
    """``None`` for a bucket the adapter serves with no engine at all."""

    input: dict[str, Any] = Field(default_factory=dict)
    expect: dict[str, Any] = Field(default_factory=dict)


def _load(directory: Path, model: type[Any]) -> dict[str, Any]:
    loaded: dict[str, Any] = {}
    for path in sorted(directory.glob("*.json")):
        document = model.model_validate(json.loads(path.read_text(encoding="utf-8")))
        if document.id in loaded:  # pragma: no cover - defensive
            raise ValueError(f"two files in {directory.name} share the id {document.id!r}")
        loaded[document.id] = document
    return loaded


@lru_cache(maxsize=1)
def load_recordings() -> Mapping[str, Recording]:
    """Every recorded engine response shipped in this distribution, keyed by id."""

    return _load(RECORDINGS_DIR, Recording)


@lru_cache(maxsize=1)
def load_fixtures() -> Mapping[str, BucketFixture]:
    """Every bucket fixture shipped in this distribution, keyed by id."""

    return _load(FIXTURES_DIR, BucketFixture)


def load_document(recording_id: str) -> tuple[Recording, bytes]:
    """The recording and the real bytes of the document it was recorded against."""

    recordings = load_recordings()
    if recording_id not in recordings:
        raise KeyError(recording_id)
    recording = recordings[recording_id]
    return recording, recording.document.read()


def inline_document(content_base64: str) -> bytes:
    """Decode a caller-supplied document, failing loudly on bad base64."""

    return base64.b64decode(content_base64, validate=True)
