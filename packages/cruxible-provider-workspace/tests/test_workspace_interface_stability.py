"""Drift guards on the stub interface, its vocabulary, and its manifest transcription."""

from __future__ import annotations

import tomllib
from pathlib import Path

import cruxible_provider_workspace
from cruxible_provider_runtime.manifest import ENTRYPOINT_GROUP, load_manifest
from cruxible_provider_runtime.registry import load_bucket_vocabulary
from cruxible_provider_workspace import interface

from .conftest import INTERFACE_ID, PACKAGE_DIR, REPO_ROOT


def test_the_pinned_interface_digest_still_matches_its_preimage() -> None:
    """A literal digest that no longer matches its preimage is drift, not a typo."""

    assert interface.recompute_interface_digest() == interface.INTERFACE_DIGEST


def test_the_preimage_declares_the_pure_effect_class() -> None:
    """RAT-9: the interface itself says pure, not only the manifest."""

    assert interface.INTERFACE_PREIMAGE["effect_class"] == "pure"


def test_the_preimage_names_every_run_input_field_and_nothing_else() -> None:
    from cruxible_provider_workspace.file import INPUT_FIELDS

    assert tuple(interface.INTERFACE_PREIMAGE["input"]) == INPUT_FIELDS


def test_the_manifest_pins_the_same_interface_digest(manifest_path: Path) -> None:
    manifest = load_manifest(manifest_path)
    assert manifest.implementation(INTERFACE_ID).interface_digest == interface.INTERFACE_DIGEST


def test_the_manifest_version_tracks_the_package_version(manifest_path: Path) -> None:
    manifest = load_manifest(manifest_path)
    assert manifest.distribution.version == cruxible_provider_workspace.__version__
    pyproject = tomllib.loads((PACKAGE_DIR / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["version"] == cruxible_provider_workspace.__version__


def test_the_manifest_entrypoint_matches_the_declared_entry_point(manifest_path: Path) -> None:
    """The manifest and the packaging metadata must name the same object."""

    manifest = load_manifest(manifest_path)
    pyproject = tomllib.loads((PACKAGE_DIR / "pyproject.toml").read_text(encoding="utf-8"))
    declared = pyproject["project"]["entry-points"][ENTRYPOINT_GROUP]
    assert declared[INTERFACE_ID] == manifest.implementation(INTERFACE_ID).entrypoint


def test_the_manifest_spells_the_pure_effect_class(manifest_path: Path) -> None:
    """Zero endpoints, deterministic, no side effects: pure, the way a manifest can say it."""

    implementation = load_manifest(manifest_path).implementation(INTERFACE_ID)
    assert implementation.declared_endpoints == ()
    assert implementation.requires_extras == ()
    assert implementation.deterministic is True
    assert implementation.side_effects is False
    assert implementation.backends == ("local_env", "container")


def test_the_shipped_vocabulary_matches_the_code_built_one() -> None:
    """The committed data file and the code-built vocabulary are one vocabulary."""

    shipped = Path(interface.__file__).parent / "vocab" / "workspace.file.yaml"
    assert load_bucket_vocabulary(shipped) == interface.VOCABULARY


def test_the_published_vocabulary_matches_the_code_built_one() -> None:
    published = REPO_ROOT / "vocab" / "interfaces" / "workspace.file.yaml"
    assert load_bucket_vocabulary(published) == interface.VOCABULARY


def test_the_classifier_covers_every_bucket_in_the_cube() -> None:
    """Every declared class must be reachable from some input."""

    import base64

    def payload(data: bytes) -> dict[str, object]:
        return {"content_encoding": "base64", "bytes": base64.b64encode(data).decode("ascii")}

    samples = {
        "content_kind=text;byte_size=tiny": payload(b"hello\n"),
        "content_kind=text;byte_size=small": payload(b"x" * 5_000),
        "content_kind=text;byte_size=medium": payload(b"x" * 70_000),
        "content_kind=text;byte_size=large": payload(b"x" * 1_100_000),
        "content_kind=binary;byte_size=tiny": payload(b"\x00\xff"),
        "content_kind=binary;byte_size=small": payload(b"\xff" * 5_000),
        "content_kind=binary;byte_size=medium": payload(b"\xff" * 70_000),
        "content_kind=binary;byte_size=large": payload(b"\xff" * 1_100_000),
    }
    derived = set()
    for bucket, sample in samples.items():
        assignment = interface.classify(sample)
        assert assignment is not None
        assert interface.VOCABULARY.bucket_id(assignment) == bucket
        derived.add(bucket)
    assert derived == set(interface.VOCABULARY.all_bucket_ids())


def test_the_size_classes_sit_exactly_on_their_ceilings() -> None:
    assert interface.byte_size_class(0) == "tiny"
    assert interface.byte_size_class(4_096) == "tiny"
    assert interface.byte_size_class(4_097) == "small"
    assert interface.byte_size_class(65_536) == "small"
    assert interface.byte_size_class(65_537) == "medium"
    assert interface.byte_size_class(1_048_576) == "medium"
    assert interface.byte_size_class(1_048_577) == "large"


def test_content_kind_is_measured_not_declared() -> None:
    assert interface.content_kind_class(b"") == "text"
    assert interface.content_kind_class("héllo".encode()) == "text"
    assert interface.content_kind_class(b"text with a \x00 in it") == "binary"
    assert interface.content_kind_class(b"\xff\xfe") == "binary"
    assert interface.content_kind_class("héllo".encode("latin-1")) == "binary"
