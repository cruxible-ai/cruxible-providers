"""The opt-in real-engine lane for the web plane.

``pytest -m engine``. Never part of the default run, and never part of the
default CI lane: everything here needs the ``browser`` extra installed *and* the
browser binary downloaded, which is exactly the cost the heavy-engine split
exists to keep out of an ordinary test run.

What the lane is for. The default lane exercises ``web.render.dom`` against a
recorded post-assembly DOM, and the wiring that records every origin against a
page double; that proves the adapter, and proves nothing about the renderer.
These tests drive the real renderer over a page served from a loopback server
in this process, so the claims they add are the ones that are missing: a
browser this adapter drives does assemble the page, the adapter reads what it
assembled, and a real Playwright page satisfies the surface the wiring is
written against.

Each test skips with a reason rather than failing when the engine is absent, so
that ``pytest -m engine --collect-only`` collects cleanly on a machine with no
engines at all.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from cruxible_provider_runtime.egress import EgressRecorder
from cruxible_provider_runtime.errors import Declined
from cruxible_provider_web.addresses import AddressGuard, IPAddress
from cruxible_provider_web.browser import PlaywrightRenderer
from cruxible_provider_web.operations import PRIVATE_OR_INTERNAL_ADDRESS

pytestmark = pytest.mark.engine

ASSEMBLING_PAGE = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>Gauge dashboard</title></head>
<body>
  <div id="root"></div>
  <noscript>This dashboard requires JavaScript.</noscript>
  <script>
    document.getElementById("root").innerHTML =
      "<main><h1>Gauge dashboard</h1><p>Newlyn reported a mean sea level of 3.214 " +
      "metres over the last complete tidal cycle, assembled client-side.</p></main>";
  </script>
</body>
</html>
"""


PAGE = ASSEMBLING_PAGE.encode("utf-8")

