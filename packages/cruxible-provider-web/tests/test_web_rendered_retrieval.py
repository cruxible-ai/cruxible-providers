"""What a rendered run may claim, and what it must record.

A browser is the one retrieval path that does not go through the instrumented
HTTP client. It opens its own sockets, follows its own redirects, and pulls
whatever the markup names — so nothing it does reaches the run's recorder unless
the adapter asks it to, and nothing it received reaches the receipt unless the
adapter reads it back off the response rather than off the request.

Both of those decisions live in ``drive_page`` rather than inside the engine,
which is what makes this lane possible: the page double below implements the
same surface a Playwright page does, so the wiring is reviewed here rather than
only on a machine with a browser installed. ``tests/test_web_engine.py`` is the
other half — it asserts a real page satisfies that surface.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from cruxible_provider_runtime.egress import EgressRecorder
from cruxible_provider_runtime.protocol import Budgets
from cruxible_provider_runtime.provider_api import ProviderResult, ProviderRunContext
from cruxible_provider_web.engines import RenderedPage, drive_page
from cruxible_provider_web.fetch import CLIENT_SIDE_RENDER, WebFetch
from cruxible_provider_web.recordings import load_recordings

BUDGETS = Budgets(wall_clock_seconds=30.0, output_bytes=1_000_000)
RENDERED_BUCKET = "source_kind=js_rendered;access=public;page_weight=light"

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


class _FakePage:
    """A page double carrying the surface ``drive_page`` drives.

    ``contacts`` is the script this navigation plays back: the ``(event, url)``
    pairs a browser would emit while loading — the redirect hop, the script the
    markup names, the endpoint that script queries. It stands in for the sequence
    of events, not for a browser.
    """

    def __init__(
        self,
        *,
        response: _FakeResponse | None,
        html: str,
        contacts: Sequence[tuple[str, str]] = (),
    ) -> None:
        self._response = response
        self._html = html
        self._contacts = list(contacts)
        self._handlers: dict[str, list[Callable[[Any], None]]] = {}
        self.url = "about:blank"
        self.navigations: list[tuple[str, str, float]] = []

    def on(self, event: str, handler: Callable[[Any], None]) -> None:
        self._handlers.setdefault(event, []).append(handler)

    def goto(self, url: str, *, wait_until: str, timeout: float) -> _FakeResponse | None:
        self.navigations.append((url, wait_until, timeout))
        self.url = url
        for event, contacted in self._contacts:
            for handler in self._handlers.get(event, []):
                handler(_Event(contacted))
        if self._response is not None:
            self.url = self._response.url
        return self._response

    def content(self) -> str:
        return self._html


class _PageRenderer:
    """A renderer that drives the production wiring over a page double."""

    name = "playwright"

    def __init__(self, page: _FakePage) -> None:
        self._page = page

    def render(self, url: str, *, timeout_seconds: float, recorder: EgressRecorder) -> RenderedPage:
        return drive_page(
            self._page,
            url,
            timeout_seconds=timeout_seconds,
            recorder=recorder,
            engine=self.name,
        )


def _redirected_page() -> _FakePage:
    """The reviewer's case: a cross-origin redirect, a third party, a 404."""

    return _FakePage(
        response=_FakeResponse(
            url=SETTLED,
            status=200,
            headers={"Content-Type": "text/html; charset=utf-8", "ETag": '"9f21"'},
            payload=WIRE_BODY,
        ),
        html=ASSEMBLED,
        contacts=[
            ("request", REQUESTED),
            ("response", "https://first.example/report"),
            ("request", SETTLED),
            ("request", "https://cdn.thirdparty.example/bundle.js"),
            ("request", "https://api.readings.example/v1/gauges"),
            # A browser also loads things that have no origin at all. They are
            # not egress and must not be pushed at a recorder that would refuse
            # to normalise them.
            ("request", "data:text/css;base64,Ym9keXt9"),
            ("response", SETTLED),
        ],
    )


