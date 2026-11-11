"""The browser ``web.render.dom`` drives, and the wiring that records what it contacts.

``web.render.dom`` is a fetch (D-R37): a browser navigates to the URL the run
names, runs the page's scripts and pulls the subresources the markup names, and
the serialized DOM it assembles is the Capture. The real engine is Playwright,
which is a browser and is therefore behind the ``browser`` extra, declared by
the ``web.render.dom`` implementation's manifest row. Nothing in the default
lane imports Playwright: recorded renders replay a post-assembly DOM, and an
environment that genuinely lacks the extra refuses rather than crashing on the
import.

**Why the browser wiring is not inside the browser.** A browser is an engine;
getting every host it touched into the run's recorder, and refusing a page that
is not on the public web, is not. Those decisions live in :func:`drive_page`,
over the :class:`BrowserPage` protocol, so that the default lane can execute
them against a double.

**What the private-address guard covers here, and what it does not.** A browser
opens its own sockets and resolves names itself, so the address-pinned HTTP
client ``web.fetch`` uses cannot stand between it and the network.
:func:`drive_page` applies the guard at the two points this adapter controls:
the URL the run names is vetted before the browser navigates (a private,
loopback, link-local or local-only target is refused and the browser is never
sent there), and the address the browser reports for the main-frame response is
judged before the assembled page is returned (a page served from a private
address -- after a redirect, or a name re-pointed between the check and the
browser's own lookup -- is refused rather than captured). That response is the
one the captured DOM belongs to: a page whose main frame moved on to another
document, before or while it was read, is declined (:func:`drive_page`). It
does **not** stop
the browser from sending requests to private addresses along the way: redirect
hops before the final one, the subresources, fetches and WebSockets a page
opens, and a navigation a script starts after the page loaded are sent by the
browser unchecked. Containment of those comes only from the network the browser
runs in.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit

from cruxible_provider_runtime.egress import EgressRecorder
from cruxible_provider_runtime.errors import decline

from .addresses import AddressGuard, literal_address
from .http import USER_AGENT
from .operations import ENGINE_UNAVAILABLE, PROVIDER_DECLINED

__all__ = [
    "CONTACT_SCHEMES",
    "PAGE_CONTACT_EVENTS",
    "BrowserPage",
    "MainFrameResponse",
    "PageRenderer",
    "PlaywrightRenderer",
    "RenderedPage",
    "drive_page",
]


@dataclass(frozen=True)
class RenderedPage:
    """A page after client-side assembly, and the main-frame response under it.

    ``html`` is what a browser *built* -- script output, injected markup, a DOM
    no origin ever sent -- and is the Capture's bytes. ``final_url``,
    ``status_code`` and ``headers`` describe the main-frame response the page was
    assembled from, which go to the coordinate.
    """

    engine: str
    html: str
    final_url: str
    status_code: int
    headers: Mapping[str, str] = field(default_factory=dict)


class MainFrameResponse(Protocol):
    """The slice of a browser's navigation response this plane reads.

    ``request`` is the Playwright ``Request`` the response answers; its
    ``is_navigation_request()`` and ``frame.parent_frame`` say whether the
    response is a main-frame document.
    """

    url: str
    status: int

    @property
    def request(self) -> Any: ...

    def all_headers(self) -> dict[str, str]: ...

    def server_addr(self) -> Mapping[str, Any] | None:
        """The address the browser connected to for this response (Playwright's
        ``{"ipAddress", "port"}``), or ``None`` when it does not say."""
        ...


class BrowserPage(Protocol):
    """The slice of a browser page :func:`drive_page` drives.

    Written down as a protocol rather than left implicit so that the wiring can
    be exercised without a browser. The engine-marked lane is what asserts a real
    Playwright page satisfies it. ``url`` is the main frame's current URL.
    """

    url: str

    def on(self, event: str, handler: Callable[[Any], None]) -> None: ...

    def goto(self, url: str, *, wait_until: str, timeout: float) -> MainFrameResponse | None: ...

    def content(self) -> str: ...


class PageRenderer(Protocol):
    name: str

    def render(
        self, url: str, *, timeout_seconds: float, recorder: EgressRecorder
    ) -> RenderedPage: ...


#: Every scheme a browser reaches a host over: page loads and fetches, and the
#: WebSocket handshakes a page opens.
CONTACT_SCHEMES = frozenset({"http", "https", "ws", "wss"})
_WEBSOCKET_DEFAULT_PORTS = {"ws": 80, "wss": 443}

PAGE_CONTACT_EVENTS = ("request", "response", "websocket")
"""The Playwright ``Page`` events :func:`drive_page` records a contact from.

