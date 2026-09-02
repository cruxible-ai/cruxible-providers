"""Per-bucket conformance fixtures, as shipped data.

Every claimed bucket needs a fixture that passes, and this adapter's fixtures are
complete inputs: the exact base64 payload, its declared length and digest, and
the digest of the body the adapter must produce for it. Core's seed bundle
carries these bytes, and core's classifier re-proof classifies the same inputs
and expects the same buckets.

Loaded only by tests and by registration-time checks. The adapter itself never
reads a file -- see ``file.py``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["FIXTURES_DIR", "BucketFixture", "load_fixtures"]

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


class BucketFixture(BaseModel):
    """One claimed bucket, the exact input that measures into it, and what it must produce."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    note: str
    interface_id: str
    bucket_selector: str
    bucket_id: str
    input: dict[str, Any] = Field(default_factory=dict)
    expect: dict[str, Any] = Field(default_factory=dict)


@lru_cache(maxsize=1)
def load_fixtures() -> Mapping[str, BucketFixture]:
    """Every bucket fixture shipped in this distribution, keyed by id."""

    loaded: dict[str, BucketFixture] = {}
    for path in sorted(FIXTURES_DIR.glob("*.json")):
        fixture = BucketFixture.model_validate(json.loads(path.read_text(encoding="utf-8")))
        if fixture.id in loaded:  # pragma: no cover - defensive
            raise ValueError(f"two fixture files share the id {fixture.id!r}")
        if path.stem != fixture.id:  # pragma: no cover - defensive
            raise ValueError(f"fixture {fixture.id!r} lives in a file named {path.name!r}")
        loaded[fixture.id] = fixture
    return loaded
