"""``web.render.dom``: a fetch through a browser, and everything it contacts recorded.

A browser is the one path in this plane that does not go through the
instrumented HTTP client: it opens its own sockets and pulls whatever the markup
names, so nothing it does reaches the run's recorder unless the adapter asks it
to. That decision lives in ``drive_page`` rather than inside the engine, which
is what makes this lane possible: the page double below implements the same
surface a Playwright page does, so the wiring is reviewed here rather than only
on a machine with a browser installed. ``tests/test_web_engine.py`` is the other
half -- it asserts a real page satisfies that surface.
"""

from __future__ import annotations

import base64
import hashlib
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import pytest
from cruxible_provider_runtime.egress import EgressRecorder
from cruxible_provider_runtime.errors import Declined
from cruxible_provider_runtime.protocol import Budgets
from cruxible_provider_runtime.provider_api import ProviderResult, ProviderRunContext
from cruxible_provider_web.addresses import AddressGuard
from cruxible_provider_web.browser import PAGE_CONTACT_EVENTS, RenderedPage, drive_page
from cruxible_provider_web.dom import WebRenderDom
from cruxible_provider_web.operations import (
    ENGINE_UNAVAILABLE,
    PRIVATE_OR_INTERNAL_ADDRESS,
    PROVIDER_DECLINED,
)
from cruxible_provider_web.recordings import load_recordings

BUDGETS = Budgets(wall_clock_seconds=30.0, output_bytes=1_000_000)

PUBLIC_V4 = "93.184.216.34"


def _public(host: str, port: int) -> Sequence[str]:
    """A resolver stub: every name is public. No test here touches real DNS."""

    del host, port
    return (PUBLIC_V4,)


def _guard() -> AddressGuard:
    return AddressGuard(_public)


