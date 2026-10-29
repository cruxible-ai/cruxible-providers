"""Cruxible provider adapters for the document plane.

Two operations, one entrypoint each: ``doc.to_markdown`` converts a supplied
document into Markdown, and ``ocr.extract`` recovers text from supplied page
images. Both engines are heavy and both live behind per-engine extras that the
base install does not carry; the base distribution holds the adapter logic, the
packaged documents, and the recorded engine responses the conformance suite
replays.

Nothing in this plane retrieves anything. The document arrives in the run input,
so everything these adapters produce is derived, and the output shape says so.
"""

from __future__ import annotations

from pathlib import Path

from .documents import PROVIDER_DECLINED
from .ocr import OPERATION as OCR_OPERATION
from .ocr import PaddleOcrExtract
from .recordings import load_recordings
from .to_markdown import OPERATION as MARKDOWN_OPERATION
from .to_markdown import DoclingToMarkdown

__version__ = "0.3.0"

PACKAGE_ROOT = Path(__file__).resolve().parent
MANIFEST_PATH = PACKAGE_ROOT / "manifest.yaml"
CONTAINER_DIR = PACKAGE_ROOT.parent.parent / "container"

__all__ = [
    "CONTAINER_DIR",
    "MANIFEST_PATH",
    "MARKDOWN_OPERATION",
    "OCR_OPERATION",
    "PACKAGE_ROOT",
    "PROVIDER_DECLINED",
    "DoclingToMarkdown",
    "PaddleOcrExtract",
    "__version__",
    "load_recordings",
]
