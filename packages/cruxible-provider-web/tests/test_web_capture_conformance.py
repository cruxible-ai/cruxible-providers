"""Capture conformance: every Capture this plane produces is a payload, verbatim.

The rule (D-R25): a Capture's bytes are the document an operation returns --
the body an origin sent, the DOM a browser assembled, or the derived document a
derive made -- and how it was obtained is the typed coordinate, never inside the
bytes. Held here against the recorded exchanges this distribution ships, for
every operation whose result carries Captures, in either form Core reads
(``cruxible_provider_runtime.testing.captured_members``): a ``capture<K>``
field, or each member of a ``list<capture<K>>`` field:

* each member's stored bytes are the origin bytes (digest equality with the
  recording);
* no wrapper keys (``retrieved``, ``derived``, ``body_base64``, ``exchanges``)
  appear in them;
* each member carries its own coordinate, with the exchange's metadata (and,
  for a derived document, the content digest of the Capture it was made from).

A new Capture-producing operation without a case below fails
``test_every_capture_producing_operation_has_a_case``. The list form's
reference operation is ``noop.captures`` (``cruxible-provider-noop``), held by
the same helpers there.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from collections.abc import Callable, Mapping
from typing import Any

import pytest
from cruxible_provider_runtime.registration import load_registration
from cruxible_provider_runtime.testing import (
    CapturedMember,
    assert_payload_verbatim,
    captured_members,
)
from cruxible_provider_web import PACKAGE_ROOT
from cruxible_provider_web.dom import WebRenderDom
from cruxible_provider_web.fetch import WebFetch
from cruxible_provider_web.pages import WebFetchPages
from cruxible_provider_web.recordings import Recording, load_recordings
from cruxible_provider_web.render import WebRender, document_json
from cruxible_provider_web.search import SearxngSearch

from .test_web_secrets import _context

SHIPPED = {
    (item.name, item.version): item.model_dump(mode="json")
    for item in load_registration(PACKAGE_ROOT).declares.contracts
}


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _carries_captures(out: Mapping[str, Any]) -> bool:
    fields: Mapping[str, Any] = out["fields"]
    return any(
        declared["type"]["kind"] == "capture"
        or (declared["type"]["kind"] == "list" and declared["type"]["item"]["kind"] == "capture")
        for declared in fields.values()
    )


def _capture_operations() -> list[str]:
    """Every operation whose result carries Captures (in a field, or in a list)."""

    return sorted(
        operation.name
        for operation in load_registration(PACKAGE_ROOT).operations
        if _carries_captures(SHIPPED[(operation.out.name, operation.out.version)])
    )


def _members(operation: str, result: Any) -> list[CapturedMember]:
    assert result.status == "ok", result
    declared = load_registration(PACKAGE_ROOT).operation(operation).out
    members = captured_members(result.output, SHIPPED[(declared.name, declared.version)])
    assert members, f"{operation} returned no Capture"
    assert_payload_verbatim(members)
    return members


def _fetched(recording: Recording) -> Any:
    return WebFetch()(_context("web.fetch", input={"url": recording.request_url}))


def _fetch_case(recording: Recording) -> None:
    (member,) = _members("web.fetch", _fetched(recording))
    assert _digest(member.stored) == _digest(recording.response.body.encode("utf-8"))
    coordinate = member.coordinate
    assert coordinate["final_url"] == recording.request_url
    assert coordinate["status"] == recording.response.status_code
    assert coordinate["content_type"] == recording.response.headers.get("content-type", "")
    assert coordinate["source"] == "packaged-recording"
    assert "derived_from" not in coordinate and "renderer" not in coordinate


def _pages_case(recording: Recording) -> None:
    """Several pages of one request: each member is the body its page's origin sent."""

    values = [f"station-{index:02d}" for index in range(12)]
    result = WebFetchPages()(
        _context(
            "web.fetch.pages",
            input={
                "url": recording.request_url,
                "query": [{"name": "station", "values": values, "join": "comma"}],
                "page_param": "station",
                "max_url_chars": len(recording.request_url) + 40,
            },
            declared_endpoints=("dynamic:target-from-run-input",),
        )
    )
    members = _members("web.fetch.pages", result)
    assert len(members) > 1
    origin = _digest(recording.response.body.encode("utf-8"))
    for index, member in enumerate(members):
        assert _digest(member.stored) == origin
        coordinate = member.coordinate
        assert coordinate["page_index"] == index
        assert coordinate["page_count"] == len(members)
        assert coordinate["url"].startswith(recording.request_url + "?station=")
        assert coordinate["source"] == "packaged-recording"
        assert "derived_from" not in coordinate and "renderer" not in coordinate
    # One request per member: identified by its request digest, never its index.
    assert len({member.coordinate["request_digest"] for member in members}) == len(members)


