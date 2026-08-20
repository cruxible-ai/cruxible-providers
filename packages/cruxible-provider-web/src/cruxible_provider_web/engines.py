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
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from cruxible_provider_runtime.errors import RefusalCode, refuse

__all__ = [
    "Extraction",
    "HtmlExtractor",
    "PageRenderer",
    "PlaywrightRenderer",
    "RenderedPage",
    "TrafilaturaExtractor",
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
    """A document after client-side assembly, plus who assembled it."""

    engine: str
    html: str


class HtmlExtractor(Protocol):
    name: str

    def extract(self, html: str, *, url: str) -> Extraction: ...


class PageRenderer(Protocol):
    name: str

    def render(self, url: str, *, timeout_seconds: float) -> RenderedPage: ...


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

    def render(self, url: str, *, timeout_seconds: float) -> RenderedPage:
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
                page = browser.new_page(user_agent=_USER_AGENT)
                page.goto(url, wait_until="networkidle", timeout=timeout_seconds * 1000)
                html = page.content()
            finally:
                browser.close()
        return RenderedPage(engine=self.name, html=html)


_USER_AGENT = "cruxible-provider-web/0.1 (+https://cruxible.ai)"
