"""Drift guards on the stub interfaces and the manifest transcription."""

from __future__ import annotations

import tomllib
from pathlib import Path

import cruxible_provider_web
import pytest
from cruxible_provider_runtime.buckets import BucketVocabulary
from cruxible_provider_runtime.manifest import ENTRYPOINT_GROUP, ProviderManifest
from cruxible_provider_runtime.registry import load_bucket_vocabulary
from cruxible_provider_web import interfaces

STUBS = [
    ("web.fetch", interfaces.FETCH_PREIMAGE, interfaces.FETCH_INTERFACE_DIGEST),
    ("search.web", interfaces.SEARCH_PREIMAGE, interfaces.SEARCH_INTERFACE_DIGEST),
]


@pytest.mark.parametrize(("interface_id", "preimage", "digest"), STUBS, ids=lambda v: str(v)[:24])
def test_the_pinned_interface_digest_still_matches_its_preimage(
    interface_id: str, preimage: dict[str, object], digest: str
) -> None:
    """A literal digest that no longer matches its preimage is drift, not a typo."""

    assert interfaces.recompute_interface_digest(preimage) == digest
    assert preimage["interface_id"] == interface_id


@pytest.mark.parametrize(("interface_id", "preimage", "digest"), STUBS, ids=lambda v: str(v)[:24])
def test_the_manifest_pins_the_same_interface_digest(
    manifest: ProviderManifest, interface_id: str, preimage: dict[str, object], digest: str
) -> None:
    del preimage
    assert manifest.implementation(interface_id).interface_digest == digest


def test_the_manifest_version_tracks_the_package_version(manifest: ProviderManifest) -> None:
    assert manifest.distribution.version == cruxible_provider_web.__version__


def test_the_manifest_entrypoints_match_the_declared_entry_points(
    manifest: ProviderManifest, manifest_path: Path
) -> None:
    """The manifest and the packaging metadata must name the same objects."""

    pyproject = manifest_path.parents[2] / "pyproject.toml"
    declared = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["entry-points"][
        ENTRYPOINT_GROUP
    ]
    for implementation in manifest.implementations:
        assert declared[implementation.interface_id] == implementation.entrypoint
    assert set(declared) == {impl.interface_id for impl in manifest.implementations}


def test_the_declared_extras_exist_in_the_distribution(
    manifest: ProviderManifest, manifest_path: Path
) -> None:
    """An implementation cannot require an engine the distribution cannot install.

    The bind path refuses this against the lock; this catches it against the
    project metadata, which is where it would be introduced.
    """

    pyproject = tomllib.loads(
        (manifest_path.parents[2] / "pyproject.toml").read_text(encoding="utf-8")
    )
    available = set(pyproject["project"].get("optional-dependencies", {}))
    for implementation in manifest.implementations:
        assert set(implementation.requires_extras) <= available


@pytest.mark.parametrize("interface_id", ["web.fetch", "search.web"])
def test_the_shipped_vocabulary_matches_the_published_one(interface_id: str) -> None:
    """The distribution's copy and the repository's draft are one document.

    The package ships its own copy because an installed provider classifies
    without a repository to read; that copy is a duplicate, and a duplicate that
    nothing compares is a fork waiting to happen.
    """

    repo_root = Path(interfaces.__file__).resolve().parents[4]
    published = load_bucket_vocabulary(repo_root / "vocab" / "interfaces" / f"{interface_id}.yaml")
    shipped: BucketVocabulary = (
        interfaces.FETCH_VOCABULARY if interface_id == "web.fetch" else interfaces.SEARCH_VOCABULARY
    )
    assert shipped == published


def test_the_fetch_classifier_reaches_every_source_kind() -> None:
    """Every declared class must be reachable from some input."""

    samples = {
        "static_html": {"url": "https://example.test/page"},
        "js_rendered": {"url": "https://example.test/app", "render": True},
        "api_json": {"url": "https://example.test/api/v1/things"},
        "binary": {"url": "https://example.test/report.pdf"},
    }
    derived = {
        key: (interfaces.classify_web_fetch(payload) or {}).get("source_kind")
        for key, payload in samples.items()
    }
    assert derived == {key: key for key in samples}


def test_the_fetch_classifier_reaches_every_access_and_weight_class() -> None:
    base = {"url": "https://example.test/page"}
    assert (interfaces.classify_web_fetch(base) or {})["access"] == "public"
    assert (interfaces.classify_web_fetch({**base, "paced": True}) or {})[
        "access"
    ] == "rate_limited"
    assert (interfaces.classify_web_fetch({**base, "credential_ref": "web.token"}) or {})[
        "access"
    ] == "authenticated"
    weights = {
        "light": 1024,
        "medium": 1024 * 1024,
        "heavy": 8 * 1024 * 1024,
    }
    for expected, cap in weights.items():
        assert (interfaces.classify_web_fetch({**base, "max_bytes": cap}) or {})[
            "page_weight"
        ] == expected


def test_the_search_classifier_reaches_every_class() -> None:
    forms = {
        "keyword": {"query": "tide gauge"},
        "natural_language": {"query": "how has the Newlyn gauge drifted since 1915?"},
        "boolean": {"query": "tide AND gauge site:example.test"},
    }
    for expected, payload in forms.items():
        assert (interfaces.classify_search(payload) or {})["query_form"] == expected

    recencies = {"any_time": None, "realtime": 6, "recent": 24 * 30}
    for expected, hours in recencies.items():
        payload = {"query": "tide gauge"} | ({"max_age_hours": hours} if hours else {})
        assert (interfaces.classify_search(payload) or {})["recency"] == expected

    depths = {"shallow": 10, "standard": 25, "deep": 100}
    for expected, limit in depths.items():
        assert (interfaces.classify_search({"query": "tide gauge", "limit": limit}) or {})[
            "result_depth"
        ] == expected


def test_an_input_with_nothing_classifiable_returns_none() -> None:
    """The classifier says "I cannot place this" rather than guessing a bucket."""

    assert interfaces.classify_web_fetch({"render": True}) is None
    assert interfaces.classify_search({"limit": 10}) is None
