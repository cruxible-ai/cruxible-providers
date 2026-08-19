"""Canonical JSON encoding and domain-tagged digests.

Every digest this runtime computes is ``sha256(domain_tag || 0x00 || canonical_json(preimage))``
rendered as ``sha256:<64 hex chars>``. The domain tag is a prefix over the bytes,
never a field inside the preimage, so a preimage can never be re-read as
belonging to a different domain.

Canonical JSON here means: UTF-8, object keys sorted, no insignificant
whitespace, no NaN/Infinity, and no non-JSON scalar types. Encoding refuses
rather than coercing, because a coercion would silently change an identity.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

__all__ = [
    "SHA256_RE",
    "canonical_json",
    "domain_digest",
    "normalize_sha256",
    "sha256_hex",
]

SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_BARE_HEX_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def canonical_json(value: Any) -> bytes:
    """Encode ``value`` as canonical JSON bytes."""

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def domain_digest(domain_tag: str, preimage: Any) -> str:
    """Digest ``preimage`` under ``domain_tag``."""

    if not domain_tag:
        raise ValueError("domain_tag must be non-empty")
    hasher = hashlib.sha256()
    hasher.update(domain_tag.encode("utf-8"))
    hasher.update(b"\x00")
    hasher.update(canonical_json(preimage))
    return f"sha256:{hasher.hexdigest()}"


def sha256_hex(data: bytes) -> str:
    """``sha256:``-prefixed digest of raw bytes."""

    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def normalize_sha256(value: str) -> str:
    """Accept ``sha256:<hex>`` or a bare lowercase/uppercase hex digest.

    Lock files and container registries spell the same digest several ways.
    Normalising at the edge keeps exactly one spelling inside every preimage.
    """

    candidate = value.strip()
    if _BARE_HEX_RE.match(candidate):
        return f"sha256:{candidate.lower()}"
    lowered = candidate.lower()
    if SHA256_RE.match(lowered):
        return lowered
    raise ValueError(f"not a sha256 digest: {value!r}")
