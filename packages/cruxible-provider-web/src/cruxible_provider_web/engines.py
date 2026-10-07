"""The two engine seams of the web plane, and their real implementations.

An engine is anything that turns retrieved material into derived material, or
that produces material a plain HTTP GET cannot. There are two here:

``HtmlExtractor``
    Main-content extraction. The real one is trafilatura, which is a **base**
    dependency: it is pure Python over an lxml wheel, so keeping it out of the
    default lane would buy a few megabytes and cost the lane its only real
    derivation.

``PageRenderer``
    Client-side assembly. The real one is Playwright, which is a browser and is
    therefore behind the ``browser`` extra, declared by the ``web.fetch``
    implementation's manifest. Nothing in the default lane imports it: the
    js_rendered fixtures replay a recorded post-assembly DOM, and an environment
    that genuinely lacks the extra refuses rather than crashing on the import.

Both seams are injectable, and both defaults are the production spelling. The
injection exists so a test can hold one variable still, not so a test can
replace the thing under test.

**Why the browser wiring is not inside the browser.** A browser is an engine;
deciding what a rendered run is allowed to claim, what it is allowed to contact,
and getting every host it touched into the run's recorder, is not. Those
decisions live in :func:`drive_page`, over the :class:`BrowserPage` protocol, so
that the default lane can execute them against a double. Wiring only a machine
with a browser installed can run is wiring nobody reviews, and the receipt is
exactly what it decides.

**The browser never opens a connection of its own.** Every request a page makes
— the navigation, each redirect hop, every subresource — is routed through the
same guarded, address-pinned client a plain fetch uses, and answered from what
that client retrieved. A private target is refused there before anything is
sent, exactly as it is on the plain path, and a main-frame refusal refuses the
run. Routing does not see everything a browser can do — a WebSocket, a
preconnect — so the browser is also launched against a proxy that accepts no
connections and with non-proxied UDP disabled: whatever the routing cannot see
has nowhere to go.
"""

from __future__ import annotations

import socket
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpcore
from cruxible_provider_runtime.egress import EgressRecorder
from cruxible_provider_runtime.errors import RefusalCode, RefusalError, refuse

from .addresses import AddressGuard, Resolver
from .http import (
    MAX_REDIRECTS,
    REDIRECT_STATUSES,
    USER_AGENT,
    HttpResponse,
    PinnedTransport,
    RecordingClient,
    _next_hop,
)
from .interfaces import MAX_RESPONSE_BYTES

__all__ = [
    "BrowserPage",
    "BrowserRequest",
    "BrowserRoute",
    "Extraction",
    "HtmlExtractor",
    "MainFrameResponse",
    "PageRenderer",
    "PlaywrightRenderer",
    "RenderedPage",
    "RouteGate",
    "TrafilaturaExtractor",
    "drive_page",
]


@dataclass(frozen=True)
class Extraction:
    """Derived material. Never observed, whatever the extractor thinks."""

    engine: str
    kind: str
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RenderedPage:
    """A document after client-side assembly, and the exchange it came out of.

    The two halves are separate fields because they are two different kinds of
    claim. ``html`` is what a browser *built* — script output, injected markup, a
    DOM no origin ever sent — and is derived material under every contract.
    ``final_url``, ``status_code``, ``headers`` and ``body`` are the main-frame
    response the browser actually received, and they are the only part of a
    rendered run that records an exchange. Answering with the requested URL and a
    hopeful 200 instead is how a cross-origin redirect ending on a 404 that a
    script repaints reaches a receipt as a successful fetch of the URL that was
    asked for.
    """

    engine: str
    html: str
    final_url: str
    status_code: int | None = None
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes | None = None
    """The main-frame response body, when the browser could produce one.

    ``None`` rather than ``b""`` when it could not: an empty body and an
    unavailable one are different facts, and a digest over the second would be a
    fabrication of the first.
    """


class MainFrameResponse(Protocol):
    """The slice of a browser's navigation response this plane reads."""

    url: str
    status: int

    def all_headers(self) -> dict[str, str]: ...

    def body(self) -> bytes: ...


class BrowserFrame(Protocol):
    """The slice of a frame this plane reads: whether it is the top one."""

    @property
    def parent_frame(self) -> BrowserFrame | None: ...


class BrowserRequest(Protocol):
    """The slice of a browser request the route gate reads."""

    @property
    def url(self) -> str: ...

    @property
    def method(self) -> str: ...

    @property
    def headers(self) -> dict[str, str]: ...

    @property
    def post_data_buffer(self) -> bytes | None: ...

    @property
    def frame(self) -> BrowserFrame: ...

    def is_navigation_request(self) -> bool: ...