REQUESTED = "https://first.example/report"
SETTLED = "https://second.example/report"
ASSEMBLED = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Gauge report</title></head>
<body><main><h1>Gauge report</h1>
<p>Newlyn reported a mean sea level of 3.214 metres over the last complete tidal
cycle, assembled client-side from the readings endpoint.</p></main></body></html>
"""


#: The events a Playwright ``Page`` emits (``Page.on``, playwright-python 1.4x).
#: The double below refuses any other name, so a hook attached to a misspelt or
#: invented event fails here rather than silently never firing in a browser;
#: ``tests/test_web_engine.py`` holds the same names against the real class.
PLAYWRIGHT_PAGE_EVENTS = frozenset(
    {
        "close",
        "console",
        "crash",
        "dialog",
        "domcontentloaded",
        "download",
        "filechooser",
        "frameattached",
        "framedetached",
        "framenavigated",
        "load",
        "pageerror",
        "popup",
        "request",
        "requestfailed",
        "requestfinished",
        "response",
        "websocket",
        "worker",
    }
)


@dataclass
class _Event:
    """What a page hands its handlers: a ``Request``, ``Response`` or ``WebSocket``."""

    url: str


@dataclass(frozen=True, eq=False)
class _FakeFrame:
    """A frame; the main frame is the one with no parent. Compared by identity."""

    parent_frame: _FakeFrame | None = None

    __hash__ = object.__hash__


MAIN_FRAME = _FakeFrame()


@dataclass
class _FakeRequest:
    """The request a response answers: a navigation of some frame, or not."""

    navigation: bool = True
    frame: _FakeFrame = MAIN_FRAME

    def is_navigation_request(self) -> bool:
        return self.navigation


@dataclass
class _FakeResponse:
    """The main-frame response surface, as a browser exposes it.

    By default it answers a main-frame navigation, which is what ``goto``
    returns and what the page emits on ``response`` when the document arrives.
    """

    url: str
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    server_address: str | None = PUBLIC_V4
    """What the browser reports it connected to; ``None`` when it does not say."""
    request: _FakeRequest = field(default_factory=_FakeRequest)

    def all_headers(self) -> dict[str, str]:
        return dict(self.headers)

    def server_addr(self) -> dict[str, Any] | None:
        if self.server_address is None:
            return None
        return {"ipAddress": self.server_address, "port": 443}


class _FakePage:
    """A page double carrying the surface ``drive_page`` drives.

    ``contacts`` is the script this navigation plays back: the ``(event, url)``
    pairs a browser would emit while loading -- the main frame, the redirect it
    followed, the script the markup names, the endpoint that script queries. It
    stands in for the sequence of events, not for a browser.

    ``documents`` are the responses the page emits on ``response`` as documents
    arrive (by default just ``response``, the one ``goto`` returns), and the main
    frame's ``url`` ends on the last of them unless ``url_after`` says otherwise.
    ``while_read`` runs inside ``content``: whatever the page does while it is
    being serialized.
    """

    def __init__(
        self,
        *,
        html: str = ASSEMBLED,
        contacts: Sequence[tuple[str, str]] = (),
        response: _FakeResponse | None = None,
        documents: Sequence[_FakeResponse] | None = None,
        url_after: str | None = None,
        while_read: Callable[[_FakePage], None] | None = None,
    ) -> None:
        self._html = html
        self._contacts = list(contacts)
        self._handlers: dict[str, list[Callable[[Any], None]]] = {}
        self._response = response
        self._documents = (
            list(documents)
            if documents is not None
            else ([response] if response is not None else [])
        )
        self._url_after = url_after
        self._while_read = while_read
        self.url = "about:blank"
        self.order: list[str] = []

    def on(self, event: str, handler: Callable[[Any], None]) -> None:
        if event not in PLAYWRIGHT_PAGE_EVENTS:
            raise AssertionError(f"a Playwright page emits no {event!r} event")
        self.order.append(event)
        self._handlers.setdefault(event, []).append(handler)

    def emit(self, event: str, payload: Any) -> None:
        for handler in self._handlers.get(event, []):
            handler(payload)

    def goto(self, url: str, *, wait_until: str, timeout: float) -> _FakeResponse | None:
        del wait_until, timeout
        self.order.append("goto")
        self.url = url
        for event, contacted in [("request", url), *self._contacts]:
            self.emit(event, _Event(contacted))
        for document in self._documents:
            self.emit("response", document)
            if document.request.navigation and document.request.frame.parent_frame is None:
                self.url = document.url
        if self._url_after is not None:
            self.url = self._url_after
        return self._response

    def content(self) -> str:
        if self._while_read is not None:
            self._while_read(self)
        return self._html


class _PageRenderer:
    """A renderer that drives the production wiring over a page double."""

    name = "playwright"

    def __init__(self, page: _FakePage, guard: AddressGuard | None = None) -> None:
        self._page = page
        self._guard = guard or _guard()

    def render(self, url: str, *, timeout_seconds: float, recorder: EgressRecorder) -> RenderedPage:
        return drive_page(
            self._page,
            url,
            timeout_seconds=timeout_seconds,
            recorder=recorder,
            engine=self.name,
            guard=self._guard,
        )


def _page(**changes: Any) -> _FakePage:
    arguments: dict[str, Any] = {
        "contacts": [
            ("request", SETTLED),
            ("request", "https://cdn.thirdparty.example/bundle.js"),
            ("request", "https://api.readings.example/v1/gauges"),
            # A browser also loads things that have no origin at all. They are
            # not egress and must not be pushed at a recorder that would refuse
            # to normalise them.
            ("request", "data:text/css;base64,Ym9keXt9"),
            ("response", "https://api.readings.example/v1/gauges"),
        ],
        "response": _FakeResponse(
            url=SETTLED,
            status=200,
            headers={"Content-Type": "text/html", "ETag": '"v7"', "Content-Length": "77"},
        ),
    }
    arguments.update(changes)
    return _FakePage(**arguments)


def _context(**overrides: Any) -> ProviderRunContext:
    fields: dict[str, Any] = {
        "run_id": "run-dom",
        "operation": "web.render.dom",
        "operation_signature_digest": "sha256:" + "aa" * 32,
        "build_digest": "sha256:" + "cc" * 32,
        "implementation_digest": "sha256:" + "bb" * 32,
        "input": {"url": REQUESTED},
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
    return WebRenderDom(renderer=_PageRenderer(page))(_context(egress=recorder, **overrides)), (
        recorder
    )


def test_the_hooks_are_attached_before_the_navigation() -> None:
    page = _page()
    result, _ = _run(page)
    assert result.status == "ok", result
    assert page.order[-1] == "goto"
    assert set(page.order[:-1]) == {"request", "response", "websocket"}


def test_every_hook_is_on_an_event_a_playwright_page_emits() -> None:
    assert "websocket" in PAGE_CONTACT_EVENTS
    assert set(PAGE_CONTACT_EVENTS) <= PLAYWRIGHT_PAGE_EVENTS


def test_a_websocket_the_page_opens_reaches_the_recorder() -> None:
    """Playwright reports a WebSocket only through ``websocket``, never ``request``."""

    contacts = [
        ("request", SETTLED),
        ("websocket", "wss://stream.third.example/live?channel=gauges"),
        ("websocket", "ws://Plain.Third.Example:80/feed"),
        ("websocket", "wss://stream.other.example:8443/feed"),
    ]
    page = _page(contacts=contacts)
    recorder = EgressRecorder()
    serialized = page.content
    at_serialization: list[str] = []

    def content() -> str:
        at_serialization.extend(recorder.observed())
        return serialized()

    page.content = content  # type: ignore[method-assign]
    result = WebRenderDom(renderer=_PageRenderer(page))(_context(egress=recorder))
    assert result.status == "ok", result
    expected = {
        "https://first.example",
        "https://second.example",
        # Normalised like an HTTP origin: path and query dropped, host lowered,
        # the scheme's default port implicit, any other port kept.
        "wss://stream.third.example",
        "ws://plain.third.example",
        "wss://stream.other.example:8443",
    }
    assert set(recorder.observed()) == expected
    # Recorded before the DOM was serialized: the Capture never precedes them.
    assert set(at_serialization) == expected


def test_without_the_websocket_hook_the_endpoint_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The double plays WebSockets on their own event, as a browser does.

    With only the HTTP hooks attached (the wiring before this fix) the stream's
    host never reaches the recorder: the regression above is not satisfied by
    the request/response hooks by accident.
    """

    from cruxible_provider_web import browser

    monkeypatch.setattr(browser, "PAGE_CONTACT_EVENTS", ("request", "response"))
    recorder = EgressRecorder()
    page = _page(contacts=[("websocket", "wss://stream.third.example/live")])
    drive_page(
        page,
        REQUESTED,
        timeout_seconds=1.0,
        recorder=recorder,
        engine="playwright",
        guard=_guard(),
    )
    # The requested URL and the main-frame document's response; no stream.
    assert recorder.observed() == ["https://first.example", "https://second.example"]