#: A page that opens a WebSocket back to the server that served it. The
#: handshake fails (the server speaks only HTTP), and that does not matter: a
#: browser reports the socket on the page's ``websocket`` event when it is
#: created, which is the contact the recorder must hold.
STREAMING_PAGE = b"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Live gauges</title></head>
<body><main><h1>Live gauges</h1></main>
<script>new WebSocket("ws://" + location.host + "/live?channel=gauges");</script>
</body></html>
"""


@pytest.fixture()
def page_url() -> Iterator[str]:
    """The assembling page, served from a loopback server for the test's duration.

    ``/stream`` on the same server is the page that opens a WebSocket.
    """

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = STREAMING_PAGE if self.path == "/stream" else PAGE
            self.send_response(200)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/dashboard"
    finally:
        server.shutdown()
        server.server_close()


class _LoopbackServed(AddressGuard):
    """The production guard, except for the loopback server this lane serves from.

    Everything else it judges exactly as the production guard does, including
    the address the browser reports for the main-frame response.
    """

    def judge(self, url: str, host: str, address: IPAddress) -> None:
        if not address.is_loopback:
            super().judge(url, host, address)


def _render(url: str, recorder: EgressRecorder | None = None):  # type: ignore[no-untyped-def]
    return PlaywrightRenderer(guard=_LoopbackServed()).render(
        url, timeout_seconds=30.0, recorder=recorder or EgressRecorder()
    )


@pytest.fixture()
def browser_available() -> None:
    playwright = pytest.importorskip(
        "playwright.sync_api", reason="the browser extra is not installed"
    )
    try:
        with playwright.sync_playwright() as instance:
            instance.chromium.launch(headless=True).close()
    except Exception as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"a chromium build is not available: {exc}")


@pytest.mark.usefixtures("browser_available")
def test_the_renderer_returns_the_assembled_document(page_url: str) -> None:
    rendered = _render(page_url)

    assert rendered.engine == "playwright"
    # Present only after the script ran: the assertion fails if the adapter
    # returned the initial response instead of the assembled document.
    assert "3.214" in rendered.html
    # The script's own source carries the reading too; the assembled element is
    # what only a run script produces. (The <noscript> fallback stays in any
    # serialized DOM, so its text says nothing here; the extraction test below
    # is where it must be absent.)
    assert '<div id="root"><main><h1>Gauge dashboard</h1>' in rendered.html


@pytest.mark.usefixtures("browser_available")
def test_a_real_page_satisfies_the_surface_the_wiring_is_written_against(page_url: str) -> None:
    """The half of the seam the default lane cannot reach.

    ``drive_page`` is written against a protocol, and the default lane drives it
    over a double. That proves the wiring and proves nothing about whether a real
    Playwright page answers ``on``, ``goto``, ``content`` and a navigation
    response the way the protocol says. This is where that is established: the
    page pulls nothing, so the recorder holds exactly the main frame's origin.
    """

    recorder = EgressRecorder()
    rendered = _render(page_url, recorder)

    assert rendered.final_url == page_url
    assert rendered.status_code == 200
    assert rendered.headers["content-type"].startswith("text/html")
    assert recorder.observed() == [page_url.removesuffix("/dashboard")]


@pytest.mark.usefixtures("browser_available")
def test_the_assembled_document_extracts_the_way_the_recording_says(page_url: str) -> None:
    """Ties the real renderer to the recorded fixture's expectation.

    The recording claims a rendered dashboard extracts to Markdown carrying the
    assembled reading. This runs the real browser and the real extractor over an
    equivalent page and asserts the same thing, which is what keeps the recording
    from drifting into a description of nothing.
    """

    from cruxible_provider_web.engines import TrafilaturaExtractor

    rendered = _render(page_url)
    extraction = TrafilaturaExtractor().extract(rendered.html, url=page_url)

    assert extraction.kind == "markdown"
    assert "3.214" in extraction.text
    assert "requires JavaScript" not in extraction.text


def test_the_contact_hooks_are_events_a_real_playwright_page_emits() -> None:
    """``drive_page`` attaches its hooks by name, and ``Page.on`` accepts any name.

    A misspelt event would never fire and never fail, so the names are held
    against the ones Playwright's own ``Page`` declares.
    """

    pytest.importorskip("playwright.sync_api", reason="the browser extra is not installed")
    from cruxible_provider_web.browser import PAGE_CONTACT_EVENTS
    from playwright._impl._page import Page

    emitted = set(vars(Page.Events).values())
    assert {"request", "response", "websocket"} <= set(PAGE_CONTACT_EVENTS)
    assert set(PAGE_CONTACT_EVENTS) <= emitted


@pytest.mark.usefixtures("browser_available")
def test_a_websocket_a_real_page_opens_reaches_the_recorder(page_url: str) -> None:
    """The browser-engine check of the ``websocket`` wiring, end to end."""

    origin = page_url.removesuffix("/dashboard")
    recorder = EgressRecorder()
    _render(f"{origin}/stream", recorder)

    assert set(recorder.observed()) == {origin, origin.replace("http://", "ws://", 1)}


@pytest.mark.usefixtures("browser_available")
def test_the_production_renderer_refuses_a_loopback_page(page_url: str) -> None:
    """With the production guard, the same loopback page is never navigated to."""

    recorder = EgressRecorder()
    with pytest.raises(Declined) as refused:
        PlaywrightRenderer().render(page_url, timeout_seconds=30.0, recorder=recorder)

    assert refused.value.code == PRIVATE_OR_INTERNAL_ADDRESS
    assert refused.value.refusal.detail["address_class"] == "loopback"
    assert recorder.observed() == []


@pytest.mark.usefixtures("browser_available")
def test_a_real_browser_reports_the_address_that_served_the_main_frame(page_url: str) -> None:
    """The post-navigation check reads ``Response.server_addr``; a real page has one.

    If a Playwright release stopped reporting it, every production render would
    be declined; this is where that would show.
    """

    rendered = _render(page_url)

    assert rendered.status_code == 200
