"""Capture conformance: every Capture this plane produces is a payload, verbatim.

The rule (D-R25): a Capture's bytes are the document an operation returns --
the body an origin sent, or the derived document a render made -- and how it
was obtained is the typed coordinate, never inside the bytes. Held here against
the recorded exchanges this distribution ships, for every operation whose result
Contract is Capture wire:

* the stored bytes are the origin bytes (digest equality with the recording);
* no wrapper keys (``retrieved``, ``derived``, ``body_base64``, ``exchanges``)
  appear in them;
* the coordinate carries the exchange's metadata (and, for a derived document,
  the content digest of the Capture it was made from).

A new Capture-producing operation without a case below fails
``test_every_capture_producing_operation_has_a_case``.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
from collections.abc import Callable
from typing import Any

import pytest
from cruxible_provider_runtime.registration import load_registration
from cruxible_provider_web import PACKAGE_ROOT, contract_members
from cruxible_provider_web.fetch import WebFetch
from cruxible_provider_web.recordings import Recording, load_recordings
from cruxible_provider_web.render import WebRender
from cruxible_provider_web.search import SearxngSearch

from .test_web_secrets import _context

WRAPPER_KEYS = frozenset({"retrieved", "derived", "body_base64", "exchanges"})


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _capture_operations() -> list[str]:
    """Every operation whose out Contract is Capture wire (it carries the bytes)."""

    shipped = {
        (document["name"], document["version"]): document
        for document in (json.loads(body) for body in contract_members().values())
    }
    found = []
    for operation in load_registration(PACKAGE_ROOT).operations:
        out = shipped[(operation.out.name, operation.out.version)]
        if "content_base64" in out["fields"]:
            found.append(operation.name)
    return sorted(found)


def _stored(result: Any) -> bytes:
    assert result.status == "ok", result
    output = result.output
    stored = base64.b64decode(output["content_base64"], validate=True)
    assert output["byte_length"] == len(stored)
    assert output["bytes_digest"] == _digest(stored)
    return stored


def _no_wrapper(stored: bytes) -> None:
    try:
        document = json.loads(stored)
    except ValueError:
        return
    if isinstance(document, dict):
        assert not WRAPPER_KEYS & set(document), sorted(WRAPPER_KEYS & set(document))


def _fetched(recording: Recording) -> Any:
    return WebFetch()(_context("web.fetch", input={"url": recording.request_url}))


def _fetch_case(recording: Recording) -> None:
    result = _fetched(recording)
    stored = _stored(result)
    origin = recording.response.body.encode("utf-8")
    assert _digest(stored) == _digest(origin)
    _no_wrapper(stored)
    coordinate = result.output["coordinate"]
    assert coordinate["final_url"] == recording.request_url
    assert coordinate["status"] == recording.response.status_code
    assert coordinate["content_type"] == recording.response.headers.get("content-type", "")
    assert coordinate["source"] == "packaged-recording"
    assert "derived_from" not in coordinate


def _search_case(recording: Recording) -> None:
    result = SearxngSearch()(
        _context(
            "search.web",
            input={"query": "tide gauge"},
            coordinates={"instance_url": "https://fixture.invalid"},
            declared_endpoints=("https://fixture.invalid",),
        )
    )
    stored = _stored(result)
    assert _digest(stored) == _digest(recording.response.body.encode("utf-8"))
    _no_wrapper(stored)
    assert result.output["coordinate"]["source"] == "packaged-recording"


def _render_case(recording: Recording, form: str) -> None:
    fetched = _fetched(recording)
    source = _stored(fetched)
    result = WebRender()(
        _context(
            "web.render",
            input={
                "form": form,
                "source": {
                    "content_base64": fetched.output["content_base64"],
                    "coordinate": fetched.output["coordinate"],
                },
            },
        )
    )
    stored = _stored(result)
    if form == "dom":
        # The recorded post-assembly DOM is the origin of a recorded render.
        assert recording.rendered_body is not None
        assert _digest(stored) == _digest(recording.rendered_body.encode("utf-8"))
    _no_wrapper(stored)
    coordinate = result.output["coordinate"]
    assert coordinate["derived_from"] == _digest(source)
    assert coordinate["renderer"]
    # The fetched Capture's bytes never carry the render, and the render's
    # bytes never carry the fetched Capture.
    assert stored != source


def _cases() -> dict[str, list[tuple[str, Callable[[], None]]]]:
    recordings = load_recordings()
    fetchable = [item for item in recordings.values() if item.id != "searxng-answer"]
    rendered = [item for item in fetchable if item.rendered_body is not None]
    # A text render reads the main content of a document an origin sent whole;
    # a client-assembled page carries none until it is rendered.
    html = [
        item
        for item in fetchable
        if item.response.headers.get("content-type", "").startswith("text/html")
        and item.rendered_body is None
    ]
    cases: dict[str, list[tuple[str, Callable[[], None]]]] = {
        "web.fetch": [(item.id, lambda item=item: _fetch_case(item)) for item in fetchable],
        "search.web": [("searxng-answer", lambda: _search_case(recordings["searxng-answer"]))],
        "web.render": [
            (f"dom:{item.id}", lambda item=item: _render_case(item, "dom")) for item in rendered
        ],
    }
    if importlib.util.find_spec("trafilatura") is not None:
        cases["web.render"] += [
            (f"text:{item.id}", lambda item=item: _render_case(item, "text")) for item in html
        ]
    return cases


CASES = _cases()


def test_every_capture_producing_operation_has_a_case() -> None:
    assert _capture_operations() == sorted(CASES)
    assert all(CASES[name] for name in CASES)


@pytest.mark.parametrize(
    "case",
    [check for name in sorted(CASES) for _id, check in CASES[name]],
    ids=[f"{name}:{case_id}" for name in sorted(CASES) for case_id, _check in CASES[name]],
)
def test_the_stored_bytes_are_the_origin_bytes(case: Callable[[], None]) -> None:
    case()