def _search_case(recording: Recording) -> None:
    result = SearxngSearch()(
        _context(
            "search.web",
            input={"query": "tide gauge"},
            coordinates={"instance_url": "https://fixture.invalid"},
            declared_endpoints=("https://fixture.invalid",),
        )
    )
    (member,) = _members("search.web", result)
    assert _digest(member.stored) == _digest(recording.response.body.encode("utf-8"))
    assert member.coordinate["source"] == "packaged-recording"


def _dom_case(recording: Recording) -> None:
    result = WebRenderDom()(
        _context(
            "web.render.dom",
            input={"url": recording.request_url},
            declared_endpoints=("dynamic:target-from-run-input",),
        )
    )
    (member,) = _members("web.render.dom", result)
    # The recorded post-assembly DOM is the origin of a recorded render.
    assert recording.rendered_body is not None
    assert _digest(member.stored) == _digest(recording.rendered_body.encode("utf-8"))
    assert member.coordinate["renderer"] == f"recorded:{recording.id}"
    assert "derived_from" not in member.coordinate
    # The browser's DOM is not the body the origin sent.
    assert member.stored != recording.response.body.encode("utf-8")


def _render_case(recording: Recording, form: str) -> None:
    fetched = _fetched(recording)
    (source,) = _members("web.fetch", fetched)
    result = WebRender()(
        _context(
            "web.render",
            input={"form": form, "source": fetched.output["capture"]},
            declared_endpoints=(),
        )
    )
    (member,) = _members("web.render", result)
    if form == "json":
        # The derive's origin is a function of the cited bytes: recompute it.
        assert member.stored == document_json(
            recording.response.body,
            url=recording.request_url,
            final_url=recording.request_url,
            text=json.loads(member.stored)["text"],
        )
    assert member.coordinate["derived_from"] == _digest(source.stored)
    assert member.coordinate["renderer"]
    # The fetched Capture's bytes never carry the derive, and the derive's
    # bytes never carry the fetched Capture.
    assert member.stored != source.stored


def _cases() -> dict[str, list[tuple[str, Callable[[], None]]]]:
    recordings = load_recordings()
    fetchable = [item for item in recordings.values() if item.id != "searxng-answer"]
    rendered = [item for item in fetchable if item.rendered_body is not None]
    # A derive reads the main content of a document an origin sent whole; a
    # client-assembled page carries none until a browser assembles it.
    html = [
        item
        for item in fetchable
        if item.response.headers.get("content-type", "").startswith("text/html")
        and item.rendered_body is None
    ]
    cases: dict[str, list[tuple[str, Callable[[], None]]]] = {
        "web.fetch": [(item.id, lambda item=item: _fetch_case(item)) for item in fetchable],
        "web.fetch.pages": [(item.id, lambda item=item: _pages_case(item)) for item in fetchable],
        "search.web": [("searxng-answer", lambda: _search_case(recordings["searxng-answer"]))],
        "web.render.dom": [(item.id, lambda item=item: _dom_case(item)) for item in rendered],
        "web.render": [],
    }
    if importlib.util.find_spec("trafilatura") is not None:
        cases["web.render"] += [
            (f"{form}:{item.id}", lambda item=item, form=form: _render_case(item, form))
            for item in html
            for form in ("json", "text")
        ]
    return cases


CASES = _cases()


def test_every_capture_producing_operation_has_a_case() -> None:
    assert _capture_operations() == sorted(CASES)
    missing = [name for name in CASES if not CASES[name]]
    if missing == ["web.render"] and importlib.util.find_spec("trafilatura") is None:
        pytest.skip("web.render's cases need trafilatura, a base dependency")
    assert not missing, missing


@pytest.mark.parametrize(
    "case",
    [check for name in sorted(CASES) for _id, check in CASES[name]],
    ids=[f"{name}:{case_id}" for name in sorted(CASES) for case_id, _check in CASES[name]],
)
def test_the_stored_bytes_are_the_origin_bytes(case: Callable[[], None]) -> None:
    case()
