"""The document plane's engine seams, and their real implementations.

Two seams, two heavy engines, two extras:

``MarkdownEngine`` — Docling, behind the ``docling`` extra. Docling drags a
tensor stack; nothing in the default lane imports it.

``OcrEngine`` — PaddleOCR, behind the ``paddleocr`` extra. PaddleOCR rather than
Surya, and licensing decides it before quality does: PaddleOCR is Apache-2.0,
which an Apache-2.0 distribution can name as an optional dependency without
qualification, while Surya is GPL-3.0-or-later with a separate commercial grant
whose terms depend on the adopter's revenue. Putting a licence question between
an adopter and a provider they are about to bind is a cost with no upside at this
stage. PaddleOCR also publishes CPU wheels for every platform the launch marker
environments name, which Surya's torch dependency does not make simple.

Both real engines import inside the method and turn a missing import into a typed
``environment_divergence`` refusal rather than letting an ImportError cross the
process boundary as an error. An environment that lacks the engine its
implementation declared has diverged from the resolution it was supposed to be;
that is not a failed answer, and the taxonomy already has a name for it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from cruxible_provider_runtime.errors import RefusalCode, refuse

from .documents import document_suffix

__all__ = [
    "DoclingMarkdownEngine",
    "MarkdownEngine",
    "MarkdownResult",
    "OcrEngine",
    "OcrPage",
    "OcrResult",
    "PaddleOcrEngine",
    "PlainTextEngine",
]


@dataclass(frozen=True)
class MarkdownResult:
    engine: str
    markdown: str
    page_count: int
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OcrPage:
    page: int
    text: str
    line_count: int
    mean_confidence: float | None


@dataclass(frozen=True)
class OcrResult:
    engine: str
    pages: tuple[OcrPage, ...]
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return "\n\n".join(page.text for page in self.pages)


class MarkdownEngine(Protocol):
    name: str

    def convert(self, data: bytes, *, filename: str, media_type: str) -> MarkdownResult: ...


class OcrEngine(Protocol):
    name: str

    def read(self, data: bytes, *, filename: str, language: str) -> OcrResult: ...


class PlainTextEngine:
    """The engine-free path, and a real conversion rather than a stub.

    An already-linear document — text, Markdown, CSV — needs no layout analysis
    to become Markdown, so requiring a tensor stack for one would be an
    absurdity. CSV becomes a table because a CSV *is* a table and flattening it
    into lines would destroy the only structure it has.

    This is also what gives the plane a success path that runs in the default
    conformance lane end to end, through a real child process, with no engine
    installed anywhere.
    """

    name = "plain-text"

    def convert(self, data: bytes, *, filename: str, media_type: str) -> MarkdownResult:
        text = data.decode("utf-8", "replace")
        if filename.lower().endswith((".csv", ".tsv")) or media_type.startswith("text/csv"):
            markdown = _csv_to_markdown(text, delimiter="\t" if filename.endswith(".tsv") else ",")
        else:
            markdown = text.strip() + "\n" if text.strip() else ""
        return MarkdownResult(
            engine=self.name,
            markdown=markdown,
            page_count=1,
            metadata={"converted_as": "already-linear text"},
        )


def _csv_to_markdown(text: str, *, delimiter: str) -> str:
    import csv
    import io

    rows = [row for row in csv.reader(io.StringIO(text), delimiter=delimiter) if row]
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    padded = [[*row, *[""] * (width - len(row))] for row in rows]
    header, *body = padded
    lines = ["| " + " | ".join(header) + " |", "| " + " | ".join(["---"] * width) + " |"]
    lines += ["| " + " | ".join(row) + " |" for row in body]
    return "\n".join(lines) + "\n"


class DoclingMarkdownEngine:
    """Document conversion with Docling. Requires the ``docling`` extra."""

    name = "docling"

    def convert(self, data: bytes, *, filename: str, media_type: str) -> MarkdownResult:
        del media_type
        try:
            from docling.datamodel.base_models import DocumentStream
            from docling.document_converter import DocumentConverter
        except ImportError as exc:
            raise refuse(
                RefusalCode.ENVIRONMENT_DIVERGENCE,
                "this implementation declares the 'docling' extra and the materialized "
                "environment does not carry it",
                required_extra="docling",
                engine=self.name,
            ) from exc

        from io import BytesIO

        # The bytes are converted in memory. Writing a caller's document to a
        # temporary file would put it on a disk nobody asked about, and the
        # provider environment is not a security boundary.
        stream = DocumentStream(name=filename, stream=BytesIO(data))
        result = DocumentConverter().convert(stream)
        document = result.document
        pages = getattr(document, "pages", None)
        return MarkdownResult(
            engine=self.name,
            markdown=document.export_to_markdown(),
            page_count=len(pages) if pages is not None else 1,
            metadata={"status": str(getattr(result, "status", ""))},
        )


class PaddleOcrEngine:
    """Text recovery with PaddleOCR. Requires the ``paddleocr`` extra.

    The orientation and unwarping sub-models are off. They cost a model download
    and a per-page pass each, and the buckets this implementation claims are the
    ones where they change nothing: a clean, deskewed, single-orientation page.
    A bucket that needed them would be a different claim with its own fixture.
    """

    name = "paddleocr"

    def __init__(self, *, language: str = "en") -> None:
        self._language = language
        self._engine: Any | None = None

    def _engine_for(self, language: str) -> Any:
        if self._engine is not None:  # pragma: no cover - engine lane only
            return self._engine
        try:
            from paddleocr import PaddleOCR
        except ImportError as exc:
            raise refuse(
                RefusalCode.ENVIRONMENT_DIVERGENCE,
                "this implementation declares the 'paddleocr' extra and the materialized "
                "environment does not carry it",
                required_extra="paddleocr",
                engine=self.name,
            ) from exc
        self._engine = PaddleOCR(
            lang=language,
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
        )
        return self._engine

    def read(self, data: bytes, *, filename: str, language: str) -> OcrResult:  # pragma: no cover
        # Not covered by the default lane by construction: reaching this line
        # means the engine extra is installed, which is the engine lane.
        import tempfile
        from pathlib import Path

        engine = self._engine_for(language or self._language)
        with tempfile.TemporaryDirectory() as directory:
            # The name is this module's; only the extension comes from the run
            # input, and only after `document_suffix` has established that what
            # it was given is an extension. PaddleOCR dispatches on the
            # extension, which is the whole of what the caller's filename is
            # worth here.
            target = Path(directory) / f"page{document_suffix(filename)}"
            target.write_bytes(data)
            # `.ocr()` is deprecated in PaddleOCR 3.x and forwards to predict()
            # with an incompatible keyword; predict() is the supported call.
            results = engine.predict(str(target))

        pages: list[OcrPage] = []
        for index, page in enumerate(results, start=1):
            texts = list(page.get("rec_texts") or [])
            scores = [float(score) for score in (page.get("rec_scores") or [])]
            pages.append(
                OcrPage(
                    page=index,
                    text="\n".join(texts),
                    line_count=len(texts),
                    mean_confidence=(sum(scores) / len(scores)) if scores else None,
                )
            )
        return OcrResult(engine=self.name, pages=tuple(pages), metadata={"language": language})
