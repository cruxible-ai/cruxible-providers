"""``doc.to_markdown`` — convert a supplied document into structured Markdown.

Everything this implementation produces is **derived**. The document is supplied
by the caller rather than retrieved from anywhere, so there is no observed-shaped
material to be had, and the output says so structurally: a ``document`` block
identifying what was converted, and a ``derived`` block holding the conversion.
The adapter mints no Capture; the CaptureContract governs the grade, and it can
only ever be a derived one.

Three conversion paths, chosen by what the document is rather than by what the
caller would prefer:

* an **already-linear** document — text, Markdown, CSV — is converted by the
  adapter itself, because layout analysis of a document with no layout is a
  tensor stack solving nothing;
* a **packaged fixture** replays the recorded engine response for the document
  shipped in this distribution, labelled ``recorded:<id>``;
* anything else goes to Docling, which the manifest declares the ``docling``
  extra for.

The declared page count is **checked, not trusted**. A document that opens to a
different page-count class than the bucket the run was admitted into refuses:
the alternative is a Capture built from a partial conversion that looks complete.
"""

from __future__ import annotations

from typing import Any

from cruxible_provider_runtime.errors import RefusalCode, RefusalError, refuse
from cruxible_provider_runtime.provider_api import ProviderResult, ProviderRunContext

from .documents import ResolvedSource, resolve_source
from .engines import DoclingMarkdownEngine, MarkdownEngine, MarkdownResult, PlainTextEngine
from .interfaces import MARKDOWN_INTERFACE_ID, document_format, page_count_class

__all__ = ["DoclingToMarkdown"]


class _RecordedMarkdownEngine:
    """Replays the recorded conversion of a packaged document."""

    def __init__(self, source: ResolvedSource) -> None:
        assert source.recording is not None
        self._source = source
        self.name = f"recorded:{source.recording.id}"

    def convert(self, data: bytes, *, filename: str, media_type: str) -> MarkdownResult:
        del data, filename, media_type
        recording = self._source.recording
        assert recording is not None
        output = recording.output
        return MarkdownResult(
            engine=self.name,
            markdown=str(output["markdown"]),
            page_count=int(output.get("page_count", 1)),
            metadata={
                "recorded_for_engine": recording.engine,
                "recorded_from": recording.recorded_from,
            },
        )


class DoclingToMarkdown:
    """Convert a document to Markdown."""

    interface_id = MARKDOWN_INTERFACE_ID

    def __init__(self, *, engine: MarkdownEngine | None = None) -> None:
        # The default is the production engine. Injection exists so a test can
        # hold the engine still, not so a test can replace what is under test.
        self._engine = engine

    def __call__(self, context: ProviderRunContext) -> ProviderResult:
        try:
            source = resolve_source(context.input, interface_id=self.interface_id)
            declared_pages = _declared_pages(context)
            engine = self._engine or _engine_for(source)
            result = engine.convert(
                source.data, filename=source.filename, media_type=source.media_type
            )
        except RefusalError as exc:
            return ProviderResult(status="refused", refusal=exc.refusal)

        if not result.markdown.strip():
            # A born-digital document that yields nothing is a failed conversion
            # wearing the shape of a successful one.
            return ProviderResult.refused(
                RefusalCode.PROVIDER_DECLINED,
                "the conversion recovered no text from a document declared to carry one",
                engine=result.engine,
                document_sha256=source.sha256,
            )

        observed_class = page_count_class(result.page_count)
        declared_class = page_count_class(declared_pages)
        if observed_class != declared_class:
            return ProviderResult.refused(
                RefusalCode.PROVIDER_DECLINED,
                "the document opened to a different page-count class than the bucket "
                "this run was admitted into",
                declared_bucket=context.input_bucket,
                declared_page_count=declared_pages,
                observed_page_count=result.page_count,
                observed_class=observed_class,
            )

        events: list[dict[str, Any]] = []
        if source.recording is not None:
            events.append(
                {
                    "kind": "recorded_engine_response",
                    "recording_id": source.recording.id,
                    "recorded_for_engine": source.recording.engine,
                    "note": "replayed from a response shipped in this distribution; no engine ran",
                }
            )
        return ProviderResult.ok(
            {
                "input_bucket": context.input_bucket,
                "document": source.describe(declared_pages=declared_pages),
                "derived": {
                    "kind": "markdown",
                    "engine": result.engine,
                    "text": result.markdown,
                    "page_count": result.page_count,
                    "metadata": result.metadata,
                },
            },
            metrics={
                "page_count": float(result.page_count),
                "character_count": float(len(result.markdown)),
            },
            events=events,
        )


def _declared_pages(context: ProviderRunContext) -> int:
    pages = context.input.get("page_count", 1)
    if not isinstance(pages, int) or isinstance(pages, bool) or pages <= 0:
        raise refuse(RefusalCode.PROVIDER_DECLINED, "page_count must be a positive integer")
    return pages


def _engine_for(source: ResolvedSource) -> MarkdownEngine:
    if document_format({"filename": source.filename, "media_type": source.media_type}) == (
        "plain_text"
    ):
        return PlainTextEngine()
    if source.recording is not None:
        return _RecordedMarkdownEngine(source)
    return DoclingMarkdownEngine()