class BrowserRoute(Protocol):
    """A request the browser has paused, waiting for the gate's decision."""

    @property
    def request(self) -> BrowserRequest: ...

    def abort(self, error_code: str = ...) -> None: ...

    def continue_(self) -> None: ...

    def fulfill(self, *, status: int, headers: dict[str, str], body: bytes) -> None: ...


class BrowserPage(Protocol):
    """The slice of a browser page :func:`drive_page` drives.

    Written down as a protocol rather than left implicit so that the wiring can
    be exercised without a browser. The engine-marked lane is what asserts a real
    Playwright page satisfies it.
    """

    @property
    def url(self) -> str: ...

    def route(self, url: str, handler: Callable[[BrowserRoute], None]) -> None: ...

    def goto(self, url: str, *, wait_until: str, timeout: float) -> MainFrameResponse | None: ...

    def content(self) -> str: ...


class HtmlExtractor(Protocol):
    name: str

    def extract(self, html: str, *, url: str) -> Extraction: ...


class PageRenderer(Protocol):
    name: str

    def render(
        self, url: str, *, timeout_seconds: float, recorder: EgressRecorder
    ) -> RenderedPage: ...


_UNFORWARDED_REQUEST_HEADERS = frozenset(
    {
        # Hop-by-hop, or describing a connection the browser never opened.
        "host",
        "connection",
        "keep-alive",
        "proxy-connection",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "content-length",
        # The client decodes what it receives, so it negotiates what it can
        # decode rather than inheriting the browser's list.
        "accept-encoding",
    }
)
_CREDENTIAL_HEADERS = frozenset({"cookie", "authorization"})
_UNFULFILLED_RESPONSE_HEADERS = frozenset(
    # The body handed back to the browser is already decoded and complete.
    {"content-encoding", "content-length", "transfer-encoding", "connection", "keep-alive"}
)


def _forwardable(headers: Mapping[str, str]) -> dict[str, str]:
    return {
        name: value
        for name, value in headers.items()
        if name.lower() not in _UNFORWARDED_REQUEST_HEADERS
    }


def _is_main_frame_navigation(request: BrowserRequest) -> bool:
    try:
        return request.is_navigation_request() and request.frame.parent_frame is None
    except Exception:  # pragma: no cover - a request with no frame (a worker's)
        return False


class RouteGate:
    """The route handler every request a rendered page makes passes through.

    Each request is vetted and sent by the guarded client, and the browser is
    answered with what that client got back — so the browser's own networking
    never opens a socket, and a private target is refused before anything is
    sent to it, exactly as on the plain path.

    Redirects are where a route handler would otherwise lose sight of a hop: a
    browser follows a redirect it was answered with without routing the next
    request. So a redirect never reaches the browser. A **main-frame** redirect
    is recorded in :attr:`redirect` and the navigation aborted, and
    :func:`drive_page` navigates to the destination itself — through this gate
    again, with the address bar, relative URLs and the receipt all naming where
    the page actually is. A **subresource** redirect is followed here, hop by
    hop, each hop vetted, with credentials dropped after the first.

    A main-frame request the gate refuses is kept in :attr:`failure`, and
    :func:`drive_page` refuses the run with it. A refused subresource is aborted
    and the page carries on without it.
    """

    def __init__(self, client: RecordingClient, *, cap_bytes: int = MAX_RESPONSE_BYTES) -> None:
        self._client = client
        self._cap_bytes = cap_bytes
        self.redirect: str | None = None
        self.failure: Exception | None = None

    def take_redirect(self) -> str | None:
        """The main-frame redirect the last navigation was answered with, once."""

        redirect, self.redirect = self.redirect, None
        return redirect

    def __call__(self, route: BrowserRoute) -> None:
        request = route.request
        if urlsplit(request.url).scheme not in {"http", "https"}:
            # data:, blob:, file: — nothing crosses a network, so nothing is
            # for the address guard to judge.
            route.continue_()
            return
        main = _is_main_frame_navigation(request)
        try:
            response = self._client.exchange(
                request.method,
                request.url,
                headers=_forwardable(request.headers),
                content=request.post_data_buffer,
                cap_bytes=self._cap_bytes,
            )
            location = response.headers.get("location")
            if response.status_code in REDIRECT_STATUSES and location:
                destination = _next_hop(request.url, location, credentialed=False)
                if main:
                    self.redirect = destination
                    route.abort("aborted")
                    return
                response = self._follow(request, response.status_code, destination)
        except Exception as exc:
            # Broad on purpose: a handler that raises leaves the request paused
            # until the navigation times out. The refusal travels on for a
            # main-frame request; a subresource is simply not served.
            if main:
                self.failure = exc
            route.abort("blockedbyclient" if isinstance(exc, RefusalError) else "failed")
            return
        route.fulfill(
            status=response.status_code,
            headers={
                name: value
                for name, value in response.headers.items()
                if name not in _UNFULFILLED_RESPONSE_HEADERS
            },
            body=response.body,
        )

    def _follow(self, request: BrowserRequest, status: int, destination: str) -> HttpResponse:
        headers = {
            name: value
            for name, value in _forwardable(request.headers).items()
            if name.lower() not in _CREDENTIAL_HEADERS
        }
        method, content = request.method, request.post_data_buffer
        for _ in range(MAX_REDIRECTS):
            if status not in {307, 308}:
                method, content = "GET", None
            response = self._client.exchange(
                method, destination, headers=headers, content=content, cap_bytes=self._cap_bytes
            )
            location = response.headers.get("location")
            if response.status_code not in REDIRECT_STATUSES or not location:
                return response
            status = response.status_code
            destination = _next_hop(destination, location, credentialed=False)
        raise refuse(
            RefusalCode.REDIRECT_LIMIT,
            f"a subresource redirect chain did not settle within {MAX_REDIRECTS} hops",
            url=request.url,
            max_redirects=MAX_REDIRECTS,
        )


