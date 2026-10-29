"""The reference no-op Cruxible provider.

The smallest package that exercises every rule the provider protocol imposes.
It is a fixture with a job: a new plane package copies its shape, and the
conformance suite here is the suite every plane package inherits.
"""

from __future__ import annotations

from pathlib import Path

from .provider import CREDENTIAL_MISSING, CREDENTIAL_REF, OPERATION, PROVIDER_DECLINED, NoopEcho

__version__ = "0.3.0"

PACKAGE_ROOT = Path(__file__).resolve().parent
MANIFEST_PATH = PACKAGE_ROOT / "manifest.yaml"
CONTAINER_DIR = PACKAGE_ROOT.parent.parent / "container"

__all__ = [
    "CONTAINER_DIR",
    "CREDENTIAL_MISSING",
    "CREDENTIAL_REF",
    "MANIFEST_PATH",
    "OPERATION",
    "PACKAGE_ROOT",
    "PROVIDER_DECLINED",
    "NoopEcho",
    "__version__",
]
