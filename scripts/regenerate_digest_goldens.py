#!/usr/bin/env python
"""Regenerate the digest goldens from the synthetic fixtures.

Run deliberately, and read the diff. A moved digest golden means a preimage
definition changed, which re-keys every track record pinned to the old value —
so the diff is the review, not a formality.

    uv run python scripts/regenerate_digest_goldens.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS = REPO_ROOT / "packages" / "cruxible-provider-runtime" / "tests"
sys.path.insert(0, str(REPO_ROOT / "packages" / "cruxible-provider-runtime" / "src"))

from cruxible_provider_runtime.digests import (  # noqa: E402
    implementation_digest,
    materialization_digest,
)
from cruxible_provider_runtime.resolution import (  # noqa: E402
    MarkerEnvironment,
    load_uv_lock,
    resolve,
)

GOLDEN_DIR = TESTS / "fixtures" / "golden"


def main() -> int:
    cases: dict[str, dict[str, str]] = json.loads(
        (GOLDEN_DIR / "implementation-cases.json").read_text(encoding="utf-8")
    )["cases"]
    lock = load_uv_lock(GOLDEN_DIR / "sample.uv.lock")
    environments = {
        key: MarkerEnvironment.model_validate(value)
        for key, value in json.loads(
            (GOLDEN_DIR / "marker-environments.json").read_text(encoding="utf-8")
        ).items()
    }
    document = {
        "implementation": {
            name: implementation_digest(**case) for name, case in sorted(cases.items())
        },
        "materialization": {
            env_id: materialization_digest(resolve(lock, "sample-provider", env))
            for env_id, env in sorted(environments.items())
        },
    }
    target = GOLDEN_DIR / "expected-digests.json"
    target.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {target.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
