"""Drift guards on the stub interface and its manifest transcription."""

from __future__ import annotations

from pathlib import Path

import yaml
from cruxible_provider_noop import interface
from cruxible_provider_runtime.manifest import ENTRYPOINT_GROUP, load_manifest


def test_the_pinned_interface_digest_still_matches_its_preimage() -> None:
    """A literal digest that no longer matches its preimage is drift, not a typo."""

    assert interface.recompute_interface_digest() == interface.INTERFACE_DIGEST


def test_the_manifest_pins_the_same_interface_digest(manifest_path: Path) -> None:
    manifest = load_manifest(manifest_path)
    assert manifest.implementation("noop.echo").interface_digest == interface.INTERFACE_DIGEST


def test_the_manifest_version_tracks_the_package_version(manifest_path: Path) -> None:
    import cruxible_provider_noop

    manifest = load_manifest(manifest_path)
    assert manifest.distribution.version == cruxible_provider_noop.__version__


def test_the_manifest_entrypoint_matches_the_declared_entry_point(manifest_path: Path) -> None:
    """The manifest and the packaging metadata must name the same object."""

    manifest = load_manifest(manifest_path)
    pyproject = Path(manifest_path).parents[2] / "pyproject.toml"
    import tomllib

    document = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    declared = document["project"]["entry-points"][ENTRYPOINT_GROUP]
    assert declared["noop.echo"] == manifest.implementation("noop.echo").entrypoint


def test_the_vocabulary_data_file_matches_the_registered_vocabulary() -> None:
    """The committed data file and the code-built vocabulary are one vocabulary."""

    repo_root = Path(interface.__file__).resolve().parents[4]
    path = repo_root / "vocab" / "stub" / "noop.echo.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    from cruxible_provider_runtime.buckets import BucketVocabulary

    assert BucketVocabulary.model_validate(document) == interface.VOCABULARY


def test_the_classifier_covers_every_bucket_in_the_cube() -> None:
    """Every declared class must be reachable from some input."""

    samples = {
        "payload_size=tiny;charset=ascii": {"text": "short"},
        "payload_size=tiny;charset=unicode": {"text": "héllo"},
        "payload_size=small;charset=ascii": {"text": "x" * 100},
        "payload_size=small;charset=unicode": {"text": "é" * 100},
        "payload_size=large;charset=ascii": {"text": "x" * 2000},
        "payload_size=large;charset=unicode": {"text": "é" * 2000},
    }
    derived = set()
    for payload in samples.values():
        assignment = interface.classify(payload)
        assert assignment is not None
        derived.add(interface.VOCABULARY.bucket_id(assignment))
    assert derived == set(interface.VOCABULARY.all_bucket_ids())
