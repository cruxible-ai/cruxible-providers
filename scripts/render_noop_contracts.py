#!/usr/bin/env python
"""Write the Contracts the reference provider's operations carry into its registration.

``cruxible-provider-noop`` carries a copy of every Contract its operations reach
in the ``declares`` block of its ``registration.json`` (registration schema 3),
as the web plane does, so the reference operations install from the package
alone. ``noop.echo``'s four are exactly the ones Core's installer tests use (no
descriptions), so their digests do not move. Runs in Core's interpreter, like
``scripts/render_web_contracts.py``::

    "$CRUXIBLE_CORE_PYTHON" scripts/render_noop_contracts.py          # rewrite
    "$CRUXIBLE_CORE_PYTHON" scripts/render_noop_contracts.py --check  # exit 1 on drift
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.render_web_contracts import render, write  # noqa: E402

TARGET = REPO_ROOT / "packages/cruxible-provider-noop/src/cruxible_provider_noop"

#: ``captures.MAX_FILES``.
MAX_FILES = 16

#: Name to definition. ``noop.echo``'s match Core's ``noop_contract_files``.
DEFINITIONS: dict[str, dict[str, Any]] = {
    "noop.echo.request": {"fields": {"mode": "string?", "text": "string?"}},
    "noop.echo.result": {
        "fields": {
            "echo": "string",
            "credential_length": "int?",
            "credential_fingerprint": "string?",
            "context_repr": "string?",
        }
    },
    "noop.echo.refusal.credential_missing": {"fields": {"ref": "string"}},
    "noop.echo.refusal.provider_declined": {
        "fields": {
            "mode": "string?",
            "known": "list<string>{max_items: 32}?",
            "run_id": "string?",
        }
    },
    "noop.captures.request": {
        "description": (
            "The recorded files to return, in order: each is one request and becomes "
            "its own Capture."
        ),
        "fields": {"files": f"list<string>{{max_items: {MAX_FILES}}}"},
    },
    "noop.captures.result": {
        "description": (
            "One Capture per named file, in order: each member is the file's exact bytes, "
            "identified by its coordinate (the file and a request digest)."
        ),
        "fields": {"captures": f"list<capture<*>>{{max_items: {MAX_FILES}}}"},
    },
    "noop.captures.refusal.provider_declined": {
        "description": (
            "Detail of a provider_declined refusal: for an unknown file, its name and "
            "the recorded files there are."
        ),
        "fields": {
            "file": "string?",
            "known": f"list<string>{{max_items: {MAX_FILES}}}?",
        },
    },
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="exit 1 if the file drifted")
    args = parser.parse_args(argv)
    return write(
        TARGET, render(DEFINITIONS), check=args.check, script="scripts/render_noop_contracts.py"
    )


if __name__ == "__main__":
    raise SystemExit(main())
