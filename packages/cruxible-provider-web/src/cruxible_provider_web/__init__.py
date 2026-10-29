"""Cruxible provider adapters for the web plane.

Two operations, each served by its own entrypoint: ``web.fetch`` retrieves one
resource the run names, and ``search.web`` queries a configured SearXNG
instance. Neither is an engine. The heavy one — a browser — lives behind the
``browser`` extra, which the ``web.fetch`` implementation's manifest row
declares and the resolver materializes into that implementation's environment
and no other.
"""

from __future__ import annotations

from pathlib import Path

from .fetch import WebFetch
from .operations import FETCH_OPERATION, SEARCH_OPERATION
from .recordings import FIXTURE_HOST, load_recordings
from .search import SearxngSearch

__version__ = "0.3.0"

PACKAGE_ROOT = Path(__file__).resolve().parent
MANIFEST_PATH = PACKAGE_ROOT / "manifest.yaml"
CONTAINER_DIR = PACKAGE_ROOT.parent.parent / "container"

__all__ = [
    "CONTAINER_DIR",
    "FETCH_OPERATION",
    "FIXTURE_HOST",
    "MANIFEST_PATH",
    "PACKAGE_ROOT",
    "SEARCH_OPERATION",
    "SearxngSearch",
    "WebFetch",
    "__version__",
    "load_recordings",
]
