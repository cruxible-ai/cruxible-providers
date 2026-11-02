"""``web.render``: a derived Capture citing the fetched one, and what it records.

A dom render is the one path in this plane that does not go through the
instrumented HTTP client: a browser opens its own sockets and pulls whatever the
markup names, so nothing it does reaches the run's recorder unless the adapter
asks it to. And the document it assembles must be the cited one: the main frame
is served from the cited bytes, never fetched again.

Both decisions live in ``drive_page`` rather than inside the engine, which is
what makes this lane possible: the page double below implements the same
surface a Playwright page does, so the wiring is reviewed here rather than only
on a machine with a browser installed. ``tests/test_web_engine.py`` is the other
half — it asserts a real page satisfies that surface.
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import pytest
from cruxible_provider_runtime.egress import EgressRecorder
from cruxible_provider_runtime.protocol import Budgets
from cruxible_provider_runtime.provider_api import ProviderResult, ProviderRunContext
from cruxible_provider_web.engines import Extraction, RenderedPage, drive_page
from cruxible_provider_web.operations import PROVIDER_DECLINED
from cruxible_provider_web.recordings import load_recordings
from cruxible_provider_web.render import WebRender

BUDGETS = Budgets(wall_clock_seconds=30.0, output_bytes=1_000_000)

REQUESTED = "https://first.example/report"
SETTLED = "https://second.example/report"
ASSEMBLED = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Gauge report</title></head>
<body><main><h1>Gauge report</h1>
<p>Newlyn reported a mean sea level of 3.214 metres over the last complete tidal
cycle, assembled client-side from the readings endpoint.</p></main></body></html>
"""
WIRE_BODY = b'<!DOCTYPE html>\n<html lang="en"><body><div id="root"></div></body></html>\n'


@dataclass
class _Event:
    """What a page hands its ``request`` and ``response`` handlers."""

    url: str


@dataclass
class _FakeResponse:
    """The main-frame response surface, as a browser exposes it."""

    url: str
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    payload: bytes = b""

    def all_headers(self) -> dict[str, str]:
        return dict(self.headers)

    def body(self) -> bytes:
        return self.payload


class _FakeRoute:
    def __init__(self) -> None:
        self.fulfilled: dict[str, Any] | None = None

    def fulfill(self, **kwargs: Any) -> None:
        self.fulfilled = kwargs


class _FakePage:
    """A page double carrying the surface ``drive_page`` drives.

    ``contacts`` is the script this navigation plays back: the ``(event, url)``
    pairs a browser would emit while loading — the main frame, the script the
    markup names, the endpoint that script queries. It stands in for the
    sequence of events, not for a browser. A main-frame request a route matches
    is fulfilled by the route, as a browser does, and the response carries the
    fulfilled body.
    """

    def __init__(self, *, html: str, contacts: Sequence[tuple[str, str]] = ()) -> None:
        self._html = html
        self._contacts = list(contacts)
        self._handlers: dict[str, list[Callable[[Any], None]]] = {}
        self._routes: list[tuple[Callable[[str], bool], Callable[[Any], None]]] = []
        self.url = "about:blank"
        self.served: dict[str, Any] | None = None
        self.order: list[str] = []

    def on(self, event: str, handler: Callable[[Any], None]) -> None:
        self.order.append(event)
        self._handlers.setdefault(event, []).append(handler)

    def route(self, url: Callable[[str], bool], handler: Callable[[Any], None]) -> None:
        self.order.append("route")
        self._routes.append((url, handler))

    def goto(self, url: str, *, wait_until: str, timeout: float) -> _FakeResponse | None:
        del wait_until, timeout
        self.order.append("goto")
        self.url = url
        route = _FakeRoute()
        for matches, handler in self._routes:
            if matches(url):
                handler(route)
                break
        self.served = route.fulfilled
        for event, contacted in [("request", url), *self._contacts, ("response", url)]:
            for handler in self._handlers.get(event, []):
                handler(_Event(contacted))
        if self.served is None:
            return None
        return _FakeResponse(
            url=url,
            status=self.served["status"],
            headers=self.served["headers"],
            payload=self.served["body"],
        )

    def content(self) -> str:
        return self._html


