"""Cruxible provider adapters for the web plane.

Three operations, each served by its own entrypoint. ``web.fetch`` retrieves
one resource the run names and ``search.web`` queries a configured SearXNG
instance; each returns the body the origin sent as its Capture, verbatim, with
how it was obtained in the typed HTTP coordinate. ``web.render`` reads a fetched
Capture and returns a derived Capture citing it (an assembled DOM, or the main
content as Markdown). The heavy engine — a browser — lives behind the
``browser`` extra, which the ``web.render`` implementation's manifest row
declares and the resolver materializes into that implementation's environment
and no other.

The package also ships the bodies of every Contract its operations pin, under
``contracts/`` (``contract_members``): each file is the accepted-tree member an
operator accepts before installing the package, and the install pins the
operations to them.
"""

from __future__ import annotations

from pathlib import Path

from .fetch import WebFetch
from .operations import FETCH_OPERATION, RENDER_OPERATION, SEARCH_OPERATION
from .recordings import FIXTURE_HOST, load_recordings
from .render import WebRender
from .search import SearxngSearch

__version__ = "0.4.0"

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
    "RENDER_OPERATION",
    "SEARCH_OPERATION",
    "SearxngSearch",
    "WebFetch",
    "WebRender",
    "__version__",
    "contract_members",
    "load_recordings",
]
