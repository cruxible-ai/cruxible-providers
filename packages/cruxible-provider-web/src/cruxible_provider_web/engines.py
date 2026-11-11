"""The extraction engine ``web.render`` reads a fetched document with.

``web.render`` is a derive (D-R37): pure computation over the exact bytes of a
Capture this run fetched, contacting nothing. Its engine is main-content
extraction, and the real one is trafilatura, a **base** dependency: it is pure
Python over an lxml wheel, so the derive needs no extra and no browser.

The browser ``web.render.dom`` drives is a different engine with a different
effect -- it fetches what a page pulls -- and lives in :mod:`.browser`.

The seam is injectable and the default is the production spelling. The
injection exists so a test can hold one variable still, not so a test can
replace the thing under test.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from cruxible_provider_runtime.errors import decline

from .operations import PROVIDER_DECLINED

__all__ = ["Extraction", "ExtractionOutput", "HtmlExtractor", "TrafilaturaExtractor"]

ExtractionOutput = Literal["markdown", "text"]
"""Main content as Markdown (``form: text``) or as plain text (``form: json``)."""


@dataclass(frozen=True)
class Extraction:
    """Derived material. Never observed, whatever the extractor thinks."""

    engine: str
    kind: str
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)


class HtmlExtractor(Protocol):
    name: str

    def extract(
        self,
        html: str,
        *,
        url: str,
        output: ExtractionOutput = "markdown",
        require_content: bool = True,
    ) -> Extraction: ...


class TrafilaturaExtractor:
    """Main-content extraction with trafilatura, as Markdown or plain text.

    ``favor_precision`` is on: an adapter feeding a governed Capture should drop
    a boilerplate paragraph rather than admit one, because the downstream cost of
    a navigation menu inside an extracted document is paid by every claim built
    on it.

    The document is parsed **once** and the parsed tree is used for both the
    metadata pass and the body pass. Trafilatura reaches the body only through
    ``extract`` and metadata only through ``extract_metadata``, so two calls are
    unavoidable; two *parses* are not, and two parses could disagree with each
    other about what the document is.

    Extraction is a function of the document: no clock, no network (``url`` only
    resolves relative references), so a derive built on it is pure.
    """

    name = "trafilatura"

    def extract(
        self,
        html: str,
        *,
        url: str,
        output: ExtractionOutput = "markdown",
        require_content: bool = True,
    ) -> Extraction:
        from trafilatura import extract as extract_text
        from trafilatura import extract_metadata
        from trafilatura.utils import load_html

        tree = load_html(html)
        if tree is None:
            raise decline(
                PROVIDER_DECLINED,
                "the cited document could not be parsed as HTML",
                url=url,
                engine=self.name,
            )
        metadata = extract_metadata(tree)
        text = extract_text(
            tree,
            url=url,
            output_format="markdown" if output == "markdown" else "txt",
            with_metadata=False,
            include_comments=False,
            include_tables=True,
            favor_precision=True,
        )
        if not text and require_content:
            raise decline(
                PROVIDER_DECLINED,
                "no main content could be extracted from the cited document",
                url=url,
                engine=self.name,
            )
        return Extraction(
            engine=self.name,
            kind=output,
            text=text or "",
            metadata={
                "title": getattr(metadata, "title", None),
                "author": getattr(metadata, "author", None),
                "published": getattr(metadata, "date", None),
                "sitename": getattr(metadata, "sitename", None),
                "language": getattr(metadata, "language", None),
            },
        )
