"""Cruxible provider adapters for the web plane.

Two implementations, of two interfaces: ``web.fetch`` retrieves one resource the
run names, and ``search.web`` queries a configured SearXNG instance. Neither is
an engine. The heavy one — a browser — lives behind the ``browser`` extra, which
the ``web.fetch`` implementation's manifest declares and the resolver
materializes into that implementation's environment and no other.
"""

from __future__ import annotations

from pathlib import Path

from .fetch import WebFetch
from .interfaces import (
    FETCH_INTERFACE_DIGEST,
    FETCH_INTERFACE_ID,
    FETCH_VOCABULARY,
    SEARCH_INTERFACE_DIGEST,
    SEARCH_INTERFACE_ID,
    SEARCH_VOCABULARY,
    classify_search,
    classify_web_fetch,
    registrations,
)
from .recordings import FIXTURE_HOST, load_recordings
from .search import SearxngSearch

__version__ = "0.1.0"

PACKAGE_ROOT = Path(__file__).resolve().parent
MANIFEST_PATH = PACKAGE_ROOT / "manifest.yaml"
CONTAINER_DIR = PACKAGE_ROOT.parent.parent / "container"

__all__ = [
    "CONTAINER_DIR",
    "FETCH_INTERFACE_DIGEST",
    "FETCH_INTERFACE_ID",
    "FETCH_VOCABULARY",
    "FIXTURE_HOST",
    "MANIFEST_PATH",
    "PACKAGE_ROOT",
    "SEARCH_INTERFACE_DIGEST",
    "SEARCH_INTERFACE_ID",
    "SEARCH_VOCABULARY",
    "SearxngSearch",
    "WebFetch",
    "__version__",
    "classify_search",
    "classify_web_fetch",
    "load_recordings",
    "registrations",
]
