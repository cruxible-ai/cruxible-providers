"""The launch bucket vocabularies, as data.

RP-0 ships the format and the launch vocabularies; core registers them. These
tests are what "ships as data" has to mean if it is to mean anything: the files
parse under the published schema, they are internally consistent, and they are
honestly marked draft.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from cruxible_provider_runtime.buckets import BucketVocabulary
from cruxible_provider_runtime.registry import load_bucket_vocabularies, load_bucket_vocabulary

REPO_ROOT = Path(__file__).resolve().parent.parent
VOCAB_DIR = REPO_ROOT / "vocab"
INTERFACES = VOCAB_DIR / "interfaces"
SCHEMA_PATH = VOCAB_DIR / "bucket-vocabulary.schema.json"

LAUNCH_INTERFACES = {
    "calc.calibrate",
    "calc.reduce",
    "db.cdc_read",
    "db.row_select",
    "doc.to_markdown",
    "effect.notify",
    "feed.fetch",
    "match.record",
    "match.semantic",
    "ocr.extract",
    "sbom.parse",
    "score.rank",
    "search.web",
    "stat.test",
    "text.classify",
    "text.extract_structured",
    "ts.anomaly",
    "ts.forecast",
    "vcs.events",
    "web.fetch",
}

QUANTITATIVE = {
    "calc.calibrate",
    "calc.reduce",
    "match.record",
    "score.rank",
    "stat.test",
    "ts.anomaly",
    "ts.forecast",
}

DOCUMENT_SLOTS = {"doc.to_markdown", "ocr.extract"}

VOCABULARY_FILES = sorted(INTERFACES.glob("*.yaml"))


def test_every_launch_interface_has_a_vocabulary() -> None:
    loaded = load_bucket_vocabularies(INTERFACES)
    assert set(loaded) == LAUNCH_INTERFACES


@pytest.mark.parametrize("path", VOCABULARY_FILES, ids=lambda p: p.stem)
def test_vocabulary_parses(path: Path) -> None:
    vocabulary = load_bucket_vocabulary(path)
    assert vocabulary.interface_id == path.stem


@pytest.mark.parametrize("path", VOCABULARY_FILES, ids=lambda p: p.stem)
def test_vocabulary_is_marked_draft(path: Path) -> None:
    """Acceptance happens in core. Nothing here may pre-declare itself accepted."""

    assert load_bucket_vocabulary(path).status == "draft"


@pytest.mark.parametrize("path", VOCABULARY_FILES, ids=lambda p: p.stem)
def test_every_class_carries_a_description(path: Path) -> None:
    vocabulary = load_bucket_vocabulary(path)
    assert vocabulary.description.strip()
    for dimension in vocabulary.dimensions:
        assert dimension.description.strip()
        for bucket_class in dimension.classes:
            assert bucket_class.description.strip(), (
                f"{path.stem}/{dimension.name}/{bucket_class.id} has no description; "
                "an undescribed class is a class nobody can classify into consistently"
            )


@pytest.mark.parametrize("path", VOCABULARY_FILES, ids=lambda p: p.stem)
def test_every_bucket_id_round_trips(path: Path) -> None:
    vocabulary = load_bucket_vocabulary(path)
    for bucket in vocabulary.all_bucket_ids():
        assignment = dict(segment.split("=", 1) for segment in bucket.split(";"))
        assert vocabulary.bucket_id(assignment) == bucket


@pytest.mark.parametrize("interface_id", sorted(QUANTITATIVE))
def test_quantitative_slots_carry_the_most_detail(interface_id: str) -> None:
    """The quant slots are where narrow ML will later compete on the same key."""

    vocabulary = load_bucket_vocabulary(INTERFACES / f"{interface_id}.yaml")
    assert len(vocabulary.dimensions) >= 4, (
        f"{interface_id} has {len(vocabulary.dimensions)} dimensions; a quantitative "
        "slot too coarse to separate competence hides the comparison it exists for"
    )


@pytest.mark.parametrize("interface_id", sorted({"ts.anomaly", "ts.forecast"}))
def test_series_slots_declare_frequency_length_and_domain(interface_id: str) -> None:
    vocabulary = load_bucket_vocabulary(INTERFACES / f"{interface_id}.yaml")
    assert {"frequency", "series_length", "domain_class"} <= set(vocabulary.dimension_names)


@pytest.mark.parametrize("interface_id", sorted(DOCUMENT_SLOTS))
def test_document_slots_declare_format_scan_and_page_count(interface_id: str) -> None:
    vocabulary = load_bucket_vocabulary(INTERFACES / f"{interface_id}.yaml")
    names = set(vocabulary.dimension_names)
    assert "page_count" in names
    assert "layout" in names
    assert names & {"format", "scanned", "scan_quality", "script"}


# A PROVISIONAL guardrail, fitted to the launch vocabularies rather than derived
# from anything. The largest launch cube (ts.anomaly) enumerates 4800 buckets, so
# this ceiling sits just above what already exists: it will catch an accidental
# dimension explosion and will not catch a merely-large vocabulary. It is a
# tripwire against carelessness, not a considered limit, and whoever finds it
# blocking a genuine design should move it and say so rather than work around it.
BUCKET_CUBE_CEILING = 5000


def test_cube_sizes_stay_bounded() -> None:
    """A ceiling on the cube, not on what gets fixtured.

    Conformance fixtures are required per declared *selector*, and a selector may
    wildcard whole dimensions, so a large cube is not automatically a large
    fixture burden. What a large cube does mean is that enumeration stops working
    as a review tool. See the note on the constant: this bound is fitted, not
    derived.
    """

    sizes = {
        path.stem: len(load_bucket_vocabulary(path).all_bucket_ids()) for path in VOCABULARY_FILES
    }
    oversized = {name: size for name, size in sizes.items() if size > BUCKET_CUBE_CEILING}
    assert not oversized, f"vocabularies above the provisional ceiling: {oversized}"
    assert max(sizes.values()) > 1000, (
        "the quantitative vocabularies are meant to be detailed; if the largest cube "
        "has become small, a dimension was probably dropped by accident"
    )


def test_the_published_schema_matches_the_model() -> None:
    """The schema file is the format; drifting from the model would be a lie."""

    import json

    published = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert published == BucketVocabulary.model_json_schema()


PACKAGES = REPO_ROOT / "packages"
SHIPPED_COPIES = sorted(PACKAGES.glob("*/src/*/vocab/*.yaml"))


def test_at_least_one_package_ships_a_vocabulary_copy() -> None:
    """Otherwise the drift test below would pass by having nothing to compare."""

    assert SHIPPED_COPIES


@pytest.mark.parametrize("path", SHIPPED_COPIES, ids=lambda p: f"{p.parents[2].name}/{p.stem}")
def test_a_shipped_vocabulary_copy_matches_the_published_one(path: Path) -> None:
    """A plane package ships the vocabulary it classifies against, and it must not fork.

    The copy exists because an installed provider classifies without a repository
    to read: ``vocab/interfaces/`` is repository data and does not travel in a
    wheel. A duplicate nothing compares is a fork waiting to happen, so this is
    the comparison — over the parsed vocabulary rather than the bytes, since
    formatting is not the thing that must agree.
    """

    published = INTERFACES / path.name
    assert published.is_file(), f"{path.name} is shipped by a package but is not published here"
    assert load_bucket_vocabulary(path) == load_bucket_vocabulary(published)


def test_the_stub_vocabulary_lives_apart_from_the_launch_set() -> None:
    stub = yaml.safe_load((VOCAB_DIR / "stub" / "noop.echo.yaml").read_text(encoding="utf-8"))
    assert stub["interface_id"] == "noop.echo"
    assert "noop.echo" not in LAUNCH_INTERFACES
