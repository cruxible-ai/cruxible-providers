"""The ``workspace.file`` built-in Source adapter.

Core reads the file under the G5 law; this package structures the bytes it was
handed into a capture body, and does nothing else. It is the first built-in that
enters an instance by the seed-by-proposal verb (G6), and the package layout is
the ordinary plane layout so that nothing about being a built-in is special on
the rails side.
"""

from __future__ import annotations

from pathlib import Path

from .file import WorkspaceFile, structure_bytes
from .interface import INTERFACE_DIGEST, INTERFACE_ID, VOCABULARY, classify, registration

__version__ = "0.1.0"

PACKAGE_ROOT = Path(__file__).resolve().parent
MANIFEST_PATH = PACKAGE_ROOT / "manifest.yaml"
CONTAINER_DIR = PACKAGE_ROOT.parent.parent / "container"

__all__ = [
    "CONTAINER_DIR",
    "INTERFACE_DIGEST",
    "INTERFACE_ID",
    "MANIFEST_PATH",
    "PACKAGE_ROOT",
    "VOCABULARY",
    "WorkspaceFile",
    "__version__",
    "classify",
    "registration",
    "structure_bytes",
]