``request`` and ``response`` cover every HTTP exchange the page makes, its
redirects included. A WebSocket is not one of them: Playwright reports it only
through the page's separate ``websocket`` event, so without that hook a page
streaming from ``wss://stream.third.example`` would assemble into a Capture whose
receipt never names the host. The names are Playwright's own (``Page.on``);
``tests/test_web_engine.py`` holds them against a real browser.
"""


def _record_contact(recorder: EgressRecorder, url: str) -> None:
    """Record ``url`` when it names an origin the egress contract is about.

    A browser also loads ``data:``, ``blob:`` and ``file:`` URLs, and the
    recorder's subject is who a provider talked to over a network. Normalising a
    hostless URL refuses rather than records, so the filter has to be on the way
    in rather than left to the recorder.

    A WebSocket endpoint is recorded under its own scheme, normalised the way an
    HTTP origin is: ``scheme://host[:port]``, path and query dropped, and the
    scheme's default port (80 for ``ws``, 443 for ``wss``) left implicit.
    """

    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in CONTACT_SCHEMES or not parts.hostname:
        return
    if scheme in _WEBSOCKET_DEFAULT_PORTS:
        host = parts.hostname.lower()
        if ":" in host:
            host = f"[{host}]"
        port = parts.port
        if port is not None and port != _WEBSOCKET_DEFAULT_PORTS[scheme]:
            host = f"{host}:{port}"
        url = f"{scheme}://{host}"
    recorder.record(url)


def drive_page(
    page: BrowserPage,
    url: str,
    *,
    timeout_seconds: float,
    recorder: EgressRecorder,
    engine: str,
    guard: AddressGuard,
) -> RenderedPage:
    """Assemble the page at ``url`` in ``page``, recording every origin it contacts.

    Every hook in :data:`PAGE_CONTACT_EVENTS` is attached before the navigation
    and each is load-bearing. A browser contacts hosts the adapter never named --
    the CDN the markup pulls a script from, the API that script queries, the
    redirects those answer with, the WebSocket a page streams over -- and none of
    that passes through an instrumented HTTP client, so without these hooks none
    of it reaches the run's recorder and the receipt understates the run to
    exactly the degree the page was interesting. The main frame is a contact like
    any other. Each event hands its handler an object with the contacted ``url``
    (a Playwright ``Request``, ``Response`` or ``WebSocket``).

    ``guard`` is applied twice (see the module docstring for what that does and
    does not cover): ``url`` is vetted before anything is attached or navigated,
    and the main-frame response's server address is judged before the page's
    content is read.

    **The response and the DOM describe one document.** ``goto`` returns the
    response of the navigation it started, but the page can move on to another
    document before its wait ends -- a script navigating, a refresh -- and the
    DOM read afterwards would then be that other document's, under the first
    one's URL, status, validators and checked address. So every main-frame
    document response is tracked as it arrives, and the page is read only when
    the response ``goto`` returned is still the latest of them and the main frame
    is still on that response's origin (a same-origin ``pushState`` keeps the
    document; a navigation to ``about:blank`` or a ``blob:`` URL does not). That
    state is recorded before any further call into the browser and checked
    again after the served address is looked up and after the read, so a
    document arriving during either call -- the binding delivers events while a
    call is in flight -- is a change, and the page is declined rather than
    captured.
    """

    guard.vet(url)

    def contacted(event: Any) -> None:
        _record_contact(recorder, event.url)

    documents: list[Any] = []

    def document_response(response: Any) -> None:
        if _is_main_frame_document(response):
            documents.append(response)

    for name in PAGE_CONTACT_EVENTS:
        page.on(name, contacted)
    page.on("response", document_response)
    response = page.goto(url, wait_until="networkidle", timeout=timeout_seconds * 1000)
    if response is None:
        # A navigation the browser answered without a response (a same-document
        # move, an about: page) assembled no fetched page; nothing to capture.
        raise decline(
            PROVIDER_DECLINED,
            "the browser got no main-frame response for the page",
            url=url,
            engine=engine,
        )
    # The pairing baseline is taken before any further call into the browser:
    # the sync binding dispatches events while a call is in flight, so a
    # document arriving during the address lookup or the read must be seen as a
    # change against this, never become the baseline itself.
    baseline = (len(documents), page.url)

    def paired() -> bool:
        return (
            bool(documents)
            and documents[-1] is response
            and _same_origin(page.url, response.url)
            and (len(documents), page.url) == baseline
        )

    if not paired():
        raise decline(
            PROVIDER_DECLINED,
            "the page moved on to another document after the navigation it was opened "
            "for, so the page and the response it was served under cannot be paired",
            url=url,
            engine=engine,
        )
    _judge_served(guard, response, url=url, engine=engine)
    if not paired():
        raise decline(
            PROVIDER_DECLINED,
            "the page moved on to another document after the navigation it was opened "
            "for, so the page and the response it was served under cannot be paired",
            url=url,
            engine=engine,
        )
    html = page.content()
    if not paired():
        raise decline(
            PROVIDER_DECLINED,
            "the page's main frame changed while the page was being read",
            url=url,
            engine=engine,
        )
    return RenderedPage(
        engine=engine,
        html=html,
        final_url=response.url,
        status_code=response.status,
        headers={key.lower(): value for key, value in response.all_headers().items()},
    )


def _is_main_frame_document(response: Any) -> bool:
    """Whether ``response`` answers a navigation of the page's main frame.

    Redirect responses of that navigation count too, so the last one is the
    document the frame settled on. Anything that cannot say -- a contact
    double, a request without a frame -- is not a main-frame document.
    """

    try:
        request = response.request
        return bool(request.is_navigation_request()) and request.frame.parent_frame is None
    except Exception:
        return False


def _same_origin(current: str, served: str) -> bool:
    """Whether the main frame's ``current`` URL is still on ``served``'s http(s) origin."""

    def origin(value: str) -> tuple[str, str, int] | None:
        parts = urlsplit(value)
        scheme = parts.scheme.lower()
        if scheme not in {"http", "https"} or not parts.hostname:
            return None
        try:
            port = parts.port
        except ValueError:
            return None
        return scheme, parts.hostname.lower(), port or (443 if scheme == "https" else 80)

    here = origin(current)
    return here is not None and here == origin(served)


def _judge_served(
    guard: AddressGuard, response: MainFrameResponse, *, url: str, engine: str
) -> None:
    """Refuse a main-frame response the browser got from a non-public address.

    The address is the one the browser reports it connected to, so a redirect
    onto a private host, or a name that resolved publicly when :meth:`vet` asked
    and privately when the browser did, is judged on where the bytes actually
    came from. A response whose address the browser does not report, or reports
    unreadably, is declined rather than trusted.
    """

    served = response.server_addr()
    reported = served.get("ipAddress") if served else None
    address = literal_address(reported) if isinstance(reported, str) else None
    host = urlsplit(response.url).hostname
    if address is None or not host:
        raise decline(
            PROVIDER_DECLINED,
            "the browser did not report which address served the page, so it could not "
            "be checked against the private-address guard",
            url=url,
            engine=engine,
        )
    guard.judge(response.url, host.lower(), address)


class PlaywrightRenderer:
    """Client-side assembly with a real browser. Requires the ``browser`` extra.

    The import is inside the method, and its failure is a **typed refusal**
    rather than an ImportError crossing the process boundary as an error. The
    distinction is the one the taxonomy draws: an environment missing the engine
    its implementation declared is not a failed answer, it is an environment that
    diverges from the resolution it was supposed to be -- which is exactly what
    the declared ``engine_unavailable`` refusal says.
    """

    name = "playwright"

    def __init__(self, *, guard: AddressGuard | None = None) -> None:
        # The production guard resolves through the system resolver. The seam
        # lets the engine lane serve its page from a loopback server.
        self._guard = guard or AddressGuard()

    def render(self, url: str, *, timeout_seconds: float, recorder: EgressRecorder) -> RenderedPage:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise decline(
                ENGINE_UNAVAILABLE,
                "this implementation declares the 'browser' extra and the materialized "
                "environment does not carry it",
                required_extra="browser",
                engine=self.name,
            ) from exc

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                # Everything that decides what the run records happens in
                # drive_page, which the default lane executes over a double. This
                # method's whole job is to hand it a real page.
                return drive_page(
                    browser.new_page(user_agent=USER_AGENT),
                    url,
                    timeout_seconds=timeout_seconds,
                    recorder=recorder,
                    engine=self.name,
                    guard=self._guard,
                )
            finally:
                browser.close()
