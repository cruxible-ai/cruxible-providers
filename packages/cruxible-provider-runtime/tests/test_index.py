"""Fetch-on-bind: pinned indexes, redirects, hashes, air-gapped mode.

No test here touches the network. The index is a fake whose whole job is to
misbehave in the four ways the contract names.
"""

from __future__ import annotations

import pytest

from cruxible_provider_runtime.canonical import sha256_hex
from cruxible_provider_runtime.errors import RefusalCode, RefusalError
from cruxible_provider_runtime.index import ArtifactFetcher, IndexConfig
from cruxible_provider_runtime.resolution import ResolvedDistribution
from cruxible_provider_runtime.testing import FakeIndexTransport

INDEX = "https://index.example/simple"
URL = f"{INDEX}/leaf/leaf-1.0.0-py3-none-any.whl"
BODY = b"wheel bytes"


def _distribution(url: str = URL, sha256: str | None = None) -> ResolvedDistribution:
    return ResolvedDistribution(
        name="leaf",
        version="1.0.0",
        sha256=sha256 or sha256_hex(BODY),
        kind="wheel",
        filename="leaf-1.0.0-py3-none-any.whl",
        url=url,
    )


def test_matching_artifact_is_returned() -> None:
    fetcher = ArtifactFetcher(
        IndexConfig(index_urls=(INDEX,)), FakeIndexTransport(files={URL: BODY})
    )
    assert fetcher.fetch(_distribution()) == BODY


def test_url_outside_the_pinned_index_refuses() -> None:
    fetcher = ArtifactFetcher(
        IndexConfig(index_urls=(INDEX,)),
        FakeIndexTransport(files={"https://elsewhere.example/leaf.whl": BODY}),
    )
    with pytest.raises(RefusalError) as exc:
        fetcher.fetch(_distribution(url="https://elsewhere.example/leaf.whl"))
    assert exc.value.code is RefusalCode.INDEX_NOT_PINNED


def test_prefix_confusion_does_not_count_as_pinned() -> None:
    """``https://index.example.evil/`` must not match ``https://index.example``."""

    fetcher = ArtifactFetcher(IndexConfig(index_urls=("https://index.example",)))
    assert not fetcher.config.covers("https://index.example.evil/leaf.whl")


def test_redirect_refuses_rather_than_being_followed() -> None:
    transport = FakeIndexTransport(
        files={URL: BODY}, redirects={URL: "https://elsewhere.example/leaf.whl"}
    )
    fetcher = ArtifactFetcher(IndexConfig(index_urls=(INDEX,)), transport)
    with pytest.raises(RefusalError) as exc:
        fetcher.fetch(_distribution())
    assert exc.value.code is RefusalCode.INDEX_REDIRECT


def test_hash_mismatch_refuses() -> None:
    transport = FakeIndexTransport(files={URL: b"different bytes"})
    fetcher = ArtifactFetcher(IndexConfig(index_urls=(INDEX,)), transport)
    with pytest.raises(RefusalError) as exc:
        fetcher.fetch(_distribution())
    assert exc.value.code is RefusalCode.ARTIFACT_HASH_MISMATCH


def test_air_gapped_never_fetches() -> None:
    transport = FakeIndexTransport(files={URL: BODY})
    fetcher = ArtifactFetcher(IndexConfig(index_urls=(INDEX,), air_gapped=True), transport)
    with pytest.raises(RefusalError) as exc:
        fetcher.fetch(_distribution())
    assert exc.value.code is RefusalCode.AIR_GAPPED_CACHE_MISS
    assert transport.requested == [], "air-gapped mode must not touch the transport at all"


def test_missing_transport_refuses_rather_than_improvising() -> None:
    fetcher = ArtifactFetcher(IndexConfig(index_urls=(INDEX,)))
    with pytest.raises(RefusalError) as exc:
        fetcher.fetch(_distribution())
    assert exc.value.code is RefusalCode.NETWORK_DISABLED


def test_non_200_refuses() -> None:
    transport = FakeIndexTransport(files={URL: BODY}, statuses={URL: 404})
    fetcher = ArtifactFetcher(IndexConfig(index_urls=(INDEX,)), transport)
    with pytest.raises(RefusalError) as exc:
        fetcher.fetch(_distribution())
    assert exc.value.code is RefusalCode.INDEX_NOT_PINNED


def test_index_config_requires_at_least_one_index() -> None:
    with pytest.raises(ValueError, match="at least one index"):
        IndexConfig(index_urls=())


def test_index_config_rejects_plain_http() -> None:
    with pytest.raises(ValueError, match="https or file"):
        IndexConfig(index_urls=("http://index.example/simple",))
