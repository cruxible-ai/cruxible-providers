"""What a rendered run may claim, what it may contact, and what it must record.

A browser is the one retrieval path that does not start in the instrumented HTTP
client. It follows its own redirects and pulls whatever the markup names — so
the adapter routes every request the page makes back through the run's guarded
client, answers the browser with what that client retrieved, and reads the
receipt off the main-frame response rather than off the request.

All of that lives in ``drive_page`` and its route gate rather than inside the
engine, which is what makes this lane possible: the page double below implements
the same surface a Playwright page does — it pauses each request on the route
handler and acts on the handler's decision — so the wiring is reviewed here
rather than only on a machine with a browser installed.
``tests/test_web_engine.py`` is the other half: it asserts a real page satisfies
that surface.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from cruxible_provider_runtime.egress import EgressRecorder
from cruxible_provider_runtime.errors import RefusalCode, RefusalError
from cruxible_provider_runtime.protocol import Budgets
from cruxible_provider_runtime.provider_api import ProviderResult, ProviderRunContext
from cruxible_provider_web.addresses import AddressGuard, Resolver
from cruxible_provider_web.engines import RenderedPage, RouteGate, drive_page
from cruxible_provider_web.fetch import CLIENT_SIDE_RENDER, WebFetch
from cruxible_provider_web.http import RecordingClient
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

PUBLIC = "93.184.216.34"
"""What the resolver stub answers for every name it is not told otherwise about."""


# -- the origins -------------------------------------------------------------


class _Origins:
    """Canned answers keyed on ``host + path``, and a record of what was asked."""

    def __init__(self, routes: Mapping[str, httpx.Response]) -> None:
        self._routes = dict(routes)
        self.seen: list[tuple[str, dict[str, str]]] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def hosts_seen(self) -> list[str]:
        return [key.split("/", 1)[0] for key, _ in self.seen]

    def _handle(self, request: httpx.Request) -> httpx.Response:
        key = f"{request.url.host}{request.url.path}"
        self.seen.append((key, {name.lower(): value for name, value in request.headers.items()}))
        answer = self._routes.get(key)
        if answer is None:  # pragma: no cover - defensive
            raise AssertionError(f"the client contacted an unrouted endpoint: {key}")
        return httpx.Response(
            status_code=answer.status_code,
            headers=answer.headers,
            content=answer.content,
            request=request,
        )


def _resolver(answers: Mapping[str, Sequence[str]] | None = None) -> Resolver:
    table = dict(answers or {})

    def resolve(host: str, port: int) -> Sequence[str]:
        del port
        return table.get(host, (PUBLIC,))

    return resolve


def _redirect(location: str, *, status_code: int = 302) -> httpx.Response:
    return httpx.Response(status_code=status_code, headers={"location": location})


def _html(body: bytes = WIRE_BODY, *, status_code: int = 200, **headers: str) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        headers={"content-type": "text/html; charset=utf-8", **headers},
        content=body,
    )


def _asset(body: bytes = b"/* asset */") -> httpx.Response:
    return httpx.Response(status_code=200, headers={"content-type": "text/plain"}, content=body)


# -- the page double ---------------------------------------------------------


@dataclass(frozen=True)
class _Frame:
    parent_frame: _Frame | None = None


MAIN_FRAME = _Frame()


@dataclass
class _Request:
    url: str
    navigation: bool
    frame: _Frame = MAIN_FRAME
    method: str = "GET"
    headers: dict[str, str] = field(
        default_factory=lambda: {
            "user-agent": "page-double",
            "accept": "*/*",
            "accept-encoding": "gzip, br, zstd",
            "cookie": "session=page-double",
        }
    )
    post_data_buffer: bytes | None = None

    def is_navigation_request(self) -> bool:
        return self.navigation


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


class _Route:
    """A paused request, and what the handler decided about it."""

    def __init__(self, request: _Request) -> None:
        self.request = request
        self.outcome: str | None = None
        self.error_code: str | None = None
        self.response: _FakeResponse | None = None

    def abort(self, error_code: str = "failed") -> None:
        self.outcome, self.error_code = "aborted", error_code

    def continue_(self) -> None:
        self.outcome = "continued"

    def fulfill(self, *, status: int, headers: dict[str, str], body: bytes) -> None:
        self.outcome = "fulfilled"
        self.response = _FakeResponse(
            url=self.request.url, status=status, headers=dict(headers), payload=body
        )


class _NavigationAborted(Exception):
    pass


class _FakePage:
    """A page double that pauses every request on the route handler.

    ``goto`` issues the main-frame request; if the handler fulfils it, the page
    "loads" and issues ``subresources``, then ``late_navigation`` if one is set —
    a script sending the page somewhere after load. A main-frame request the
    handler aborts raises from ``goto``, as Playwright does.
    """

    def __init__(
        self,
        *,
        html: str = ASSEMBLED,
        subresources: Sequence[str] = (),
        late_navigation: str | None = None,
        main_frame_response: bool = True,
    ) -> None:
        self._html = html
        self._subresources = list(subresources)
        self._late_navigation = late_navigation
        self._main_frame_response = main_frame_response
        self._handler: Any = None
        self.url = "about:blank"
        self.navigations: list[tuple[str, str, float]] = []
        self.routes: list[_Route] = []

    def route(self, url: str, handler: Any) -> None:
        assert url == "**/*"
        self._handler = handler

    def goto(self, url: str, *, wait_until: str, timeout: float) -> _FakeResponse | None:
        self.navigations.append((url, wait_until, timeout))
        main = self._dispatch(_Request(url, navigation=True))
        if main.response is None:
            raise _NavigationAborted(f"net::ERR_ABORTED at {url}")
        self.url = url
        for subresource in self._subresources:
            self._dispatch(_Request(subresource, navigation=False))
        if self._late_navigation is not None:
            late, self._late_navigation = self._late_navigation, None
            self._dispatch(_Request(late, navigation=True))
        return main.response if self._main_frame_response else None

    def content(self) -> str:
        return self._html

    def route_for(self, url: str) -> _Route:
        return next(route for route in self.routes if route.request.url == url)

    def _dispatch(self, request: _Request) -> _Route:
        assert self._handler is not None, "a request was made before the gate was installed"
        route = _Route(request)
        self._handler(route)
        assert route.outcome is not None, "the gate left a request paused"
        self.routes.append(route)
        return route


class _PageRenderer:
    """A renderer that drives the production wiring over a page double."""

    name = "playwright"

    def __init__(
        self, page: _FakePage, origins: _Origins, resolver: Resolver | None = None
    ) -> None:
        self._page = page
        self._origins = origins
        self._resolver = resolver or _resolver()

    def render(self, url: str, *, timeout_seconds: float, recorder: EgressRecorder) -> RenderedPage:
        client = RecordingClient(
            recorder,
            timeout_seconds=timeout_seconds,
            transport=self._origins.transport(),
            guard=AddressGuard(self._resolver),
        )
        with client:
            return drive_page(
                self._page,
                url,
                timeout_seconds=timeout_seconds,
                client=client,
                engine=self.name,
            )


SUBRESOURCES = (
    "https://cdn.thirdparty.example/bundle.js",
    "https://api.readings.example/v1/gauges",
    # A browser also loads things that have no origin at all. They are not
    # egress, and there is no address for the guard to judge.
    "data:text/css;base64,Ym9keXt9",
)


def _redirected_origins(*, settled: httpx.Response | None = None) -> _Origins:
    """The reviewer's case: a cross-origin redirect, a third party, an API."""

    return _Origins(
        {
            "first.example/report": _redirect(SETTLED),
            "second.example/report": settled or _html(etag='"9f21"'),
            "cdn.thirdparty.example/bundle.js": _asset(),
            "api.readings.example/v1/gauges": _asset(b"[]"),
        }
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


def _run(
    page: _FakePage, origins: _Origins, resolver: Resolver | None = None, **overrides: Any
) -> tuple[ProviderResult, EgressRecorder]:
    recorder = EgressRecorder()
    context = _context(egress=recorder, **overrides)
    return WebFetch(renderer=_PageRenderer(page, origins, resolver))(context), recorder


def test_every_origin_the_page_contacts_reaches_the_recorder() -> None:
    """A dynamic declaration is governed by its recording, so it must be complete.

    The redirect destination, the CDN the markup names and the API its script
    queries are all hosts this run talked to. Every one of them was contacted
    through the run's client, so every one of them is in the recorder; the
    ``data:`` URL contacted nobody and is not.
    """

    page = _FakePage(subresources=SUBRESOURCES)
    _, recorder = _run(page, _redirected_origins())

    assert recorder.observed() == [
        "https://api.readings.example",
        "https://cdn.thirdparty.example",
        "https://first.example",
        "https://second.example",
    ]
    assert page.route_for("data:text/css;base64,Ym9keXt9").outcome == "continued"


def test_the_receipt_reports_the_response_the_browser_got() -> None:
    """Not the request the caller wrote, which is what a placeholder amounts to."""

    result, _ = _run(_FakePage(subresources=SUBRESOURCES), _redirected_origins())

    assert result.status == "ok"
    assert result.output is not None
    retrieved = json.loads(base64.b64decode(result.output["content_base64"]))["retrieved"]
    assert retrieved["url"] == REQUESTED
    assert retrieved["final_url"] == SETTLED
    assert retrieved["status_code"] == 200
    assert retrieved["headers"]["etag"] == '"9f21"'
    assert retrieved["source"] == "network"
    assert base64.b64decode(retrieved["body_base64"]) == WIRE_BODY


def test_a_rendered_run_that_settles_on_a_404_is_a_failed_retrieval() -> None:
    """The concrete failure the finding names, in one run.

    A script can repaint a 404 into a page that reads perfectly well. That does
    not make it the resource the caller asked for, and reporting 200 against the
    submitted URL would put the opposite into a Capture.
    """

    result, recorder = _run(
        _FakePage(), _redirected_origins(settled=_html(b"not found", status_code=404))
    )

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
    retrieved = json.loads(base64.b64decode(result.output["content_base64"]))["retrieved"]
    derived = json.loads(base64.b64decode(result.output["content_base64"]))["derived"]

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
    assert (
        "assembled_document"
        not in json.loads(base64.b64decode(result.output["content_base64"]))["derived"]
    )
    assert (
        json.loads(base64.b64decode(result.output["content_base64"]))["retrieved"]["renderer"]
        is None
    )


def test_the_gate_is_installed_before_the_navigation_starts() -> None:
    """A request made before the gate exists would bypass it.

    The double refuses to dispatch a request with no handler installed, so a
    gate wired after ``goto`` fails here — which is precisely the failure it
    would be against a real browser. A main-frame redirect is taken by a second
    navigation, through the gate again.
    """

    recorder = EgressRecorder()
    page = _FakePage(subresources=SUBRESOURCES)
    origins = _redirected_origins()

    with RecordingClient(
        recorder,
        timeout_seconds=12.0,
        transport=origins.transport(),
        guard=AddressGuard(_resolver()),
    ) as client:
        rendered = drive_page(
            page, REQUESTED, timeout_seconds=12.0, client=client, engine="playwright"
        )

    assert page.navigations == [
        (REQUESTED, "networkidle", 12_000.0),
        (SETTLED, "networkidle", 12_000.0),
    ]
    assert page.route_for(REQUESTED).error_code == "aborted"
    assert "https://cdn.thirdparty.example" in recorder.observed()
    assert rendered.html == ASSEMBLED
    assert rendered.body == WIRE_BODY
    assert rendered.final_url == SETTLED


def test_a_navigation_with_no_main_frame_response_claims_no_status() -> None:
    """Silence rather than a hopeful 200, and the URL the browser ended on."""

    page = _FakePage(main_frame_response=False)
    origins = _Origins({"first.example/report": _html()})

    with RecordingClient(
        EgressRecorder(),
        timeout_seconds=5.0,
        transport=origins.transport(),
        guard=AddressGuard(_resolver()),
    ) as client:
        rendered = drive_page(
            page, REQUESTED, timeout_seconds=5.0, client=client, engine="playwright"
        )

    assert rendered.status_code is None
    assert rendered.body is None
    assert rendered.final_url == REQUESTED


def test_the_browser_is_answered_with_decoded_bytes_and_no_credentials_travel_a_redirect() -> None:
    """What the gate forwards, and what it hands back.

    The client decodes what it receives, so the browser must not be told the
    body is still encoded; and a subresource redirect is followed without the
    page's cookie, because the next hop is somebody the page never chose.
    """

    page = _FakePage(subresources=["https://cdn.thirdparty.example/moved.js"])
    origins = _Origins(
        {
            "first.example/report": _html(**{"content-encoding": "identity"}),
            "cdn.thirdparty.example/moved.js": _redirect("https://cdn2.thirdparty.example/x.js"),
            "cdn2.thirdparty.example/x.js": _asset(b"moved"),
        }
    )

    result, _ = _run(page, origins)

    assert result.status == "ok"
    main = page.route_for(REQUESTED)
    assert main.response is not None
    assert "content-encoding" not in main.response.headers
    moved = page.route_for("https://cdn.thirdparty.example/moved.js")
    assert moved.outcome == "fulfilled"
    assert moved.response is not None and moved.response.payload == b"moved"
    first_hop, second_hop = (headers for key, headers in origins.seen if key.startswith("cdn"))
    assert first_hop["cookie"] == "session=page-double"
    assert "cookie" not in second_hop
    # Negotiated by the client, never inherited from the browser.
    assert "zstd" not in first_hop["accept-encoding"]


# -- the private-address guard, on the rendered path ------------------------


@pytest.mark.parametrize(
    ("subresource", "answers"),
    [
        ("http://169.254.169.254/latest/meta-data/", {}),
        ("http://[::1]:8080/admin", {}),
        ("https://intranet.example/dashboard", {"intranet.example": ("10.0.0.7",)}),
        ("https://localhost/admin", {}),
    ],
    ids=["literal-link-local", "literal-v6-loopback", "resolved-private", "localhost-name"],
)
def test_a_subresource_on_a_private_address_is_never_sent(
    subresource: str, answers: dict[str, tuple[str, ...]]
) -> None:
    """Blocked at the gate, before the request leaves; the page carries on."""

    page = _FakePage(subresources=[subresource, "https://cdn.thirdparty.example/bundle.js"])
    origins = _Origins(
        {"first.example/report": _html(), "cdn.thirdparty.example/bundle.js": _asset()}
    )

    result, recorder = _run(page, origins, _resolver(answers))

    assert result.status == "ok"
    blocked = page.route_for(subresource)
    assert (blocked.outcome, blocked.error_code) == ("aborted", "blockedbyclient")
    assert origins.hosts_seen() == ["first.example", "cdn.thirdparty.example"]
    assert recorder.observed() == ["https://cdn.thirdparty.example", "https://first.example"]


def test_a_subresource_redirect_to_a_private_address_is_never_followed() -> None:
    page = _FakePage(subresources=["https://cdn.thirdparty.example/bounce.js"])
    origins = _Origins(
        {
            "first.example/report": _html(),
            "cdn.thirdparty.example/bounce.js": _redirect("http://169.254.169.254/latest/"),
        }
    )

    result, recorder = _run(page, origins)

    assert result.status == "ok"
    assert page.route_for("https://cdn.thirdparty.example/bounce.js").outcome == "aborted"
    assert "169.254.169.254" not in origins.hosts_seen()
    assert "http://169.254.169.254" not in recorder.observed()


def _assert_refused_as_private(result: ProviderResult) -> None:
    assert result.status == "refused"
    assert result.refusal is not None
    assert result.refusal.code is RefusalCode.PROVIDER_DECLINED
    assert result.refusal.detail["address_class"]


def test_a_main_frame_on_a_private_address_refuses_the_run() -> None:
    page = _FakePage()
    origins = _Origins({})

    result, recorder = _run(page, origins, _resolver({"first.example": ("192.168.1.1",)}))

    _assert_refused_as_private(result)
    assert result.refusal is not None
    assert result.refusal.detail["address_class"] == "private"
    assert origins.seen == []
    assert recorder.observed() == []


def test_a_main_frame_redirect_to_a_private_address_refuses_the_run() -> None:
    page = _FakePage()
    origins = _Origins({"first.example/report": _redirect("http://127.0.0.1:8080/admin")})

    result, recorder = _run(page, origins)

    _assert_refused_as_private(result)
    assert origins.hosts_seen() == ["first.example"]
    assert page.navigations == [(REQUESTED, "networkidle", 24_000.0)]
    assert recorder.observed() == ["https://first.example"]


def test_a_script_navigating_the_page_to_a_private_address_refuses_the_run() -> None:
    """The main frame can be sent somewhere after load; that is a main-frame request too."""

    page = _FakePage(late_navigation="http://169.254.169.254/latest/meta-data/")
    origins = _Origins({"first.example/report": _html()})

    result, _ = _run(page, origins)

    _assert_refused_as_private(result)
    assert "169.254.169.254" not in origins.hosts_seen()


def test_the_gate_answers_every_request_it_is_handed() -> None:
    """A handler that raises leaves the request paused until the run times out."""

    gate = RouteGate(
        RecordingClient(
            EgressRecorder(),
            timeout_seconds=1.0,
            transport=_Origins({}).transport(),
            guard=AddressGuard(_resolver()),
        )
    )
    route = _Route(_Request("https://first.example/unrouted", navigation=False))

    gate(route)

    assert route.outcome == "aborted"
    assert gate.failure is None


def test_a_refusal_detail_never_carries_more_than_the_origin() -> None:
    """The path and query of a refused URL stay out of the receipt."""

    page = _FakePage()
    result, _ = _run(
        page,
        _Origins({}),
        _resolver({"first.example": ("10.1.2.3",)}),
        input={"url": "https://first.example/report?token=abc", "render": True},
    )

    _assert_refused_as_private(result)
    assert result.refusal is not None
    assert result.refusal.detail["url"] == "https://first.example"
    assert "token" not in json.dumps(result.refusal.detail)


def test_refusals_are_typed_runtime_refusals() -> None:
    with pytest.raises(RefusalError):
        AddressGuard(_resolver()).vet("http://127.0.0.1/")
