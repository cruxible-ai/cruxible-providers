"""The opt-in real-engine lane for the web plane.

``pytest -m engine``. Never part of the default run, and never part of the
default CI lane: everything here needs the ``browser`` extra installed *and* the
browser binary downloaded, which is exactly the cost the heavy-engine split
exists to keep out of an ordinary test run.

What the lane is for. The default lane exercises ``web.render`` against a
recorded post-assembly DOM, and the wiring that serves the cited bytes and
records every origin against a page double; that proves the adapter, and proves
nothing about the renderer. These tests drive the real renderer over a cited
document served by the route (no network resource), so the claims they add are
the ones that are missing: a browser this adapter drives does assemble the cited
document, the adapter reads what it assembled, and a real Playwright page
satisfies the surface the wiring is written against.

Each test skips with a reason rather than failing when the engine is absent, so
that ``pytest -m engine --collect-only`` collects cleanly on a machine with no
engines at all.
"""

from __future__ import annotations

import pytest
from cruxible_provider_runtime.egress import EgressRecorder
from cruxible_provider_web.engines import PlaywrightRenderer

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


CITED_URL = "https://example.test/dashboard"
CITED = ASSEMBLING_PAGE.encode("utf-8")


def _render(recorder: EgressRecorder | None = None):  # type: ignore[no-untyped-def]
    return PlaywrightRenderer().render(
        CITED_URL,
        document=CITED,
        content_type="text/html; charset=utf-8",
        timeout_seconds=30.0,
        recorder=recorder or EgressRecorder(),
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
def test_the_renderer_returns_the_assembled_document() -> None:
    rendered = _render()

    assert rendered.engine == "playwright"
    # Present only after the script ran: the assertion fails if the adapter
    # returned the initial response instead of the assembled document.
    assert "3.214" in rendered.html


@pytest.mark.usefixtures("browser_available")
def test_a_real_page_satisfies_the_surface_the_wiring_is_written_against() -> None:
    """The half of the seam the default lane cannot reach.

    ``drive_page`` is written against a protocol, and the default lane drives it
    over a double. That proves the wiring and proves nothing about whether a real
    Playwright page answers ``on``, ``goto``, ``content`` and a navigation
    response the way the protocol says. This is where that is established.

    The main frame is served from the cited bytes and the page pulls nothing, so
    the recorder must stay empty: the egress contract is about who a provider
    talked to over a network.
    """

    recorder = EgressRecorder()
    rendered = _render(recorder)

    assert rendered.final_url == CITED_URL
    assert recorder.observed() == []
    # The wire half and the assembled half are two artefacts of one navigation,
    # and this page is written so that they differ: the body an origin serves
    # still carries the noscript notice the script replaces.
    assert rendered.body is not None
    assert b"requires JavaScript" in rendered.body
    assert "3.214" not in rendered.body.decode("utf-8")


@pytest.mark.usefixtures("browser_available")
def test_the_assembled_document_extracts_the_way_the_recording_says() -> None:
    """Ties the real renderer to the recorded fixture's expectation.

    The recording claims a rendered dashboard extracts to Markdown carrying the
    assembled reading. This runs the real browser and the real extractor over an
    equivalent page and asserts the same thing, which is what keeps the recording
    from drifting into a description of nothing.
    """

    from cruxible_provider_web.engines import TrafilaturaExtractor

    rendered = _render()
    extraction = TrafilaturaExtractor().extract(rendered.html, url=CITED_URL)

    assert extraction.kind == "markdown"
    assert "3.214" in extraction.text
    assert "requires JavaScript" not in extraction.text
