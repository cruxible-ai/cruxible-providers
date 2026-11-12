"""Cruxible provider adapters for the web plane.

Five operations, each served by its own entrypoint. Four are fetches:
``web.fetch`` retrieves one resource the run names, ``web.fetch.pages`` sends
the same request split into as few page requests as fit (one Capture per page),
``search.web`` queries a configured SearXNG instance, and ``web.render.dom``
assembles a page in a browser; each returns the documents it got as Captures,
verbatim, with how each was obtained in the typed HTTP coordinate. ``web.render`` is a derive: pure
computation over a Capture this run fetched, returning a derived Capture citing
it (the main content as Markdown, or the page as canonical JSON). The heavy
engine — a browser — lives behind the ``browser`` extra, which the
``web.render.dom`` implementation's manifest row declares and the resolver
materializes into that implementation's environment and no other.

Each operation serves the base kit capability of its name (``search.web``
serves ``web.search``), and the registration carries a copy of every Contract
the operations reach (``declares``), so the package installs with no kit.
"""

from __future__ import annotations

from pathlib import Path

from .dom import WebRenderDom
from .fetch import WebFetch
from .operations import (
    FETCH_OPERATION,
    FETCH_PAGES_OPERATION,
    RENDER_DOM_OPERATION,
    RENDER_OPERATION,
    SEARCH_OPERATION,
)
from .pages import WebFetchPages
from .recordings import FIXTURE_HOST, load_recordings
from .render import WebRender
from .search import SearxngSearch

__version__ = "0.6.0"

PACKAGE_ROOT = Path(__file__).resolve().parent
MANIFEST_PATH = PACKAGE_ROOT / "manifest.yaml"
CONTAINER_DIR = PACKAGE_ROOT.parent.parent / "container"

__all__ = [
    "CONTAINER_DIR",
    "FETCH_OPERATION",
    "FETCH_PAGES_OPERATION",
    "FIXTURE_HOST",
    "MANIFEST_PATH",
    "PACKAGE_ROOT",
    "RENDER_DOM_OPERATION",
    "RENDER_OPERATION",
    "SEARCH_OPERATION",
    "SearxngSearch",
    "WebFetch",
    "WebFetchPages",
    "WebRender",
    "WebRenderDom",
    "__version__",
    "load_recordings",
]
