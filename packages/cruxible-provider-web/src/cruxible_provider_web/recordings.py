"""Recorded exchanges and bucket fixtures, shipped inside the distribution.

Every claimed input bucket needs a conformance fixture that passes, and a fixture
for a network adapter has to come from somewhere. These recordings are that
somewhere: response material captured once, replayed byte for byte.

Two rules keep replay from becoming fabrication.

**A recording is only ever served for a reserved host.** Every recorded request
targets ``fixture.invalid``. ``.invalid`` is reserved by RFC 2606 and can never
resolve, so no production URL can select a recording, and no recording can stand
in for a resource a caller actually asked for. The URL is the discriminator, not
a flag some caller could set.

**The replay is labelled.** A run served from a recording says so, in the output
(``retrieved.source``) and in an event on its trace. A Capture built from it
carries the fact that no origin was contacted, rather than looking like a fetch.

The recordings travel *through the real client*: they are served by an
``httpx.MockTransport``, so the request pipeline, the egress event hook, the
header handling, and the size cap all execute exactly as they do against a
network. What is replaced is the socket, and nothing above it.

Fixtures and recordings are separate files on purpose. A fixture is a *claim
about a bucket* — this input, admitted into that bucket, produces this shape;
a recording is an *exchange*. Several fixtures may exercise several buckets over
one exchange, which is exactly what the three search fixtures do, and collapsing
the two would have forced three copies of one instance answer to make the ids
line up.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "FIXTURES_DIR",
    "FIXTURE_HOST",
    "RECORDINGS_DIR",
    "BucketFixture",
    "RecordedResponse",
    "Recording",
    "is_fixture_url",
    "load_fixtures",
    "load_recordings",
    "recording_for",
]

PACKAGE_ROOT = Path(__file__).resolve().parent
RECORDINGS_DIR = PACKAGE_ROOT / "recordings"
FIXTURES_DIR = PACKAGE_ROOT / "fixtures"
FIXTURE_HOST = "fixture.invalid"


class RecordedResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status_code: int
    headers: dict[str, str]
    body: str
    """The response body as text. Recordings are text resources by construction."""


class Recording(BaseModel):
    """One recorded exchange."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    note: str
    request_url: str
    """``scheme://host/path`` of the recorded request, query string excluded.

    Matching ignores the query because a search adapter builds its query string
    from the run input, and a recording keyed on an exact parameter ordering
    would break the first time a parameter was added — failing on the
    recording's spelling rather than on the adapter's behaviour.
    """

    response: RecordedResponse
    rendered_body: str | None = None
    """The DOM after client-side assembly, for the js_rendered bucket.

    Recorded separately from ``response.body`` because that is exactly what a
    browser adds: the initial response and the assembled document are two
    artefacts, and a fixture that conflated them could not tell a renderer that
    worked from one that was never called.
    """


class BucketFixture(BaseModel):
    """One claimed bucket, and what a run in it must produce."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    note: str
    interface_id: str
    bucket_selector: str
    bucket_id: str
    """The full bucket the input must classify into, spelled out.

    Not derived from the selector: a selector may wildcard, and the point of the
    fixture is that *this* input measures into *that* bucket. Writing it down is
    what makes the classifier's behaviour a fixed, reviewable claim.
    """

    recording_id: str
    input: dict[str, Any] = Field(default_factory=dict)
    coordinates: dict[str, Any] = Field(default_factory=dict)
    secrets: dict[str, str] = Field(default_factory=dict)
    expect: dict[str, Any] = Field(default_factory=dict)


def _load(directory: Path, model: type[BaseModel]) -> dict[str, Any]:
    loaded: dict[str, Any] = {}
    for path in sorted(directory.glob("*.json")):
        document = model.model_validate(json.loads(path.read_text(encoding="utf-8")))
        identifier = str(document.id)  # type: ignore[attr-defined]
        if identifier in loaded:  # pragma: no cover - defensive
            raise ValueError(f"two files in {directory.name} share the id {identifier!r}")
        loaded[identifier] = document
    return loaded


@lru_cache(maxsize=1)
def load_recordings() -> Mapping[str, Recording]:
    """Every recorded exchange shipped in this distribution, keyed by id."""

    recordings: dict[str, Recording] = _load(RECORDINGS_DIR, Recording)
    addressed = [_addressed(recording.request_url) for recording in recordings.values()]
    if len(set(addressed)) != len(addressed):  # pragma: no cover - defensive
        raise ValueError(
            "two recordings address the same request; the replay transport would have to "
            "guess which one a request meant"
        )
    return recordings


@lru_cache(maxsize=1)
def load_fixtures() -> Mapping[str, BucketFixture]:
    """Every bucket fixture shipped in this distribution, keyed by id."""

    return _load(FIXTURES_DIR, BucketFixture)


def is_fixture_url(url: str) -> bool:
    """Whether ``url`` names the reserved recording host."""

    return (urlsplit(url).hostname or "").lower() == FIXTURE_HOST


def _addressed(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{(parts.hostname or '').lower()}{parts.path or '/'}"


def recording_for(url: str) -> Recording | None:
    """The recording whose recorded request addresses ``url``, if any."""

    target = _addressed(url)
    for recording in load_recordings().values():
        if _addressed(recording.request_url) == target:
            return recording
    return None