class _PageRenderer:
    """A renderer that drives the production wiring over a page double."""

    name = "playwright"

    def __init__(self, page: _FakePage) -> None:
        self._page = page

    def render(
        self,
        url: str,
        *,
        document: bytes,
        content_type: str,
        timeout_seconds: float,
        recorder: EgressRecorder,
    ) -> RenderedPage:
        return drive_page(
            self._page,
            url,
            document=document,
            content_type=content_type,
            timeout_seconds=timeout_seconds,
            recorder=recorder,
            engine=self.name,
        )


def _page() -> _FakePage:
    return _FakePage(
        html=ASSEMBLED,
        contacts=[
            ("request", "https://cdn.thirdparty.example/bundle.js"),
            ("request", "https://api.readings.example/v1/gauges"),
            # A browser also loads things that have no origin at all. They are
            # not egress and must not be pushed at a recorder that would refuse
            # to normalise them.
            ("request", "data:text/css;base64,Ym9keXt9"),
            ("response", "https://api.readings.example/v1/gauges"),
        ],
    )


def _coordinate(**changes: Any) -> dict[str, Any]:
    return {
        "url": REQUESTED,
        "final_url": SETTLED,
        "status": 200,
        "content_type": "text/html; charset=utf-8",
        "request_digest": "sha256:" + hashlib.sha256(REQUESTED.encode()).hexdigest(),
        "source": "network",
        **changes,
    }


def _context(**overrides: Any) -> ProviderRunContext:
    fields: dict[str, Any] = {
        "run_id": "run-rendered",
        "operation": "web.render",
        "operation_signature_digest": "sha256:" + "aa" * 32,
        "build_digest": "sha256:" + "cc" * 32,
        "implementation_digest": "sha256:" + "bb" * 32,
        "input": {
            "form": "dom",
            "source": {
                "content_base64": base64.b64encode(WIRE_BODY).decode("ascii"),
                "coordinate": _coordinate(),
            },
        },
        "coordinates": {},
        "budgets": BUDGETS,
        "declared_endpoints": ("dynamic:target-from-run-input",),
        "capture_contract": None,
        "secrets": {},
        "egress": EgressRecorder(),
    }
    fields.update(overrides)
    return ProviderRunContext(**fields)


def _run(page: _FakePage, **overrides: Any) -> tuple[ProviderResult, EgressRecorder]:
    recorder = EgressRecorder()
    context = _context(egress=recorder, **overrides)
    return WebRender(renderer=_PageRenderer(page))(context), recorder


def test_the_main_frame_is_served_from_the_cited_bytes() -> None:
    page = _page()
    result, _ = _run(page)
    assert result.status == "ok", result
    assert page.served == {
        "status": 200,
        "headers": {"content-type": "text/html; charset=utf-8"},
        "body": WIRE_BODY,
    }
    # The route and both hooks are attached before the navigation starts.
    assert page.order[-1] == "goto" and set(page.order[:-1]) == {"route", "request", "response"}


def test_every_origin_the_page_contacts_reaches_the_recorder_but_the_served_frame() -> None:
    result, recorder = _run(_page())
    assert result.status == "ok", result
    assert set(recorder.observed()) == {
        "https://cdn.thirdparty.example",
        "https://api.readings.example",
    }


def test_the_derived_capture_is_the_dom_and_cites_the_fetched_bytes() -> None:
    result, _ = _run(_page())
    assert result.status == "ok", result
    output = result.output
    derived = base64.b64decode(output["content_base64"])
    assert derived == ASSEMBLED.encode("utf-8")
    assert output["bytes_digest"] == "sha256:" + hashlib.sha256(derived).hexdigest()
    coordinate = output["coordinate"]
    assert coordinate["derived_from"] == "sha256:" + hashlib.sha256(WIRE_BODY).hexdigest()
    assert coordinate["renderer"] == "playwright"
    assert coordinate["content_type"] == "text/html; charset=utf-8"
    # The cited document's own coordinate is carried over, not re-derived.
    assert (coordinate["url"], coordinate["final_url"], coordinate["status"]) == (
        REQUESTED,
        SETTLED,
        200,
    )
    assert coordinate["source"] == "network"
    assert output["source_identity"] == "web.rendered"


