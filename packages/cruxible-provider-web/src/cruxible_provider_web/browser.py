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
getting every host it touched into the run's recorder is not. That decision
lives in :func:`drive_page`, over the :class:`BrowserPage` protocol, so that the
default lane can execute it against a double.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit

from cruxible_provider_runtime.egress import EgressRecorder
from cruxible_provider_runtime.errors import decline

from .operations import ENGINE_UNAVAILABLE, PROVIDER_DECLINED

__all__ = [
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
    """The slice of a browser's navigation response this plane reads."""

    url: str
    status: int

    def all_headers(self) -> dict[str, str]: ...


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
    """Assemble the page at ``url`` in ``page``, recording every origin it contacts.

    Both hooks are attached before the navigation and both are load-bearing. A
    browser contacts hosts the adapter never named -- the CDN the markup pulls a
    script from, the API that script queries, the redirects those answer with --
    and none of that passes through an instrumented HTTP client, so without these
    hooks none of it reaches the run's recorder and the receipt understates the
    run to exactly the degree the page was interesting. The main frame is a
    contact like any other.
    """

    def contacted(event: Any) -> None:
        _record_contact(recorder, event.url)

    page.on("request", contacted)
    page.on("response", contacted)
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
    html = page.content()
    return RenderedPage(
        engine=engine,
        html=html,
        final_url=response.url,
        status_code=response.status,
        headers={key.lower(): value for key, value in response.all_headers().items()},
    )


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
                    browser.new_page(user_agent=_USER_AGENT),
                    url,
                    timeout_seconds=timeout_seconds,
                    recorder=recorder,
                    engine=self.name,
                )
            finally:
                browser.close()


_USER_AGENT = "cruxible-provider-web/0.5 (+https://cruxible.ai)"
