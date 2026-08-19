"""Golden tests for both identity digests.

These goldens are this repository's own and are taken over **synthetic**
fixtures. That is the point: a golden over a real dependency set moves whenever
a dependency is bumped, and a golden that moves routinely stops being read.
These move only when a preimage definition changes — which is exactly the change
that must never happen quietly, because it re-keys every track record.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cruxible_provider_runtime.digests import (
    IMPLEMENTATION_DOMAIN_TAG,
    MATERIALIZATION_DOMAIN_TAG,
    container_materialization_digest,
    implementation_digest,
    implementation_preimage,
    materialization_digest,
    materialization_preimage,
)
from cruxible_provider_runtime.resolution import MarkerEnvironment, UvLock, resolve

GOLDEN_PATH = Path(__file__).parent / "fixtures" / "golden" / "expected-digests.json"

IMPLEMENTATION_CASES = {
    "sample-echo": {
        "interface_id": "sample.echo",
        "interface_digest": "sha256:" + "1a" * 32,
        "entrypoint": "sample_provider.impl:Echo",
        "distribution_sha256": "sha256:" + "2b" * 32,
    },
    "sample-echo-other-entrypoint": {
        "interface_id": "sample.echo",
        "interface_digest": "sha256:" + "1a" * 32,
        "entrypoint": "sample_provider.impl:EchoFast",
        "distribution_sha256": "sha256:" + "2b" * 32,
    },
    "sample-echo-other-distribution": {
        "interface_id": "sample.echo",
        "interface_digest": "sha256:" + "1a" * 32,
        "entrypoint": "sample_provider.impl:Echo",
        "distribution_sha256": "sha256:" + "3c" * 32,
    },
}


@pytest.fixture(scope="session")
def golden() -> dict[str, dict[str, str]]:
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


def test_domain_tags_are_the_contract_spelling() -> None:
    assert IMPLEMENTATION_DOMAIN_TAG == "cruxible.provider.implementation.v1"
    assert MATERIALIZATION_DOMAIN_TAG == "cruxible.provider.materialization.v1"


@pytest.mark.parametrize("case", sorted(IMPLEMENTATION_CASES))
def test_implementation_digest_golden(case: str, golden: dict[str, dict[str, str]]) -> None:
    assert implementation_digest(**IMPLEMENTATION_CASES[case]) == golden["implementation"][case]


def test_implementation_preimage_has_exactly_four_fields() -> None:
    preimage = implementation_preimage(**IMPLEMENTATION_CASES["sample-echo"])
    assert sorted(preimage) == [
        "distribution_sha256",
        "entrypoint",
        "interface_digest",
        "interface_id",
    ]


def test_implementation_digest_ignores_version_and_backend() -> None:
    """A version bump that does not change the artifact cannot change identity.

    The preimage has no ``version`` field at all, which is the structural form of
    "version is a claim, not an identity".
    """

    base = dict(IMPLEMENTATION_CASES["sample-echo"])
    assert implementation_digest(**base) == implementation_digest(**base)
    changed = {**base, "distribution_sha256": "sha256:" + "3c" * 32}
    assert implementation_digest(**changed) != implementation_digest(**base)


@pytest.mark.parametrize("env_id", ["linux-cp311", "macos-arm-cp311", "windows-cp312"])
def test_materialization_digest_golden(
    env_id: str,
    golden: dict[str, dict[str, str]],
    golden_lock: UvLock,
    marker_environments: dict[str, MarkerEnvironment],
) -> None:
    resolved = resolve(golden_lock, "sample-provider", marker_environments[env_id])
    assert materialization_digest(resolved) == golden["materialization"][env_id]


def test_materialization_digest_differs_per_environment(
    golden_lock: UvLock, marker_environments: dict[str, MarkerEnvironment]
) -> None:
    digests = {
        env_id: materialization_digest(resolve(golden_lock, "sample-provider", env))
        for env_id, env in marker_environments.items()
    }
    assert len(set(digests.values())) == len(digests)


def test_materialization_preimage_hashes_the_resolution_not_the_lock_bytes(
    golden_lock: UvLock, linux_env: MarkerEnvironment
) -> None:
    resolved = resolve(golden_lock, "sample-provider", linux_env)
    preimage = materialization_preimage(resolved)
    assert sorted(preimage) == ["marker_environment", "resolved"]
    rendered = json.dumps(preimage)
    assert "revision" not in rendered, "lock-format metadata must not enter the preimage"
    assert "upload-time" not in rendered
    assert preimage["resolved"] == sorted(preimage["resolved"])


def test_marker_environment_label_does_not_enter_the_preimage(
    golden_lock: UvLock, linux_env: MarkerEnvironment
) -> None:
    relabelled = linux_env.model_copy(update={"id": "a-different-label"})
    a = materialization_digest(resolve(golden_lock, "sample-provider", linux_env))
    b = materialization_digest(resolve(golden_lock, "sample-provider", relabelled))
    assert a == b


def test_container_materialization_digest_is_the_image_digest() -> None:
    image = "sha256:" + "9f" * 32
    assert container_materialization_digest(image) == image
    assert container_materialization_digest(image.removeprefix("sha256:")) == image