class _Extractor:
    name = "fixed-extractor"

    def __init__(self) -> None:
        self.read: list[str] = []

    def extract(self, html: str, *, url: str) -> Extraction:
        self.read.append(html)
        return Extraction(engine=self.name, kind="markdown", text="# Gauge report\n")


def test_a_text_render_extracts_the_cited_document_and_contacts_nothing() -> None:
    extractor = _Extractor()
    recorder = EgressRecorder()
    context = _context(
        egress=recorder,
        input={
            "form": "text",
            "source": {
                "content_base64": base64.b64encode(ASSEMBLED.encode()).decode("ascii"),
                "coordinate": _coordinate(),
            },
        },
    )
    result = WebRender(extractor=extractor)(context)
    assert result.status == "ok", result
    assert extractor.read == [ASSEMBLED]
    assert base64.b64decode(result.output["content_base64"]) == b"# Gauge report\n"
    coordinate = result.output["coordinate"]
    assert coordinate["renderer"] == "fixed-extractor"
    assert coordinate["content_type"] == "text/markdown; charset=utf-8"
    assert recorder.observed() == []


@pytest.mark.parametrize(
    "input_",
    [
        {"form": "pdf", "source": {"content_base64": "", "coordinate": _coordinate()}},
        {"form": "dom"},
        {"form": "dom", "source": {"content_base64": "not base64!", "coordinate": _coordinate()}},
        {"form": "dom", "source": {"content_base64": "", "coordinate": _coordinate()}},
        {"form": "dom", "source": {"content_base64": "eA==", "coordinate": {"url": REQUESTED}}},
        {
            "form": "text",
            "source": {
                "content_base64": base64.b64encode(b'{"a": 1}').decode("ascii"),
                "coordinate": _coordinate(content_type="application/json"),
            },
        },
    ],
    ids=["form", "no-source", "bad-base64", "empty", "bad-coordinate", "text-of-json"],
)
def test_a_render_declines_what_it_cannot_cite_or_read(input_: dict[str, Any]) -> None:
    result = WebRender(renderer=_PageRenderer(_page()), extractor=_Extractor())(
        _context(input=input_)
    )
    assert result.status == "refused"
    assert result.refusal is not None and result.refusal.code == PROVIDER_DECLINED


def test_a_recorded_render_replays_only_the_recorded_document() -> None:
    recording = load_recordings()["dashboard-rendered"]
    assert recording.rendered_body is not None
    url = recording.request_url
    fetched = recording.response.body.encode("utf-8")

    def context(document: bytes) -> ProviderRunContext:
        return _context(
            input={
                "form": "dom",
                "source": {
                    "content_base64": base64.b64encode(document).decode("ascii"),
                    "coordinate": _coordinate(url=url, final_url=url, source="packaged-recording"),
                },
            }
        )

    replayed = WebRender()(context(fetched))
    assert replayed.status == "ok", replayed
    assert base64.b64decode(replayed.output["content_base64"]) == recording.rendered_body.encode()
    assert replayed.output["coordinate"]["renderer"] == f"recorded:{recording.id}"
    assert replayed.output["coordinate"]["source"] == "packaged-recording"
    other = WebRender()(context(fetched + b"<!-- another document -->"))
    assert other.status == "refused"


class _UnroutablePage(_FakePage):
    """A page whose navigation produces no main-frame response at all."""

    def route(self, url: Callable[[str], bool], handler: Callable[[Any], None]) -> None:
        self.order.append("route")


def test_a_navigation_with_no_main_frame_response_claims_no_status() -> None:
    rendered = drive_page(
        _UnroutablePage(html=ASSEMBLED),
        REQUESTED,
        document=WIRE_BODY,
        content_type="text/html",
        timeout_seconds=1.0,
        recorder=EgressRecorder(),
        engine="playwright",
    )
    assert rendered.status_code is None
    assert rendered.body is None
    assert rendered.final_url == REQUESTED