def test_every_origin_the_page_contacts_reaches_the_recorder() -> None:
    result, recorder = _run(_page())
    assert result.status == "ok", result
    assert set(recorder.observed()) == {
        "https://first.example",
        "https://second.example",
        "https://cdn.thirdparty.example",
        "https://api.readings.example",
    }


def test_the_capture_is_the_assembled_dom_with_the_main_frame_s_coordinate() -> None:
    result, _ = _run(_page())
    assert result.status == "ok", result
    output = result.output
    stored = base64.b64decode(output["content_base64"])
    assert stored == ASSEMBLED.encode("utf-8")
    assert output["bytes_digest"] == "sha256:" + hashlib.sha256(stored).hexdigest()
    coordinate = output["coordinate"]
    assert coordinate == {
        "url": REQUESTED,
        "final_url": SETTLED,
        "status": 200,
        "content_type": "text/html; charset=utf-8",
        "request_digest": "sha256:" + hashlib.sha256(REQUESTED.encode()).hexdigest(),
        "source": "network",
        # The main frame's length is not the DOM's; only its validator is kept.
        "headers": {"etag": '"v7"'},
        "renderer": "playwright",
    }
    assert output["source_identity"] == "web.rendered"


def test_a_main_frame_outside_2xx_is_a_failed_attempt() -> None:
    result, _ = _run(_page(response=_FakeResponse(url=SETTLED, status=503)))
    assert result.status == "error" and result.error is not None
    assert result.error.kind == "HttpStatus"
    assert result.error.detail["status_code"] == 503


