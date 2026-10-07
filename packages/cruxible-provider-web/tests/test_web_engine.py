"""The opt-in real-engine lane for the web plane.

``pytest -m engine``. Never part of the default run, and never part of the
default CI lane: everything here needs the ``browser`` extra installed *and* the
browser binary downloaded, which is exactly the cost the heavy-engine split
exists to keep out of an ordinary test run.

What the lane is for. The default lane exercises the adapter against a recorded
post-assembly DOM, and the wiring that decides what a rendered run may claim
against a page double; that proves the adapter, and proves nothing about the
renderer. These tests drive the real renderer, over a local file or a local
server rather than a network resource, so the claims they add are the ones that
are missing: a browser this adapter drives does assemble a document, the adapter
reads what it assembled, a real Playwright page satisfies the surface the wiring
is written against, and every request a real page makes goes through the
private-address guard.

Each test skips with a reason rather than failing when the engine is absent, so
that ``pytest -m engine --collect-only`` collects cleanly on a machine with no
engines at all.
"""

from __future__ import annotations

import http.server
import threading
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path
from typing import Any, ClassVar

import httpcore
import pytest
from cruxible_provider_runtime.egress import EgressRecorder
from cruxible_provider_runtime.errors import RefusalCode, RefusalError
from cruxible_provider_web.engines import PlaywrightRenderer

pytestmark = pytest.mark.engine

ASSEMBLING_PAGE = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>Gauge dashboard</title></head>
<body>
  <div id="root"></div>
  <noscript>This dashboard requires JavaScript.</noscript>
  <script>
    // The reading is assembled from parts, so that it exists only in the DOM the
    // script builds and never in the body the origin served.
    document.getElementById("root").innerHTML =
      "<main><h1>Gauge dashboard</h1><p>Newlyn reported a mean sea level of " +
      ["3", "214"].join(".") +
      " metres over the last complete tidal cycle, assembled client-side.</p></main>";
  </script>
</body>
</html>
"""


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
def test_the_renderer_returns_the_assembled_document(tmp_path: Path) -> None:
    page = tmp_path / "dashboard.html"
    page.write_text(ASSEMBLING_PAGE, encoding="utf-8")

    rendered = PlaywrightRenderer().render(
        page.as_uri(), timeout_seconds=30.0, recorder=EgressRecorder()
    )

    assert rendered.engine == "playwright"
    # Present only after the script ran: the assertion fails if the adapter
    # returned the initial response instead of the assembled document.
    assert "3.214" in rendered.html


@pytest.mark.usefixtures("browser_available")
def test_a_real_page_satisfies_the_surface_the_wiring_is_written_against(tmp_path: Path) -> None:
    """The half of the seam the default lane cannot reach.

    ``drive_page`` is written against a protocol, and the default lane drives it
    over a double. That proves the wiring and proves nothing about whether a real
    Playwright page answers ``route``, ``goto``, ``content`` and a navigation
    response the way the protocol says. This is where that is established.

    The page is local, so the recorder must stay empty: a ``file:`` URL names no
    origin, and the egress contract is about who a provider talked to over a
    network.
    """

    page = tmp_path / "dashboard.html"
    page.write_text(ASSEMBLING_PAGE, encoding="utf-8")
    recorder = EgressRecorder()

    rendered = PlaywrightRenderer().render(page.as_uri(), timeout_seconds=30.0, recorder=recorder)

    assert rendered.final_url.endswith("dashboard.html")
    assert recorder.observed() == []
    # The wire half and the assembled half are two artefacts of one navigation,
    # and this page is written so that they differ: the body an origin serves
    # still carries the noscript notice the script replaces.
    assert rendered.body is not None
    assert b"requires JavaScript" in rendered.body
    assert "3.214" not in rendered.body.decode("utf-8")


@pytest.mark.usefixtures("browser_available")
def test_the_assembled_document_extracts_the_way_the_recording_says(tmp_path: Path) -> None:
    """Ties the real renderer to the recorded fixture's expectation.

    The recording claims a rendered dashboard extracts to Markdown carrying the
    assembled reading. This runs the real browser and the real extractor over an
    equivalent page and asserts the same thing, which is what keeps the recording
    from drifting into a description of nothing.
    """

    from cruxible_provider_web.engines import TrafilaturaExtractor

    page = tmp_path / "dashboard.html"
    page.write_text(ASSEMBLING_PAGE, encoding="utf-8")
    rendered = PlaywrightRenderer().render(
        page.as_uri(), timeout_seconds=30.0, recorder=EgressRecorder()
    )
    extraction = TrafilaturaExtractor().extract(rendered.html, url="https://example.test/dashboard")

    assert extraction.kind == "markdown"
    assert "3.214" in extraction.text
    assert "requires JavaScript" not in extraction.text


# -- the private-address guard, in a real browser ----------------------------

PUBLIC_V4 = "93.184.216.34"


class _Origin(http.server.BaseHTTPRequestHandler):
    """One local server standing in for every public host the page names."""

    hits: ClassVar[list[tuple[str, str]]] = []

    def do_GET(self) -> None:
        self.hits.append((self.headers.get("host", ""), self.path))
        port = self.server.server_address[1]
        if self.path == "/start":
            self._answer(302, b"", location="/final")
        elif self.path == "/final":
            self._answer(200, _final_page(port), content_type="text/html; charset=utf-8")
        elif self.path == "/moved.png":
            self._answer(302, b"", location=f"http://cdn.test:{port}/pixel.png")
        elif self.path == "/pixel.png":
            self._answer(200, b"\x89PNG", content_type="image/png")
        elif self.path == "/lib.js":
            body = b"document.title = 'scripted';"
            self._answer(200, body, content_type="application/javascript")
        elif self.path == "/leave":
            body = b"<script>location.href = 'http://169.254.169.254/latest/meta-data/';</script>"
            self._answer(200, body, content_type="text/html")
        else:
            self._answer(404, b"")

    def _answer(self, status: int, body: bytes, **headers: str) -> None:
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name.replace("_", "-"), value)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        del args


def _final_page(port: int) -> bytes:
    return f"""<!DOCTYPE html><html><head><title>start</title>
