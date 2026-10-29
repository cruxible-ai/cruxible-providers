"""The ``workspace.file`` built-in Source adapter.

Core reads the file under the G5 law; this package structures the bytes it was
handed into a capture body, and does nothing else. It serves one operation,
``workspace.file``, and the package layout is the ordinary plane layout so that
nothing about being a built-in is special on the rails side.
"""

from __future__ import annotations

from pathlib import Path

from .file import OPERATION, WorkspaceFile, input_bucket_label, structure_bytes

__version__ = "0.3.0"

PACKAGE_ROOT = Path(__file__).resolve().parent
MANIFEST_PATH = PACKAGE_ROOT / "manifest.yaml"
CONTAINER_DIR = PACKAGE_ROOT.parent.parent / "container"

__all__ = [
    "CONTAINER_DIR",
    "MANIFEST_PATH",
    "OPERATION",
    "PACKAGE_ROOT",
    "WorkspaceFile",
    "__version__",
    "input_bucket_label",
    "structure_bytes",
]
