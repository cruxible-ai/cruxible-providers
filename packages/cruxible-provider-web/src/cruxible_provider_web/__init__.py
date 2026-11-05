"""Cruxible provider adapters for the web plane.

Four operations, each served by its own entrypoint. Three are fetches:
``web.fetch`` retrieves one resource the run names, ``search.web`` queries a
configured SearXNG instance, and ``web.render.dom`` assembles a page in a
browser; each returns the document it got as its Capture, verbatim, with how it
was obtained in the typed HTTP coordinate. ``web.render`` is a derive: pure
computation over a Capture this run fetched, returning a derived Capture citing
it (the main content as Markdown, or the page as canonical JSON). The heavy
engine — a browser — lives behind the ``browser`` extra, which the
``web.render.dom`` implementation's manifest row declares and the resolver
materializes into that implementation's environment and no other.

The package also ships the bodies of every Contract its operations pin, under
``contracts/`` (``contract_members``): each file is the accepted-tree member an
operator accepts before installing the package, and the install pins the
operations to them.
"""

from __future__ import annotations

from pathlib import Path

from .dom import WebRenderDom
from .fetch import WebFetch
from .operations import (
    FETCH_OPERATION,
    RENDER_DOM_OPERATION,
    RENDER_OPERATION,
    SEARCH_OPERATION,
)
from .recordings import FIXTURE_HOST, load_recordings
from .render import WebRender
from .search import SearxngSearch

__version__ = "0.5.0"

PACKAGE_ROOT = Path(__file__).resolve().parent
MANIFEST_PATH = PACKAGE_ROOT / "manifest.yaml"
CONTAINER_DIR = PACKAGE_ROOT.parent.parent / "container"
CONTRACTS_DIR = PACKAGE_ROOT / "contracts"
"""Each Contract at ``<name>/v<N>.json``, exactly as Core stores it under ``contracts/``."""


def contract_members() -> dict[str, bytes]:
    """The accepted-tree members of every Contract this package's operations pin.

    Keyed by tree path (``contracts/<name>/v<N>.json``), valued by the exact
    canonical bytes: an operator proposes these as members, unchanged, before
    installing the package.
    """

    return {
        f"contracts/{path.relative_to(CONTRACTS_DIR).as_posix()}": path.read_bytes()
        for path in sorted(CONTRACTS_DIR.rglob("*.json"))
    }


__all__ = [
    "CONTAINER_DIR",
    "CONTRACTS_DIR",
    "FETCH_OPERATION",
    "FIXTURE_HOST",
    "MANIFEST_PATH",
    "PACKAGE_ROOT",
    "RENDER_DOM_OPERATION",
    "RENDER_OPERATION",
    "SEARCH_OPERATION",
    "SearxngSearch",
    "WebFetch",
    "WebRender",
    "WebRenderDom",
    "__version__",
    "contract_members",
    "load_recordings",
]
