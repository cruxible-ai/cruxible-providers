"""Shared fixtures for the runtime conformance suite."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from cruxible_provider_runtime.resolution import MarkerEnvironment, UvLock, load_uv_lock

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN = FIXTURES / "golden"


@pytest.fixture(scope="session")
def golden_lock() -> UvLock:
    return load_uv_lock(GOLDEN / "sample.uv.lock")


@pytest.fixture(scope="session")
def marker_environments() -> dict[str, MarkerEnvironment]:
    document = json.loads((GOLDEN / "marker-environments.json").read_text(encoding="utf-8"))
    return {key: MarkerEnvironment.model_validate(value) for key, value in document.items()}


@pytest.fixture()
def linux_env(marker_environments: dict[str, MarkerEnvironment]) -> MarkerEnvironment:
    return marker_environments["linux-cp311"]