def drive_page(
    page: BrowserPage,
    url: str,
    *,
    timeout_seconds: float,
    client: RecordingClient,
    engine: str,
) -> RenderedPage:
    """Navigate ``page`` to ``url`` with every request routed through ``client``.

    The gate is installed before the navigation starts, so nothing the page does
    precedes it. ``client`` is the run's guarded client: it records every request
    it sends into the run's recorder, so the receipt names exactly the origins
    this run contacted — the redirect it followed, the CDN its markup pulls a
    script from, the API that script queries — and nothing that was refused.

    What comes back is the main-frame response as the browser saw it, never the
    request as the caller wrote it.
    """

    gate = RouteGate(client)
    page.route("**/*", gate)
    target = url
    for _ in range(MAX_REDIRECTS + 1):
        navigation_error: Exception | None = None
        response: MainFrameResponse | None = None
        try:
            response = page.goto(target, wait_until="networkidle", timeout=timeout_seconds * 1000)
        except Exception as exc:
            navigation_error = exc
        if gate.failure is not None:
            # Also reached when the refused navigation came later than ``goto``
            # — a script sending the page somewhere — and so raised nothing.
            raise gate.failure from navigation_error
        redirect = gate.take_redirect()
        if redirect is None:
            if navigation_error is not None:
                raise navigation_error
            break
        # Vetted here as well as by the gate, so that a redirect onto a private
        # address is refused without asking the browser to go there at all.
        client.vet(redirect)
        target = redirect
    else:
        raise refuse(
            RefusalCode.REDIRECT_LIMIT,
            f"the redirect chain did not settle within {MAX_REDIRECTS} hops",
            url=url,
            next_url=target,
            max_redirects=MAX_REDIRECTS,
        )

    html = page.content()
    if response is None:
        # A navigation with no main-frame response — a same-document navigation,
        # a download — leaves nothing true to say about the wire, so nothing is
        # said about it. Where the browser ended up is still something it
        # observed.
        return RenderedPage(engine=engine, html=html, final_url=page.url)
    return RenderedPage(
        engine=engine,
        html=html,
        final_url=response.url,
        status_code=response.status,
        headers={key.lower(): value for key, value in response.all_headers().items()},
        body=_main_frame_body(response),
    )


def _main_frame_body(response: MainFrameResponse) -> bytes | None:
    """The bytes behind a navigation response, or ``None`` when there are none.

    A browser cannot always produce them: a body it already consumed and evicted
    is gone, and asking for one raises rather than answering. ``None`` says the
    run has nothing to report there, which is the truth; ``b""`` would put a
    digest of nothing into a receipt as if an origin had sent it.
    """

    try:
        return response.body()
    except Exception:  # pragma: no cover - depends on the browser's cache
        return None