# -- one document: the DOM and the response it was served under ---------------

FIRST_DOC = "https://second.example/first"
SECOND_DOC = "https://second.example/second"
SECOND_DOM = "<html><body><main>Second document</main></body></html>"


def _document(url: str, etag: str, **changes: Any) -> _FakeResponse:
    return _FakeResponse(
        url=url, status=200, headers={"content-type": "text/html", "etag": etag}, **changes
    )


def _declined_unpaired(result: ProviderResult) -> None:
    assert result.status == "refused" and result.refusal is not None
    assert result.refusal.code == PROVIDER_DECLINED
    assert result.refusal.detail == {"url": REQUESTED, "engine": "playwright"}
    assert result.output is None


def test_a_second_same_origin_document_is_not_paired_with_the_first_response() -> None:
    """``goto`` returns the first document's response; the page is on the second.

    Without the pairing check the Capture held the second document's DOM under
    the first one's URL, validator and checked address.
    """

    first = _document(FIRST_DOC, '"first"')
    page = _page(
        response=first,
        documents=[first, _document(SECOND_DOC, '"second"')],
        html=SECOND_DOM,
    )
    result, _ = _run(page)
    _declined_unpaired(result)


def test_a_later_document_from_a_private_address_is_never_read() -> None:
    first = _document(FIRST_DOC, '"first"')
    read: list[str] = []
    page = _page(
        response=first,
        documents=[first, _document(SECOND_DOC, '"second"', server_address="10.0.0.9")],
        html=SECOND_DOM,
        while_read=lambda page: read.append(page.url),
    )
    result, _ = _run(page)
    _declined_unpaired(result)
    assert read == []


def test_a_navigation_to_a_document_without_a_response_is_not_paired() -> None:
    """``about:blank`` (or a ``blob:`` URL) replaces the document with no response."""

    first = _document(FIRST_DOC, '"first"')
    for after in ("about:blank", "blob:https://second.example/4b1d"):
        result, _ = _run(_page(response=first, url_after=after))
        _declined_unpaired(result)


def test_a_main_frame_that_changes_while_it_is_read_is_declined() -> None:
    first = _document(FIRST_DOC, '"first"')

    def navigates(page: _FakePage) -> None:
        page.emit("response", _document(SECOND_DOC, '"second"'))
        page.url = SECOND_DOC

    result, _ = _run(_page(response=first, while_read=navigates))
    assert result.status == "refused" and result.refusal is not None
    assert result.refusal.code == PROVIDER_DECLINED
    assert "while the page was being read" in result.refusal.message


def test_the_capture_describes_the_one_document_it_holds() -> None:
    """The ordinary case, with what is allowed around it.

    A redirect hop before the document, a frame's own navigation, a subresource,
    and a same-origin ``pushState`` keep the pairing: the Capture's bytes, URL
    and validator are the settled document's.
    """

    redirect = _FakeResponse(url="https://first.example/report", status=302)
    settled = _document(FIRST_DOC, '"first"')
    framed = _FakeResponse(
        url="https://ads.example/frame",
        status=200,
        request=_FakeRequest(frame=_FakeFrame(parent_frame=MAIN_FRAME)),
    )
    subresource = _FakeResponse(
        url="https://second.example/data.json", status=200, request=_FakeRequest(navigation=False)
    )
    page = _page(
        response=settled,
        documents=[redirect, settled, framed, subresource],
        url_after="https://second.example/route/2?tab=gauges",
    )
    result, _ = _run(page)
    assert result.status == "ok", result
    assert base64.b64decode(result.output["content_base64"]) == ASSEMBLED.encode("utf-8")
    coordinate = result.output["coordinate"]
    assert coordinate["final_url"] == FIRST_DOC
    assert coordinate["headers"] == {"etag": '"first"'}


