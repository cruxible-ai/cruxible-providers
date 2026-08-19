"""Canonical JSON and domain-tagged digests."""

from __future__ import annotations

import pytest

from cruxible_provider_runtime.canonical import (
    canonical_json,
    domain_digest,
    normalize_sha256,
    sha256_hex,
)


def test_key_order_does_not_change_the_encoding() -> None:
    assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})


def test_encoding_has_no_insignificant_whitespace() -> None:
    assert canonical_json({"a": [1, 2]}) == b'{"a":[1,2]}'


def test_non_finite_numbers_refuse_rather_than_serialise() -> None:
    with pytest.raises(ValueError, match="Out of range"):
        canonical_json({"a": float("nan")})


def test_domain_tag_separates_identical_preimages() -> None:
    preimage = {"a": 1}
    assert domain_digest("tag.one", preimage) != domain_digest("tag.two", preimage)


def test_domain_tag_is_a_prefix_not_a_field() -> None:
    """A preimage cannot be re-read as belonging to another domain."""

    assert domain_digest("tag.one", {"a": 1}) != domain_digest(
        "tag.two", {"a": 1, "domain": "tag.one"}
    )


def test_empty_domain_tag_is_rejected() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        domain_digest("", {"a": 1})


def test_sha256_hex_is_prefixed() -> None:
    assert sha256_hex(b"").startswith("sha256:")


def test_normalisation_accepts_both_spellings() -> None:
    bare = "a" * 64
    assert normalize_sha256(bare) == f"sha256:{bare}"
    assert normalize_sha256(f"sha256:{bare.upper()}") == f"sha256:{bare}"
    assert normalize_sha256(bare.upper()) == f"sha256:{bare}"


def test_normalisation_rejects_anything_else() -> None:
    with pytest.raises(ValueError, match="not a sha256"):
        normalize_sha256("md5:abc")
