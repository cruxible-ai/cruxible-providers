"""The web plane's engine seam, and its real implementation.

An engine is anything that turns retrieved material into derived material.
There is one here:

``HtmlExtractor``
    Main-content extraction. The real one is trafilatura, which is a **base**
    dependency: it is pure Python over an lxml wheel, so keeping it out of the
    default lane would buy a few megabytes and cost the lane its only real
    derivation.

The seam is injectable, and the default is the production spelling. The
injection exists so a test can hold one variable still, not so a test can
replace the thing under test.

``web.fetch`` does not render pages in a browser. A rendered input classifies
into the ``js_rendered`` bucket, which this package's manifest does not claim,
so it is refused at admission before any provider code runs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from cruxible_provider_runtime.errors import RefusalCode, refuse

__all__ = [
    "Extraction",
    "HtmlExtractor",
    "TrafilaturaExtractor",
]


@dataclass(frozen=True)
class Extraction:
    """Derived material. Never observed, whatever the extractor thinks."""

    engine: str
    kind: str
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)


class HtmlExtractor(Protocol):
    name: str

    def extract(self, html: str, *, url: str) -> Extraction: ...


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
