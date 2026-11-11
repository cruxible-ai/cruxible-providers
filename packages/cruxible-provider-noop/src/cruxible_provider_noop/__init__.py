"""The reference no-op Cruxible provider.

The smallest package that exercises every rule the provider protocol imposes.
It is a fixture with a job: a new plane package copies its shape, and the
conformance suite here is the suite every plane package inherits.

Two operations, each served by its own entrypoint: ``noop.echo`` reaches every
branch the protocol has to survive, and ``noop.captures`` is the reference for
an operation returning several Captures from one call (``list<capture<*>>``).
The package ships the Contracts both pin under ``contracts/``
(``contract_members``).
"""

from __future__ import annotations

from pathlib import Path

from .captures import OPERATION as CAPTURES_OPERATION
from .captures import NoopCaptures
from .provider import CREDENTIAL_MISSING, CREDENTIAL_REF, OPERATION, PROVIDER_DECLINED, NoopEcho

__version__ = "0.4.0"

PACKAGE_ROOT = Path(__file__).resolve().parent
MANIFEST_PATH = PACKAGE_ROOT / "manifest.yaml"
CONTAINER_DIR = PACKAGE_ROOT.parent.parent / "container"
CONTRACTS_DIR = PACKAGE_ROOT / "contracts"
"""Each Contract at ``<name>/v<N>.json``, exactly as Core stores it under ``contracts/``."""


def contract_members() -> dict[str, bytes]:
    """The accepted-tree members of every Contract this package's operations pin."""

    return {
        f"contracts/{path.relative_to(CONTRACTS_DIR).as_posix()}": path.read_bytes()
        for path in sorted(CONTRACTS_DIR.rglob("*.json"))
    }


__all__ = [
    "CAPTURES_OPERATION",
    "CONTAINER_DIR",
    "CONTRACTS_DIR",
    "CREDENTIAL_MISSING",
    "CREDENTIAL_REF",
    "MANIFEST_PATH",
    "OPERATION",
    "PACKAGE_ROOT",
    "PROVIDER_DECLINED",
    "NoopCaptures",
    "NoopEcho",
    "__version__",
    "contract_members",
]
