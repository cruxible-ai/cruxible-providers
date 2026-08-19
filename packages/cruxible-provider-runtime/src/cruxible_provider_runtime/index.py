"""Fetch-on-bind artifact retrieval from pinned indexes.

Rules the contract fixes:

* index URLs come from configuration and nothing else — an artifact URL whose
  origin is not under a pinned index refuses (``index_not_pinned``);
* a redirect refuses (``index_redirect``) rather than being followed, because a
  followed redirect makes the pin meaningless;
* an artifact whose bytes do not hash to the pinned sha256 refuses
  (``artifact_hash_mismatch``);
* air-gapped mode is cache-only: no fetch is attempted at all
  (``air_gapped_cache_miss``).

Transport is injected. The runtime ships no HTTP client of its own: the
production transport is supplied by the embedding executor, and the test suite
uses a filesystem-backed fake index so that no test needs the network.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, field_validator

from .canonical import sha256_hex
from .errors import RefusalCode, refuse
from .resolution import ResolvedDistribution

__all__ = ["ArtifactFetcher", "IndexConfig", "Transport", "TransportResponse"]


class IndexConfig(BaseModel):
    """Pinned index URLs plus the two network postures."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    index_urls: tuple[str, ...]
    air_gapped: bool = False

    @field_validator("index_urls")
    @classmethod
    def _absolute(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("at least one index URL must be pinned")
        for url in value:
            parts = urlsplit(url)
            if parts.scheme not in {"https", "file"} or not (parts.netloc or parts.path):
                raise ValueError(f"index URL must be https or file, got {url!r}")
        return value

    def covers(self, url: str) -> bool:
        """Whether ``url`` lives under one of the pinned index prefixes."""

        for index_url in self.index_urls:
            prefix = index_url if index_url.endswith("/") else index_url + "/"
            if url.startswith(prefix):
                return True
        return False


@dataclass(frozen=True)
class TransportResponse:
    """What a transport reports back.

    ``final_url`` lets the fetcher detect a redirect without having to trust the
    transport not to follow one.
    """

    status: int
    final_url: str
    body: bytes


@runtime_checkable
class Transport(Protocol):
    """Fetches bytes for a URL without following redirects."""

    def get(self, url: str) -> TransportResponse: ...


class ArtifactFetcher:
    """Fetches pinned distribution artifacts, refusing on every mismatch."""

    def __init__(self, config: IndexConfig, transport: Transport | None = None) -> None:
        self._config = config
        self._transport = transport

    @property
    def config(self) -> IndexConfig:
        return self._config

    def fetch(self, distribution: ResolvedDistribution) -> bytes:
        return self.fetch_url(distribution.url, distribution.sha256, distribution.name)

    def fetch_url(self, url: str, expected_sha256: str, label: str) -> bytes:
        if self._config.air_gapped:
            raise refuse(
                RefusalCode.AIR_GAPPED_CACHE_MISS,
                f"air-gapped mode is cache-only; refusing to fetch {label!r}",
                url=url,
                artifact=label,
            )
        if not self._config.covers(url):
            raise refuse(
                RefusalCode.INDEX_NOT_PINNED,
                f"artifact URL for {label!r} is not under a pinned index",
                url=url,
                pinned=list(self._config.index_urls),
            )
        if self._transport is None:
            raise refuse(
                RefusalCode.NETWORK_DISABLED,
                "no transport is configured; the executor must supply one to fetch",
                url=url,
            )
        response = self._transport.get(url)
        if response.status in {301, 302, 303, 307, 308} or response.final_url != url:
            raise refuse(
                RefusalCode.INDEX_REDIRECT,
                f"index redirected the artifact for {label!r}; a followed redirect "
                "would void the pin",
                url=url,
                final_url=response.final_url,
                status=response.status,
            )
        if response.status != 200:
            raise refuse(
                RefusalCode.INDEX_NOT_PINNED,
                f"index returned status {response.status} for {label!r}",
                url=url,
                status=response.status,
            )
        actual = sha256_hex(response.body)
        if actual != expected_sha256:
            raise refuse(
                RefusalCode.ARTIFACT_HASH_MISMATCH,
                f"artifact for {label!r} does not match its pinned hash",
                url=url,
                expected=expected_sha256,
                actual=actual,
            )
        return response.body