def test_a_navigation_without_a_response_assembled_nothing_to_capture() -> None:
    with pytest.raises(Declined) as refused:
        drive_page(
            _FakePage(),
            REQUESTED,
            timeout_seconds=1.0,
            recorder=EgressRecorder(),
            engine="playwright",
            guard=_guard(),
        )
    assert refused.value.code == PROVIDER_DECLINED
    result, _ = _run(_FakePage())
    assert result.status == "refused" and result.output is None


# -- the private-address guard ----------------------------------------------
#
# A browser opens its own sockets, so the guard is applied where this adapter
# can apply it: to the URL the run names, before the browser goes there, and to
# the address the browser reports for the main-frame response it returns. What
# the browser contacts in between is recorded and not vetted; these tests pin
# down both halves of that statement.


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/iam/",
        "http://127.0.0.1:8080/admin",
        "http://[::1]/",
        "http://[fd12:3456:789a::1]/",
        "http://[fe80::1%25en0]/",
        "http://[::ffff:10.0.0.1]/",
        "http://2130706433/",
        "http://localhost:9200/",
        "http://metadata.google.internal/computeMetadata/v1/",
    ],
)
def test_a_private_run_target_is_refused_before_the_browser_navigates(url: str) -> None:
    page = _page()
    recorder = EgressRecorder()
    result = WebRenderDom(renderer=_PageRenderer(page))(
        _context(input={"url": url}, egress=recorder)
    )
    assert result.status == "refused" and result.refusal is not None
    assert result.refusal.code == PRIVATE_OR_INTERNAL_ADDRESS
    # Nothing was attached and nothing was navigated: the browser never went.
    assert page.order == []
    assert recorder.observed() == []
    assert "/" not in result.refusal.detail["url"].removeprefix("http://")


def test_a_run_target_resolving_privately_is_refused_before_navigation() -> None:
    def private(host: str, port: int) -> Sequence[str]:
        del port
        return ("10.20.30.40",) if host == "first.example" else (PUBLIC_V4,)

    page = _page()
    result = WebRenderDom(renderer=_PageRenderer(page, AddressGuard(private)))(_context())
    assert result.status == "refused" and result.refusal is not None
    assert result.refusal.code == PRIVATE_OR_INTERNAL_ADDRESS
    assert result.refusal.detail == {
        "url": "https://first.example",
        "host": "first.example",
        "address": "10.20.30.40",
        "address_class": "private",
    }
    assert page.order == []


@pytest.mark.parametrize(
    ("served", "expected"),
    [
        ("169.254.169.254", "link_local"),
        ("127.0.0.1", "loopback"),
        ("10.0.0.5", "private"),
        ("::1", "loopback"),
        ("[::1]", "loopback"),
        ("::ffff:169.254.169.254", "ipv4_embedded_link_local"),
        ("fc00::5", "private"),
    ],
)
def test_a_main_frame_served_from_a_private_address_is_not_captured(
    served: str, expected: str
) -> None:
    """A public run target the browser ends up loading from a private address.

    That is a redirect onto a private host, or a name re-pointed between the
    guard's lookup and the browser's: either way the address the browser reports
    for the response is the one judged, and the assembled page is not returned.
    """

    page = _page(
        response=_FakeResponse(
            url="http://metadata.example/latest/meta-data/",
            status=200,
            server_address=served,
        )
    )
    served_html: list[str] = []
    original = page.content

    def content() -> str:
        served_html.append("read")
        return original()

    page.content = content  # type: ignore[method-assign]
    result = WebRenderDom(renderer=_PageRenderer(page))(_context())
    assert result.status == "refused" and result.refusal is not None
    assert result.refusal.code == PRIVATE_OR_INTERNAL_ADDRESS
    assert result.refusal.detail["address_class"] == expected
    assert result.refusal.detail["url"] == "http://metadata.example"
    assert result.output is None
    # The page's content was never read, so nothing it holds can leave.
    assert served_html == []


