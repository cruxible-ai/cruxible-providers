"""Stub interface registrations for the document plane.

**These are stubs**, minted under the ``cruxible.interface.stub.v1`` domain tag
until core registers the real interfaces; a drift test asserts each literal still
matches its preimage. The bucket vocabularies ship as the same YAML the
repository publishes under ``vocab/interfaces/``, copied into the distribution so
an installed package can classify without a repository to read, and a test
asserts the two copies are one document.

A note on what the classifiers can honestly measure. A document plane's buckets
describe things that are only knowable by opening the document — how many pages
it has, whether it carries a text layer, whether its layout is tabular. The run
input therefore carries them as the caller's description, and the adapter
**checks** rather than trusts: a conversion that recovers no text from a document
declared born-digital refuses instead of returning an empty Markdown file, and a
document whose real page count exceeds the declared bucket refuses instead of
being silently truncated. Measured-not-claimed is a property of the pair, not of
the classifier alone.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from cruxible_provider_runtime.buckets import BucketVocabulary
from cruxible_provider_runtime.canonical import domain_digest
from cruxible_provider_runtime.registry import InterfaceRegistration, load_bucket_vocabulary

__all__ = [
    "MARKDOWN_INTERFACE_DIGEST",
    "MARKDOWN_INTERFACE_ID",
    "MARKDOWN_VOCABULARY",
    "OCR_INTERFACE_DIGEST",
    "OCR_INTERFACE_ID",
    "OCR_VOCABULARY",
    "STUB_INTERFACE_DOMAIN_TAG",
    "classify_ocr",
    "classify_to_markdown",
    "page_count_class",
    "registrations",
]

STUB_INTERFACE_DOMAIN_TAG = "cruxible.interface.stub.v1"
VOCAB_DIR = Path(__file__).resolve().parent / "vocab"

MARKDOWN_INTERFACE_ID = "doc.to_markdown"
OCR_INTERFACE_ID = "ocr.extract"

_SOURCE_SCHEMA = {
    "kind": {"type": "string", "required": True, "enum": ["inline", "packaged_fixture"]},
    "filename": {"type": "string", "required": False},
    "media_type": {"type": "string", "required": False},
    "content_base64": {"type": "string", "required": False},
    "id": {"type": "string", "required": False},
}

MARKDOWN_PREIMAGE: dict[str, Any] = {
    "interface_id": MARKDOWN_INTERFACE_ID,
    "version": 1,
    "input": {
        "source": _SOURCE_SCHEMA,
        "page_count": {"type": "integer", "required": False, "default": 1},
        "scanned": {"type": "string", "required": False, "default": "born_digital"},
        "layout": {"type": "string", "required": False, "default": "linear"},
    },
    "output": {
        "input_bucket": {"type": "string"},
        # No "retrieved" block: this plane retrieves nothing. The document is
        # supplied by the caller, so everything the adapter produces is derived,
        # and the output says so structurally rather than in a comment.
        "document": {"type": "object"},
        "derived": {"type": "object"},
    },
    "refusals": [
        "provider_declined",
        "environment_divergence",
        "unclaimed_bucket",
    ],
}

OCR_PREIMAGE: dict[str, Any] = {
    "interface_id": OCR_INTERFACE_ID,
    "version": 1,
    "input": {
        "source": _SOURCE_SCHEMA,
        "page_count": {"type": "integer", "required": False, "default": 1},
        "script": {"type": "string", "required": False, "default": "latin"},
        "scan_quality": {"type": "string", "required": False, "default": "clean"},
        "layout": {"type": "string", "required": False, "default": "linear"},
        "language": {"type": "string", "required": False, "default": "en"},
    },
    "output": {
        "input_bucket": {"type": "string"},
        "document": {"type": "object"},
        "derived": {"type": "object"},
    },
    "refusals": [
        "provider_declined",
        "environment_divergence",
        "unclaimed_bucket",
    ],
}

MARKDOWN_INTERFACE_DIGEST = (
    "sha256:5bb615e27f9eaa3967a87d5e90e0d4dd9d8551c6b46e610e934138fa897ead16"
)
OCR_INTERFACE_DIGEST = "sha256:7d975b826e13e71fe5adc133ce35b4eeb067d2e2673bf786d887c9e32b59c7b2"

MARKDOWN_VOCABULARY: BucketVocabulary = load_bucket_vocabulary(VOCAB_DIR / "doc.to_markdown.yaml")
OCR_VOCABULARY: BucketVocabulary = load_bucket_vocabulary(VOCAB_DIR / "ocr.extract.yaml")

_FORMAT_BY_SUFFIX = {
    ".pdf": "pdf",
    ".doc": "office",
    ".docx": "office",
    ".odt": "office",
    ".xls": "office",
    ".xlsx": "office",
    ".ods": "office",
    ".ppt": "office",
    ".pptx": "office",
    ".html": "html",
    ".htm": "html",
    ".xhtml": "html",
    ".txt": "plain_text",
    ".md": "plain_text",
    ".markdown": "plain_text",
    ".csv": "plain_text",
    ".tsv": "plain_text",
    ".eml": "email",
    ".mbox": "email",
    ".msg": "email",
}

_FORMAT_BY_MEDIA_TYPE = {
    "application/pdf": "pdf",
    "text/html": "html",
    "application/xhtml+xml": "html",
    "text/plain": "plain_text",
    "text/markdown": "plain_text",
    "text/csv": "plain_text",
    "message/rfc822": "email",
}

_SCANNED_CLASSES = {"born_digital", "mixed", "scanned"}
_LAYOUT_CLASSES = {"linear", "multi_column", "tabular", "forms"}
_SCRIPT_CLASSES = {"latin", "cjk", "rtl", "mixed"}
_SCAN_QUALITY_CLASSES = {"clean", "degraded", "handwritten"}


def page_count_class(pages: int) -> str:
    """The page-count class a real page count falls in.

    Shared with the adapters, which use it to compare the document they actually
    opened against the bucket the run was admitted into.
    """

    if pages <= 1:
        return "single"
    if pages <= 10:
        return "short"
    if pages <= 100:
        return "medium"
    return "long"


def document_format(source: Mapping[str, Any]) -> str | None:
    """The container format, from the media type first and the filename second."""

    media_type = source.get("media_type")
    if isinstance(media_type, str):
        base = media_type.split(";", 1)[0].strip().lower()
        if base in _FORMAT_BY_MEDIA_TYPE:
            return _FORMAT_BY_MEDIA_TYPE[base]
    filename = source.get("filename")
    if isinstance(filename, str) and "." in filename:
        suffix = filename[filename.rfind(".") :].lower()
        if suffix in _FORMAT_BY_SUFFIX:
            return _FORMAT_BY_SUFFIX[suffix]
    return None


def _pages(payload: Mapping[str, Any]) -> int | None:
    pages = payload.get("page_count", 1)
    if not isinstance(pages, int) or isinstance(pages, bool) or pages <= 0:
        return None
    return pages


def _source(payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
    source = payload.get("source")
    return source if isinstance(source, Mapping) else None


def classify_to_markdown(payload: Mapping[str, Any]) -> Mapping[str, str] | None:
    source = _source(payload)
    if source is None:
        return None
    document = document_format(source)
    pages = _pages(payload)
    if document is None or pages is None:
        return None
    scanned = payload.get("scanned", "born_digital")
    layout = payload.get("layout", "linear")
    if scanned not in _SCANNED_CLASSES or layout not in _LAYOUT_CLASSES:
        return None
    return {
        "format": document,
        "scanned": str(scanned),
        "page_count": page_count_class(pages),
        "layout": str(layout),
    }


def classify_ocr(payload: Mapping[str, Any]) -> Mapping[str, str] | None:
    source = _source(payload)
    if source is None:
        return None
    pages = _pages(payload)
    if pages is None:
        return None
    script = payload.get("script", "latin")
    quality = payload.get("scan_quality", "clean")
    layout = payload.get("layout", "linear")
    if (
        script not in _SCRIPT_CLASSES
        or quality not in _SCAN_QUALITY_CLASSES
        or layout not in _LAYOUT_CLASSES
    ):
        return None
    return {
        "script": str(script),
        "scan_quality": str(quality),
        "page_count": page_count_class(pages),
        "layout": str(layout),
    }


def recompute_interface_digest(preimage: Mapping[str, Any]) -> str:
    """Recompute a stub digest from its preimage (used by the drift tests)."""

    return domain_digest(STUB_INTERFACE_DOMAIN_TAG, dict(preimage))


def markdown_registration() -> InterfaceRegistration:
    return InterfaceRegistration(
        interface_id=MARKDOWN_INTERFACE_ID,
        interface_digest=MARKDOWN_INTERFACE_DIGEST,
        bucket_vocabulary=MARKDOWN_VOCABULARY,
        classifier=classify_to_markdown,
        description="Convert a supplied document into structured Markdown.",
    )


def ocr_registration() -> InterfaceRegistration:
    return InterfaceRegistration(
        interface_id=OCR_INTERFACE_ID,
        interface_digest=OCR_INTERFACE_DIGEST,
        bucket_vocabulary=OCR_VOCABULARY,
        classifier=classify_ocr,
        description="Recover text from supplied page images.",
    )


def registrations() -> tuple[InterfaceRegistration, InterfaceRegistration]:
    """Everything a stub registry needs seeding with to bind this package."""

    return (markdown_registration(), ocr_registration())