def _context(**overrides: Any) -> ProviderRunContext:
    fields: dict[str, Any] = {
        "run_id": "run-rendered",
        "interface_id": "web.fetch",
        "interface_digest": "sha256:" + "aa" * 32,
        "implementation_digest": "sha256:" + "bb" * 32,
        "input_bucket": RENDERED_BUCKET,
        "input": {"url": REQUESTED, "render": True},
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
    return WebFetch(renderer=_PageRenderer(page))(context), recorder


def test_every_origin_the_page_contacts_reaches_the_recorder() -> None:
    """A dynamic declaration is governed by its recording, so it must be complete.

    The redirect destination, the CDN the markup names and the API its script
    queries are all hosts this run talked to, and none of them passes through the
    instrumented client. A receipt naming only the URL the caller submitted
    describes the request rather than the run.
    """

    _, recorder = _run(_redirected_page())

    assert recorder.observed() == [
        "https://api.readings.example",
        "https://cdn.thirdparty.example",
        "https://first.example",
        "https://second.example",
    ]


def test_the_receipt_reports_the_response_the_browser_got() -> None:
    """Not the request the caller wrote, which is what a placeholder amounts to."""

    result, _ = _run(_redirected_page())

    assert result.status == "ok"
    assert result.output is not None
    retrieved = result.output["retrieved"]
    assert retrieved["url"] == REQUESTED
    assert retrieved["final_url"] == SETTLED
    assert retrieved["status_code"] == 200
    assert retrieved["headers"]["etag"] == '"9f21"'
    assert retrieved["source"] == "network"


def test_a_rendered_run_that_settles_on_a_404_is_a_failed_retrieval() -> None:
    """The concrete failure the finding names, in one run.

    A script can repaint a 404 into a page that reads perfectly well. That does
    not make it the resource the caller asked for, and reporting 200 against the
    submitted URL would put the opposite into a Capture.
    """

    page = _FakePage(
        response=_FakeResponse(url=SETTLED, status=404, headers={"content-type": "text/html"}),
        html=ASSEMBLED,
        contacts=[("request", REQUESTED), ("request", SETTLED)],
    )

    result, recorder = _run(page)

    assert result.status == "error"
    assert result.error is not None
    assert result.error.detail["status_code"] == 404
    assert result.error.detail["final_url"] == SETTLED
    # And the run is still fully recorded: a failed retrieval contacted the
    # hosts it contacted.
    assert recorder.observed() == ["https://first.example", "https://second.example"]


def test_the_assembled_document_is_derived_and_the_wire_body_is_retrieved() -> None:
    """The two artefacts of a rendered run, told apart in the output.

    Driven over the packaged recording rather than a double, because the
    recording carries both halves of one real exchange: an empty-root response
    and the DOM a browser built from it. ``retrieved`` must describe the first
    and ``derived`` the second — a receipt that digests the assembled DOM under
    ``retrieved.body_sha256`` is claiming an origin sent something it never sent.
    """

    recording = load_recordings()["dashboard-rendered"]
    assert recording.rendered_body is not None
    context = _context(input={"url": "https://fixture.invalid/dashboard", "render": True})

    result = WebFetch()(context)

    assert result.status == "ok"
    assert result.output is not None
    retrieved = result.output["retrieved"]
    derived = result.output["derived"]

    wire = recording.response.body.encode("utf-8")
    assembled = recording.rendered_body.encode("utf-8")
    assert retrieved["byte_count"] == len(wire)
    assert retrieved["body_sha256"] == "sha256:" + hashlib.sha256(wire).hexdigest()
    assert derived["assembled_document"] == {
        "assembly": CLIENT_SIDE_RENDER,
        "engine": "recorded:dashboard-rendered",
        "byte_count": len(assembled),
        "sha256": "sha256:" + hashlib.sha256(assembled).hexdigest(),
    }
    assert retrieved["body_sha256"] != derived["assembled_document"]["sha256"]


def test_a_plain_fetch_claims_no_assembly() -> None:
    """The negative half: nothing was assembled, so nothing says it was."""

    result = WebFetch()(
        _context(
            input={"url": "https://fixture.invalid/articles/tide-gauge-recalibration"},
            input_bucket="source_kind=static_html;access=public;page_weight=light",
        )
    )

    assert result.status == "ok"
    assert result.output is not None
    assert "assembled_document" not in result.output["derived"]
    assert result.output["retrieved"]["renderer"] is None


def test_both_hooks_are_attached_before_the_navigation_starts() -> None:
    """An origin contacted during ``goto`` is only recorded if the hook predates it.

    The double replays its contacts from inside ``goto``, so a handler attached
    afterwards would see none of them — which is precisely the failure a hook
    wired after navigation would produce against a real browser.
    """

    recorder = EgressRecorder()
    page = _redirected_page()

    rendered = drive_page(
        page, REQUESTED, timeout_seconds=12.0, recorder=recorder, engine="playwright"
    )

    assert page.navigations == [(REQUESTED, "networkidle", 12_000.0)]
    assert "https://cdn.thirdparty.example" in recorder.observed()
    assert rendered.html == ASSEMBLED
    assert rendered.body == WIRE_BODY


def test_a_navigation_with_no_main_frame_response_claims_no_status() -> None:
    """Silence rather than a hopeful 200, and the URL the browser ended on."""

    page = _FakePage(response=None, html=ASSEMBLED, contacts=[("request", REQUESTED)])

    rendered = drive_page(
        page, REQUESTED, timeout_seconds=5.0, recorder=EgressRecorder(), engine="playwright"
    )

    assert rendered.status_code is None
    assert rendered.body is None
    assert rendered.final_url == REQUESTED
