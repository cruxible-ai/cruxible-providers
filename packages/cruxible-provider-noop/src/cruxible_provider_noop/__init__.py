"""The reference no-op Cruxible provider.

The smallest package that exercises every rule the provider protocol imposes.
It is a fixture with a job: a new plane package copies its shape, and the
conformance suite here is the suite every plane package inherits.
"""

from __future__ import annotations

from pathlib import Path

from .interface import INTERFACE_DIGEST, INTERFACE_ID, VOCABULARY, classify, registration
from .provider import CREDENTIAL_REF, NoopEcho

__version__ = "0.1.0"

PACKAGE_ROOT = Path(__file__).resolve().parent
MANIFEST_PATH = PACKAGE_ROOT / "manifest.yaml"
CONTAINER_DIR = PACKAGE_ROOT.parent.parent / "container"

__all__ = [
    "CONTAINER_DIR",
    "CREDENTIAL_REF",
    "INTERFACE_DIGEST",
    "INTERFACE_ID",
    "MANIFEST_PATH",
    "PACKAGE_ROOT",
    "VOCABULARY",
    "NoopEcho",
    "__version__",
    "classify",
    "registration",
]
