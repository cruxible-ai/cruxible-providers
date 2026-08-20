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
deciding what a rendered run is allowed to claim, and getting every host it
touched into the run's recorder, is not. Those decisions live in
:func:`drive_page`, over the :class:`BrowserPage` protocol, so that the default
lane can execute them against a double. Wiring only a machine with a browser
installed can run is wiring nobody reviews, and the receipt is exactly what it
decides.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit

from cruxible_provider_runtime.egress import EgressRecorder
from cruxible_provider_runtime.errors import RefusalCode, refuse

__all__ = [
    "BrowserPage",
    "Extraction",
    "HtmlExtractor",
    "MainFrameResponse",
    "PageRenderer",
    "PlaywrightRenderer",
    "RenderedPage",
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


class BrowserPage(Protocol):
    """The slice of a browser page :func:`drive_page` drives.

    Written down as a protocol rather than left implicit so that the wiring can
    be exercised without a browser. The engine-marked lane is what asserts a real
    Playwright page satisfies it.
    """

    url: str

    def on(self, event: str, handler: Callable[[Any], None]) -> None: ...

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


def _record_contact(recorder: EgressRecorder, url: str) -> None:
    """Record ``url`` when it names an origin the egress contract is about.

    A browser also loads ``data:``, ``blob:`` and ``file:`` URLs, and the
    recorder's subject is who a provider talked to over a network. Normalising a
    hostless URL refuses rather than records, so the filter has to be on the way
    in rather than left to the recorder.
    """

    parts = urlsplit(url)
    if parts.scheme in {"http", "https"} and parts.hostname:
        recorder.record(url)


def drive_page(
    page: BrowserPage,
    url: str,
    *,
    timeout_seconds: float,
    recorder: EgressRecorder,
    engine: str,
) -> RenderedPage:
    """Navigate ``page`` to ``url``, recording every origin it contacts.

    Both hooks are attached and both are load-bearing. A browser contacts hosts
    the adapter never named — the redirect it follows, the CDN its markup pulls a
    script from, the API that script queries — and none of that passes through
    the instrumented HTTP client, so without these hooks none of it reaches the
    run's recorder and the receipt understates the run to exactly the degree the
    page was interesting. Requests cover what was attempted; responses cover the
    hops a redirect chain answers with.

    What comes back is the main-frame response as the browser saw it, never the
    request as the caller wrote it.
    """

    page.on("request", lambda event: _record_contact(recorder, event.url))
    page.on("response", lambda event: _record_contact(recorder, event.url))
    # Recorded here as well as by the hook. A browser that dies during launch
    # still leaves a run that was about to contact this origin, and a receipt
    # that omitted it would understate the attempt.
    _record_contact(recorder, url)

    response = page.goto(url, wait_until="networkidle", timeout=timeout_seconds * 1000)
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


class PlaywrightRenderer:
    """Client-side assembly with a real browser. Requires the ``browser`` extra.

    The import is inside the method, and its failure is a **typed refusal**
    rather than an ImportError crossing the process boundary as an error. The
    distinction is the one the taxonomy draws: an environment missing the engine
    its implementation declared is not a failed answer, it is an environment that
    diverges from the resolution it was supposed to be — which is exactly what
    ``environment_divergence`` names.
    """

    name = "playwright"

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

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                # Everything that decides what the run may claim happens in
                # drive_page, which the default lane executes over a double. This
                # method's whole job is to hand it a real page.
                return drive_page(
                    browser.new_page(user_agent=_USER_AGENT),
                    url,
                    timeout_seconds=timeout_seconds,
                    recorder=recorder,
                    engine=self.name,
                )
            finally:
                browser.close()


_USER_AGENT = "cruxible-provider-web/0.1 (+https://cruxible.ai)"