class TrafilaturaExtractor:
    """Main-content extraction with trafilatura, emitting Markdown.

    ``favor_precision`` is on: an adapter feeding a governed Capture should drop
    a boilerplate paragraph rather than admit one, because the downstream cost of
    a navigation menu inside an extracted document is paid by every claim built
    on it.

    The document is parsed **once** and the parsed tree is used for both the
    metadata pass and the body pass. Trafilatura reaches Markdown only through
    ``extract`` and metadata only through ``extract_metadata``, so two calls are
    unavoidable; two *parses* are not, and two parses could disagree with each
    other about what the document is.
    """

    name = "trafilatura"

    def extract(self, html: str, *, url: str) -> Extraction:
        # Imported here rather than at module scope: ``search.web`` lives in the
        # same distribution, never extracts anything, and should not pay for an
        # lxml import to answer a query.
        from trafilatura import extract as extract_text
        from trafilatura import extract_metadata
        from trafilatura.utils import load_html

        tree = load_html(html)
        if tree is None:
            raise refuse(
                RefusalCode.PROVIDER_DECLINED,
                "the retrieved document could not be parsed as HTML",
                url=url,
                engine=self.name,
            )
        metadata = extract_metadata(tree)
        text = extract_text(
            tree,
            url=url,
            output_format="markdown",
            with_metadata=False,
            include_comments=False,
            include_tables=True,
            favor_precision=True,
        )
        if not text:
            raise refuse(
                RefusalCode.PROVIDER_DECLINED,
                "no main content could be extracted from the retrieved document",
                url=url,
                engine=self.name,
            )
        return Extraction(
            engine=self.name,
            kind="markdown",
            text=text,
            metadata={
                "title": getattr(metadata, "title", None),
                "author": getattr(metadata, "author", None),
                "published": getattr(metadata, "date", None),
                "sitename": getattr(metadata, "sitename", None),
                "language": getattr(metadata, "language", None),
            },
        )


@contextmanager
def _closed_proxy() -> Iterator[str]:
    """A proxy address that refuses every connection, for as long as it is held.

    A socket bound and never listened on: the port is reserved for the duration,
    so nothing else can take it, and a connection to it is refused at once.
    """

    sink = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sink.bind(("127.0.0.1", 0))
        yield f"http://127.0.0.1:{sink.getsockname()[1]}"
    finally:
        sink.close()


class PlaywrightRenderer:
    """Client-side assembly with a real browser. Requires the ``browser`` extra.

    The import is inside the method, and its failure is a **typed refusal**
    rather than an ImportError crossing the process boundary as an error. The
    distinction is the one the taxonomy draws: an environment missing the engine
    its implementation declared is not a failed answer, it is an environment that
    diverges from the resolution it was supposed to be — which is exactly what
    ``environment_divergence`` names.

    ``resolver`` and ``network_backend`` are the address guard's seams, so a
    test can drive the real browser through the real gate without real DNS or a
    public origin. Neither loosens the guard.
    """

    name = "playwright"

    def __init__(
        self,
        *,
        resolver: Resolver | None = None,
        network_backend: httpcore.NetworkBackend | None = None,
    ) -> None:
        self._resolver = resolver
        self._network_backend = network_backend

    def render(self, url: str, *, timeout_seconds: float, recorder: EgressRecorder) -> RenderedPage:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise refuse(
                RefusalCode.ENVIRONMENT_DIVERGENCE,
                "this implementation declares the 'browser' extra and the materialized "
                "environment does not carry it",
                required_extra="browser",
                engine=self.name,
            ) from exc

        guard = AddressGuard(self._resolver)
        client = RecordingClient(
            recorder,
            timeout_seconds=timeout_seconds,
            transport=PinnedTransport(guard, network_backend=self._network_backend),
            guard=guard,
        )
        with client:
            if urlsplit(url).scheme in {"http", "https"}:
                # Refused before a browser is launched for it.
                client.vet(url)
            with _closed_proxy() as proxy, sync_playwright() as playwright:
                browser = playwright.chromium.launch(
                    headless=True,
                    proxy={"server": proxy, "bypass": "<-loopback>"},
                    args=["--force-webrtc-ip-handling-policy=disable_non_proxied_udp"],
                )
                try:
                    context = browser.new_context(user_agent=USER_AGENT, service_workers="block")
                    # Everything that decides what the run may claim happens in
                    # drive_page, which the default lane executes over a double.
                    # This method's whole job is to hand it a real page.
                    return drive_page(
                        context.new_page(),
                        url,
                        timeout_seconds=timeout_seconds,
                        client=client,
                        engine=self.name,
                    )
                finally:
                    browser.close()
