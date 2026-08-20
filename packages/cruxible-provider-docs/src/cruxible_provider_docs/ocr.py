"""``ocr.extract`` — recover text from supplied page images.

Everything here is derived, and more obviously so than anywhere else in the
roster: an OCR reading is a *model's opinion* about what marks on a page say. The
output carries the engine's own per-page confidence where the engine reports
one, and carries it as engine metadata rather than as a score this adapter
invented — there is no generic confidence number in this system, and an adapter
that manufactured one would be inventing exactly the thing the standing law
forbids.

The engine is PaddleOCR, behind the ``paddleocr`` extra. A packaged fixture
replays a recorded reading instead, labelled ``recorded:<id>``; the engine lane
runs the real engine over the same shipped image and asserts it reproduces the
recording.
"""

from __future__ import annotations

from typing import Any

from cruxible_provider_runtime.errors import RefusalCode, RefusalError, refuse
from cruxible_provider_runtime.provider_api import ProviderResult, ProviderRunContext

from .documents import ResolvedSource, resolve_source
from .engines import OcrEngine, OcrPage, OcrResult, PaddleOcrEngine
from .interfaces import OCR_INTERFACE_ID, page_count_class

__all__ = ["PaddleOcrExtract"]


class _RecordedOcrEngine:
    """Replays the recorded reading of a packaged image."""

    def __init__(self, source: ResolvedSource) -> None:
        assert source.recording is not None
        self._source = source
        self.name = f"recorded:{source.recording.id}"

    def read(self, data: bytes, *, filename: str, language: str) -> OcrResult:
        del data, filename
        recording = self._source.recording
        assert recording is not None
        pages = tuple(
            OcrPage(
                page=int(page["page"]),
                text=str(page["text"]),
                line_count=int(page["line_count"]),
                mean_confidence=(
                    float(page["mean_confidence"])
                    if page.get("mean_confidence") is not None
                    else None
                ),
            )
            for page in recording.output["pages"]
        )
        return OcrResult(
            engine=self.name,
            pages=pages,
            metadata={
                "language": language,
                "recorded_for_engine": recording.engine,
                "recorded_from": recording.recorded_from,
            },
        )


class PaddleOcrExtract:
    """Read text off page images."""

    interface_id = OCR_INTERFACE_ID

    def __init__(self, *, engine: OcrEngine | None = None) -> None:
        self._engine = engine

    def __call__(self, context: ProviderRunContext) -> ProviderResult:
        try:
            source = resolve_source(context.input, interface_id=self.interface_id)
            declared_pages = _declared_pages(context)
            language = _language(context)
            engine = self._engine or _engine_for(source)
            result = engine.read(source.data, filename=source.filename, language=language)
        except RefusalError as exc:
            return ProviderResult(status="refused", refusal=exc.refusal)

        if not result.pages or not result.text.strip():
            return ProviderResult.refused(
                RefusalCode.PROVIDER_DECLINED,
                "the reading recovered no text from the supplied image",
                engine=result.engine,
                document_sha256=source.sha256,
            )

        observed_class = page_count_class(len(result.pages))
        if observed_class != page_count_class(declared_pages):
            return ProviderResult.refused(
                RefusalCode.PROVIDER_DECLINED,
                "the image set is a different page-count class than the bucket this run "
                "was admitted into",
                declared_bucket=context.input_bucket,
                declared_page_count=declared_pages,
                observed_page_count=len(result.pages),
            )

        events: list[dict[str, Any]] = []
        if source.recording is not None:
            events.append(
                {
                    "kind": "recorded_engine_response",
                    "recording_id": source.recording.id,
                    "recorded_for_engine": source.recording.engine,
                    "note": "replayed from a reading shipped in this distribution; no engine ran",
                }
            )
        return ProviderResult.ok(
            {
                "input_bucket": context.input_bucket,
                "document": source.describe(declared_pages=declared_pages),
                "derived": {
                    "kind": "ocr_text",
                    "engine": result.engine,
                    "text": result.text,
                    "pages": [
                        {
                            "page": page.page,
                            "text": page.text,
                            "line_count": page.line_count,
                            # The engine's own number, passed through under the
                            # engine's name. Not a grade, and not comparable
                            # across engines.
                            "engine_mean_confidence": page.mean_confidence,
                        }
                        for page in result.pages
                    ],
                    "metadata": result.metadata,
                },
            },
            metrics={
                "page_count": float(len(result.pages)),
                "line_count": float(sum(page.line_count for page in result.pages)),
            },
            events=events,
        )


def _declared_pages(context: ProviderRunContext) -> int:
    pages = context.input.get("page_count", 1)
    if not isinstance(pages, int) or isinstance(pages, bool) or pages <= 0:
        raise refuse(RefusalCode.PROVIDER_DECLINED, "page_count must be a positive integer")
    return pages


def _language(context: ProviderRunContext) -> str:
    language = context.input.get("language", "en")
    if not isinstance(language, str) or not language:
        raise refuse(RefusalCode.PROVIDER_DECLINED, "language must be a string")
    return language


def _engine_for(source: ResolvedSource) -> OcrEngine:
    if source.recording is not None:
        return _RecordedOcrEngine(source)
    return PaddleOcrEngine()
