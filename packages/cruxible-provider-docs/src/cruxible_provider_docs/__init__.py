"""Cruxible provider adapters for the document plane.

Two implementations, of two interfaces: ``doc.to_markdown`` converts a supplied
document into Markdown, and ``ocr.extract`` recovers text from supplied page
images. Both engines are heavy and both live behind per-engine extras that the
base install does not carry; the base distribution holds the adapter logic, the
schemas, the bucket classifiers, the packaged documents, and the recorded engine
responses the conformance fixtures replay.

Nothing in this plane retrieves anything. The document arrives in the run input,
so everything these adapters produce is derived, and the output shape says so.
"""

from __future__ import annotations

from pathlib import Path

from .interfaces import (
    MARKDOWN_INTERFACE_DIGEST,
    MARKDOWN_INTERFACE_ID,
    MARKDOWN_VOCABULARY,
    OCR_INTERFACE_DIGEST,
    OCR_INTERFACE_ID,
    OCR_VOCABULARY,
    classify_ocr,
    classify_to_markdown,
    registrations,
)
from .ocr import PaddleOcrExtract
from .recordings import load_fixtures, load_recordings
from .to_markdown import DoclingToMarkdown

__version__ = "0.1.0"

PACKAGE_ROOT = Path(__file__).resolve().parent
MANIFEST_PATH = PACKAGE_ROOT / "manifest.yaml"
CONTAINER_DIR = PACKAGE_ROOT.parent.parent / "container"

__all__ = [
    "CONTAINER_DIR",
    "MANIFEST_PATH",
    "MARKDOWN_INTERFACE_DIGEST",
    "MARKDOWN_INTERFACE_ID",
    "MARKDOWN_VOCABULARY",
    "OCR_INTERFACE_DIGEST",
    "OCR_INTERFACE_ID",
    "OCR_VOCABULARY",
    "PACKAGE_ROOT",
    "DoclingToMarkdown",
    "PaddleOcrExtract",
    "__version__",
    "classify_ocr",
    "classify_to_markdown",
    "load_fixtures",
    "load_recordings",
    "registrations",
]
