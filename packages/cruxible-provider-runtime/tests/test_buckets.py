"""Bucket vocabulary format, ids, and selectors."""

from __future__ import annotations

import pytest
from cruxible_provider_runtime.buckets import (
    BucketClass,
    BucketDimension,
    BucketSelector,
    BucketVocabulary,
    parse_bucket_id,
)
from cruxible_provider_runtime.errors import RefusalCode, RefusalError

VOCABULARY = BucketVocabulary(
    interface_id="test.slot",
    dimensions=(
        BucketDimension(
            name="size",
            description="input size",
            classes=(
                BucketClass(id="small", description="small"),
                BucketClass(id="large", description="large"),
            ),
        ),
        BucketDimension(
            name="shape",
            description="input shape",
            classes=(
                BucketClass(id="flat", description="flat"),
                BucketClass(id="nested", description="nested"),
            ),
        ),
    ),
)


def test_bucket_id_follows_declared_dimension_order() -> None:
    assert VOCABULARY.bucket_id({"shape": "flat", "size": "large"}) == "size=large;shape=flat"


def test_all_buckets_enumerates_the_cube() -> None:
    assert VOCABULARY.all_bucket_ids() == (
        "size=small;shape=flat",
        "size=small;shape=nested",
        "size=large;shape=flat",
        "size=large;shape=nested",
    )


def test_missing_dimension_refuses() -> None:
    with pytest.raises(RefusalError) as exc:
        VOCABULARY.bucket_id({"size": "small"})
    assert exc.value.code is RefusalCode.INVALID_BUCKET_VOCABULARY


def test_unregistered_class_refuses() -> None:
    with pytest.raises(RefusalError) as exc:
        VOCABULARY.bucket_id({"size": "enormous", "shape": "flat"})
    assert exc.value.code is RefusalCode.INVALID_BUCKET_VOCABULARY


def test_unregistered_dimension_refuses() -> None:
    with pytest.raises(RefusalError) as exc:
        VOCABULARY.bucket_id({"size": "small", "shape": "flat", "colour": "red"})
    assert exc.value.code is RefusalCode.INVALID_BUCKET_VOCABULARY


def test_selector_wildcard_matches_a_whole_face() -> None:
    selector = BucketSelector.parse("size=small;shape=*", VOCABULARY)
    assert selector.matches("size=small;shape=flat")
    assert selector.matches("size=small;shape=nested")
    assert not selector.matches("size=large;shape=flat")


def test_selector_round_trips() -> None:
    assert BucketSelector.parse("size=*;shape=nested", VOCABULARY).render() == "size=*;shape=nested"


def test_selector_naming_an_unknown_class_refuses() -> None:
    with pytest.raises(RefusalError) as exc:
        BucketSelector.parse("size=enormous;shape=*", VOCABULARY)
    assert exc.value.code is RefusalCode.INVALID_BUCKET_VOCABULARY


def test_selector_missing_a_dimension_refuses() -> None:
    with pytest.raises(RefusalError) as exc:
        BucketSelector.parse("size=small", VOCABULARY)
    assert exc.value.code is RefusalCode.INVALID_BUCKET_VOCABULARY


def test_malformed_expression_refuses() -> None:
    with pytest.raises(RefusalError) as exc:
        BucketSelector.parse("size", VOCABULARY)
    assert exc.value.code is RefusalCode.INVALID_BUCKET_VOCABULARY


def test_parse_bucket_id_validates() -> None:
    assert parse_bucket_id(VOCABULARY, "size=small;shape=flat") == {
        "size": "small",
        "shape": "flat",
    }
    with pytest.raises(RefusalError):
        parse_bucket_id(VOCABULARY, "size=*;shape=flat")


def test_class_ids_cannot_collide_with_the_separators() -> None:
    with pytest.raises(ValueError, match="invalid bucket class id"):
        BucketClass(id="a;b", description="bad")
    with pytest.raises(ValueError, match="invalid bucket class id"):
        BucketClass(id="*", description="bad")


def test_duplicate_dimension_names_refuse() -> None:
    dimension = BucketDimension(
        name="size", description="x", classes=(BucketClass(id="a", description="a"),)
    )
    with pytest.raises(ValueError, match="duplicate bucket dimension"):
        BucketVocabulary(interface_id="test.slot", dimensions=(dimension, dimension))
