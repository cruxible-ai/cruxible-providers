"""Drift guards on the stub interfaces and the manifest transcription."""

from __future__ import annotations

import tomllib
from pathlib import Path

import cruxible_provider_docs
import pytest
from cruxible_provider_docs import interfaces
from cruxible_provider_runtime.buckets import BucketVocabulary
from cruxible_provider_runtime.manifest import ENTRYPOINT_GROUP, ProviderManifest
from cruxible_provider_runtime.registry import load_bucket_vocabulary

STUBS = [
    ("doc.to_markdown", interfaces.MARKDOWN_PREIMAGE, interfaces.MARKDOWN_INTERFACE_DIGEST),
    ("ocr.extract", interfaces.OCR_PREIMAGE, interfaces.OCR_INTERFACE_DIGEST),
]


@pytest.mark.parametrize(("interface_id", "preimage", "digest"), STUBS, ids=lambda v: str(v)[:20])
def test_the_pinned_interface_digest_still_matches_its_preimage(
    interface_id: str, preimage: dict[str, object], digest: str
) -> None:
    assert interfaces.recompute_interface_digest(preimage) == digest
    assert preimage["interface_id"] == interface_id


@pytest.mark.parametrize(("interface_id", "preimage", "digest"), STUBS, ids=lambda v: str(v)[:20])
def test_the_manifest_pins_the_same_interface_digest(
    manifest: ProviderManifest, interface_id: str, preimage: dict[str, object], digest: str
) -> None:
    del preimage
    assert manifest.implementation(interface_id).interface_digest == digest


def test_the_manifest_version_tracks_the_package_version(manifest: ProviderManifest) -> None:
    assert manifest.distribution.version == cruxible_provider_docs.__version__


def test_the_manifest_entrypoints_match_the_declared_entry_points(
    manifest: ProviderManifest, manifest_path: Path
) -> None:
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
    pyproject = tomllib.loads(
        (manifest_path.parents[2] / "pyproject.toml").read_text(encoding="utf-8")
    )
    available = set(pyproject["project"].get("optional-dependencies", {}))
    for implementation in manifest.implementations:
        assert implementation.requires_extras, (
            f"{implementation.interface_id} carries an engine and must declare its extra"
        )
        assert set(implementation.requires_extras) <= available


def test_the_two_implementations_require_different_extras(manifest: ProviderManifest) -> None:
    """The property the whole extras mechanism exists to support."""

    markdown = manifest.implementation("doc.to_markdown").requires_extras
    ocr = manifest.implementation("ocr.extract").requires_extras
    assert set(markdown).isdisjoint(ocr)


@pytest.mark.parametrize("interface_id", ["doc.to_markdown", "ocr.extract"])
def test_the_shipped_vocabulary_matches_the_published_one(interface_id: str) -> None:
    """The distribution's copy and the repository's draft are one document."""

    repo_root = Path(interfaces.__file__).resolve().parents[4]
    published = load_bucket_vocabulary(repo_root / "vocab" / "interfaces" / f"{interface_id}.yaml")
    shipped: BucketVocabulary = (
        interfaces.MARKDOWN_VOCABULARY
        if interface_id == "doc.to_markdown"
        else interfaces.OCR_VOCABULARY
    )
    assert shipped == published


def test_the_markdown_classifier_reaches_every_format() -> None:
    samples = {
        "pdf": "report.pdf",
        "office": "report.docx",
        "html": "report.html",
        "plain_text": "report.md",
        "email": "message.eml",
    }
    for expected, filename in samples.items():
        payload = {"source": {"kind": "inline", "filename": filename}}
        assert (interfaces.classify_to_markdown(payload) or {})["format"] == expected


def test_the_media_type_wins_over_the_filename() -> None:
    """A filename is a hint; a media type is a statement."""

    payload = {"source": {"kind": "inline", "filename": "notes", "media_type": "application/pdf"}}
    assert (interfaces.classify_to_markdown(payload) or {})["format"] == "pdf"


@pytest.mark.parametrize(
    ("pages", "expected"),
    [(1, "single"), (2, "short"), (10, "short"), (11, "medium"), (100, "medium"), (101, "long")],
)
def test_the_page_count_classes_partition_the_range(pages: int, expected: str) -> None:
    assert interfaces.page_count_class(pages) == expected


def test_the_ocr_classifier_reaches_its_declared_classes() -> None:
    base = {"source": {"kind": "inline", "filename": "page.png"}}
    assert (interfaces.classify_ocr(base) or {})["script"] == "latin"
    assert (interfaces.classify_ocr({**base, "script": "cjk"}) or {})["script"] == "cjk"
    assert (interfaces.classify_ocr({**base, "scan_quality": "handwritten"}) or {})[
        "scan_quality"
    ] == "handwritten"


def test_an_input_with_nothing_classifiable_returns_none() -> None:
    """The classifier says "I cannot place this" rather than guessing a bucket."""

    assert interfaces.classify_to_markdown({"page_count": 1}) is None
    assert interfaces.classify_to_markdown({"source": {"kind": "inline"}}) is None
    assert (
        interfaces.classify_to_markdown(
            {"source": {"kind": "inline", "filename": "x.pdf"}, "scanned": "smudged"}
        )
        is None
    )
    assert interfaces.classify_ocr({"page_count": 1}) is None
