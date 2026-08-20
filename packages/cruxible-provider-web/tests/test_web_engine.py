"""The opt-in real-engine lane for the web plane.

``pytest -m engine``. Never part of the default run, and never part of the
default CI lane: everything here needs the ``browser`` extra installed *and* the
browser binary downloaded, which is exactly the cost the heavy-engine split
exists to keep out of an ordinary test run.

What the lane is for. The default lane exercises the adapter against a recorded
post-assembly DOM; that proves the adapter, and proves nothing about the
renderer. These tests drive the real renderer, over a local file rather than a
network resource, so the claim they add is the one that is missing: a browser
this adapter drives does assemble a document, and the adapter reads what it
assembled.

Each test skips with a reason rather than failing when the engine is absent, so
that ``pytest -m engine --collect-only`` collects cleanly on a machine with no
engines at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest
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

    rendered = PlaywrightRenderer().render(page.as_uri(), timeout_seconds=30.0)

    assert rendered.engine == "playwright"
    # Present only after the script ran: the assertion fails if the adapter
    # returned the initial response instead of the assembled document.
    assert "3.214" in rendered.html


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
    rendered = PlaywrightRenderer().render(page.as_uri(), timeout_seconds=30.0)
    extraction = TrafilaturaExtractor().extract(rendered.html, url="https://example.test/dashboard")

    assert extraction.kind == "markdown"
    assert "3.214" in extraction.text
    assert "requires JavaScript" not in extraction.text