@pytest.mark.parametrize("served", [None, "", "not-an-address"])
def test_a_main_frame_without_a_readable_server_address_is_declined(served: str | None) -> None:
    page = _page(response=_FakeResponse(url=SETTLED, status=200, server_address=served))
    result = WebRenderDom(renderer=_PageRenderer(page))(_context())
    assert result.status == "refused" and result.refusal is not None
    assert result.refusal.code == PROVIDER_DECLINED
    assert result.refusal.detail == {"url": REQUESTED, "engine": "playwright"}


def test_what_the_browser_contacts_in_between_is_recorded_not_vetted() -> None:
    """The stated limit of the browser path, held so a reader cannot over-read it.

    A subresource on a private address is not refused: the browser sends it
    itself. It is recorded, and the page served from a public address is
    captured.
    """

    page = _page(contacts=[("request", "http://10.0.0.7/internal.js")])
    result, recorder = _run(page)
    assert result.status == "ok", result
    assert "http://10.0.0.7" in recorder.observed()


def test_the_production_renderer_carries_a_guard() -> None:
    from cruxible_provider_web.browser import PlaywrightRenderer

    assert isinstance(PlaywrightRenderer()._guard, AddressGuard)


def test_a_recorded_render_replays_the_recorded_dom_labelled() -> None:
    recording = load_recordings()["dashboard-rendered"]
    assert recording.rendered_body is not None
    recorder = EgressRecorder()
    replayed = WebRenderDom()(_context(input={"url": recording.request_url}, egress=recorder))
    assert replayed.status == "ok", replayed
    assert base64.b64decode(replayed.output["content_base64"]) == recording.rendered_body.encode()
    coordinate = replayed.output["coordinate"]
    assert coordinate["renderer"] == f"recorded:{recording.id}"
    assert coordinate["source"] == "packaged-recording"
    assert [event["kind"] for event in replayed.events] == ["recorded_render"]
    assert recorder.observed() == ["https://fixture.invalid"]


def test_a_recording_without_a_rendered_document_is_declined() -> None:
    result = WebRenderDom()(
        _context(input={"url": "https://fixture.invalid/articles/tide-gauge-recalibration"})
    )
    assert result.status == "refused" and result.refusal is not None
    assert result.refusal.code == PROVIDER_DECLINED


def test_an_environment_without_the_browser_extra_refuses_as_declared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    result = WebRenderDom()(_context(input={"url": "https://example.test/page"}))
    assert result.status == "refused" and result.refusal is not None
    assert result.refusal.code == ENGINE_UNAVAILABLE
    assert result.refusal.detail == {"required_extra": "browser", "engine": "playwright"}


@pytest.mark.parametrize(
    "input_",
    [
        {},
        {"url": "ftp://first.example/x"},
        {"url": REQUESTED, "logical_source": "Not A Name"},
        {"url": REQUESTED, "max_bytes": 0},
        {"url": REQUESTED, "max_bytes": 16},
    ],
    ids=["no-url", "scheme", "logical-source", "max-bytes", "too-heavy"],
)
def test_a_dom_fetch_declines_what_it_cannot_fetch(input_: dict[str, Any]) -> None:
    result, _ = _run(_page(), input=input_)
    assert result.status == "refused"
    assert result.refusal is not None and result.refusal.code == PROVIDER_DECLINED