<script src="http://cdn.test:{port}/lib.js"></script></head><body>
<img src="/moved.png"><p id="metadata">pending</p><p id="lan">pending</p>
<script>
  const mark = (id, outcome) => {{ document.getElementById(id).textContent = outcome; }};
  fetch("http://169.254.169.254/latest/meta-data/")
    .then(() => mark("metadata", "reached"), () => mark("metadata", "blocked"));
  fetch("http://lan.test:{port}/admin")
    .then(() => mark("lan", "reached"), () => mark("lan", "blocked"));
</script></body></html>""".encode()


class _LoopbackBackend(httpcore.NetworkBackend):
    """Opens every pinned connection to the local server, and records the pin."""

    def __init__(self) -> None:
        self._inner = httpcore.SyncBackend()
        self.connected: list[str] = []

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.NetworkStream:
        self.connected.append(host)
        return self._inner.connect_tcp(
            "127.0.0.1", port, timeout=timeout, local_address=local_address
        )

    def sleep(self, seconds: float) -> None:  # pragma: no cover - never retried
        self._inner.sleep(seconds)


def _resolve(host: str, port: int) -> Sequence[str]:
    del port
    return ("10.0.0.8",) if host == "lan.test" else (PUBLIC_V4,)


@pytest.fixture()
def origin() -> Iterator[int]:
    _Origin.hits = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Origin)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.usefixtures("browser_available")
def test_a_real_page_is_routed_through_the_guard(origin: int) -> None:
    """Main-frame redirect, third-party script, subresource redirect, two refusals.

    The names resolve to a public address, and the network backend carries that
    pinned connection to a local server — so the guard runs unmodified and the
    test still needs no network. The metadata address and the name resolving
    privately must be refused without the local server hearing of them.
    """

    recorder = EgressRecorder()
    backend = _LoopbackBackend()

    rendered = PlaywrightRenderer(resolver=_resolve, network_backend=backend).render(
        f"http://origin.test:{origin}/start", timeout_seconds=30.0, recorder=recorder
    )

    assert rendered.final_url == f"http://origin.test:{origin}/final"
    assert rendered.status_code == 200
    assert "scripted" in rendered.html
    assert '<p id="metadata">blocked</p>' in rendered.html
    assert '<p id="lan">blocked</p>' in rendered.html
    assert set(backend.connected) == {PUBLIC_V4}
    assert sorted(_Origin.hits) == [
        (f"cdn.test:{origin}", "/lib.js"),
        (f"cdn.test:{origin}", "/pixel.png"),
        (f"origin.test:{origin}", "/final"),
        (f"origin.test:{origin}", "/moved.png"),
        (f"origin.test:{origin}", "/start"),
    ]
    assert recorder.observed() == [f"http://cdn.test:{origin}", f"http://origin.test:{origin}"]


@pytest.mark.usefixtures("browser_available")
def test_a_real_page_sent_to_a_private_address_refuses_the_run(origin: int) -> None:
    with pytest.raises(RefusalError) as exc:
        PlaywrightRenderer(resolver=_resolve, network_backend=_LoopbackBackend()).render(
            f"http://origin.test:{origin}/leave", timeout_seconds=30.0, recorder=EgressRecorder()
        )

    assert exc.value.code is RefusalCode.PROVIDER_DECLINED
    assert exc.value.refusal.detail["address_class"] == "link_local"
