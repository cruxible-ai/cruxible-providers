"""Package-owned operation contracts and request classifiers.

Exact definitions live in bundled contracts/*.json; their frozen predecessors
remain in contracts/history for historical digest verification.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from cruxible_provider_runtime.buckets import BucketVocabulary
from cruxible_provider_runtime.canonical import domain_digest
from cruxible_provider_runtime.registration import read_interface_definition
from cruxible_provider_runtime.registry import InterfaceRegistration, load_bucket_vocabulary

__all__ = [
    "FETCH_INTERFACE_DIGEST",
    "FETCH_INTERFACE_ID",
    "FETCH_VOCABULARY",
    "SEARCH_INTERFACE_DIGEST",
    "SEARCH_INTERFACE_ID",
    "SEARCH_VOCABULARY",
    "STUB_INTERFACE_DOMAIN_TAG",
    "VOCAB_DIR",
    "classify_search",
    "classify_web_fetch",
    "fetch_registration",
    "registrations",
    "search_registration",
]

STUB_INTERFACE_DOMAIN_TAG = "cruxible.interface.stub.v1"
VOCAB_DIR = Path(__file__).resolve().parent / "vocab"

FETCH_INTERFACE_ID = "web.fetch"
SEARCH_INTERFACE_ID = "search.web"

FETCH_PREIMAGE: dict[str, Any] = read_interface_definition(Path(__file__).parent, "web.fetch")

SEARCH_PREIMAGE: dict[str, Any] = read_interface_definition(Path(__file__).parent, "search.web")

FETCH_INTERFACE_DIGEST = "sha256:9769f47abc5ac2dae6d6c623a9f9abf01afde699de48768a755f40a0334a1ade"
SEARCH_INTERFACE_DIGEST = "sha256:fea1d32087a65259d93586a9d582a5112d08f051c59d97e8737d10ad5bc295bb"

FETCH_VOCABULARY: BucketVocabulary = load_bucket_vocabulary(VOCAB_DIR / "web.fetch.yaml")
SEARCH_VOCABULARY: BucketVocabulary = load_bucket_vocabulary(VOCAB_DIR / "search.web.yaml")

DEFAULT_MAX_BYTES = 262_144
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
LIGHT_CEILING_BYTES = 262_144
MEDIUM_CEILING_BYTES = 2 * 1024 * 1024

_BINARY_SUFFIXES = {
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".zip",
    ".gz",
    ".tar",
    ".docx",
    ".xlsx",
    ".pptx",
    ".mp4",
    ".mp3",
}
_STRUCTURED_SUFFIXES = {".json", ".xml", ".atom", ".rss", ".csv"}
_BOOLEAN_MARKERS = (" AND ", " OR ", " NOT ", '"', "site:", "filetype:", "intitle:", " -")
_QUESTION_OPENERS = (
    "who",
    "what",
    "when",
    "where",
    "why",
    "how",
    "which",
    "is",
    "are",
    "does",
    "do",
    "can",
    "should",
)


def _path_of(url: str) -> str:
    from urllib.parse import urlsplit

    return urlsplit(url).path.lower()


def page_weight_class(byte_count: int) -> str:
    """The weight class a byte count falls in. Used for input and for response."""

    if byte_count <= LIGHT_CEILING_BYTES:
        return "light"
    if byte_count <= MEDIUM_CEILING_BYTES:
        return "medium"
    return "heavy"


def classify_web_fetch(payload: Mapping[str, Any]) -> Mapping[str, str] | None:
    """Derive a ``web.fetch`` bucket from the run input.

    ``source_kind`` follows the caller's explicit ``render`` flag first — asking
    for a browser is a statement about the resource — then the URL's own shape.
    ``access`` follows what the run carries: a credential ref means an
    authenticated fetch, a declared pace means a throttled origin.
    ``page_weight`` is the declared ``max_bytes`` cap, because weight is not
    knowable before retrieval; the adapter refuses if the response comes back
    heavier than the bucket the run was admitted into, so the declaration is
    checked rather than trusted.
    """

    url = payload.get("url")
    if not isinstance(url, str) or not url.strip():
        return None
    path = _path_of(url)
    suffix = path[path.rfind(".") :] if "." in path.rsplit("/", 1)[-1] else ""

    expected_format = payload.get("expected_format", "auto")
    if expected_format not in {"auto", "html", "json", "csv", "text", "bytes"}:
        return None
    if bool(payload.get("render", False)):
        source_kind = "js_rendered"
    elif expected_format == "bytes" or suffix in _BINARY_SUFFIXES:
        source_kind = "binary"
    elif expected_format in {"json", "csv"} or suffix in _STRUCTURED_SUFFIXES or "/api/" in path:
        source_kind = "api_json"
    else:
        source_kind = "static_html"

    if payload.get("credential_ref"):
        access = "authenticated"
    elif bool(payload.get("paced", False)):
        access = "rate_limited"
    else:
        access = "public"

    max_bytes = payload.get("max_bytes", DEFAULT_MAX_BYTES)
    if (
        not isinstance(max_bytes, int)
        or isinstance(max_bytes, bool)
        or not 0 < max_bytes <= MAX_RESPONSE_BYTES
    ):
        return None
    return {
        "source_kind": source_kind,
        "access": access,
        "page_weight": page_weight_class(max_bytes),
    }


def classify_search(payload: Mapping[str, Any]) -> Mapping[str, str] | None:
    """Derive a ``search.web`` bucket from the run input.

    Every dimension is measured from something the caller actually submitted:
    the query text decides its own form, the freshness bound is a number of
    hours rather than a label, and the depth is the result limit. Nothing here
    asks the caller to name a bucket.
    """

    query = payload.get("query")
    if not isinstance(query, str) or not query.strip():
        return None

    padded = f" {query.strip()} "
    words = query.split()
    if any(marker in padded for marker in _BOOLEAN_MARKERS):
        query_form = "boolean"
    elif query.strip().endswith("?") or (len(words) >= 5 and words[0].lower() in _QUESTION_OPENERS):
        query_form = "natural_language"
    else:
        query_form = "keyword"

    max_age_hours = payload.get("max_age_hours")
    if max_age_hours is None:
        recency = "any_time"
    elif (
        not isinstance(max_age_hours, int) or isinstance(max_age_hours, bool) or max_age_hours <= 0
    ):
        return None
    elif max_age_hours <= 24:
        recency = "realtime"
    elif max_age_hours <= 24 * 90:
        recency = "recent"
    else:
        recency = "any_time"

    limit = payload.get("limit", 10)
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        return None
    if limit <= 10:
        result_depth = "shallow"
    elif limit <= 50:
        result_depth = "standard"
    else:
        result_depth = "deep"

    return {"query_form": query_form, "recency": recency, "result_depth": result_depth}


def recompute_interface_digest(preimage: Mapping[str, Any]) -> str:
    """Recompute a stub digest from its preimage (used by the drift tests)."""

    return domain_digest(STUB_INTERFACE_DOMAIN_TAG, dict(preimage))


def fetch_registration() -> InterfaceRegistration:
    return InterfaceRegistration(
        interface_id=FETCH_INTERFACE_ID,
        interface_digest=FETCH_INTERFACE_DIGEST,
        bucket_vocabulary=FETCH_VOCABULARY,
        classifier=classify_web_fetch,
        description="Retrieve one web resource and, optionally, extract its main content.",
    )


def search_registration() -> InterfaceRegistration:
    return InterfaceRegistration(
        interface_id=SEARCH_INTERFACE_ID,
        interface_digest=SEARCH_INTERFACE_DIGEST,
        bucket_vocabulary=SEARCH_VOCABULARY,
        classifier=classify_search,
        description="Return ranked web results for a query from a configured instance.",
    )


def registrations() -> tuple[InterfaceRegistration, InterfaceRegistration]:
    """Everything a stub registry needs seeding with to bind this package."""

    return (fetch_registration(), search_registration())
