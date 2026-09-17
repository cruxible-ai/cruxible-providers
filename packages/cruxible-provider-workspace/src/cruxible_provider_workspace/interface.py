"""Package-owned operation contracts and request classifiers.

Exact definitions live in bundled contracts/*.json; their frozen predecessors
remain in contracts/history for historical digest verification.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from cruxible_provider_runtime.buckets import BucketClass, BucketDimension, BucketVocabulary
from cruxible_provider_runtime.canonical import domain_digest
from cruxible_provider_runtime.registration import read_interface_definition
from cruxible_provider_runtime.registry import InterfaceRegistration

__all__ = [
    "BYTE_SIZE_CEILINGS",
    "CONTENT_ENCODING",
    "INTERFACE_DIGEST",
    "INTERFACE_ID",
    "INTERFACE_PREIMAGE",
    "STUB_INTERFACE_DOMAIN_TAG",
    "VOCABULARY",
    "byte_size_class",
    "classify",
    "content_kind_class",
    "decode_declared_bytes",
    "recompute_interface_digest",
    "registration",
]

INTERFACE_ID = "workspace.file"
STUB_INTERFACE_DOMAIN_TAG = "cruxible.interface.stub.v1"

CONTENT_ENCODING = "base64"
"""The only content encoding the interface admits: RFC 4648 section 4, padded, no line breaks."""

INTERFACE_PREIMAGE: dict[str, Any] = read_interface_definition(
    Path(__file__).parent, "workspace.file"
)

INTERFACE_DIGEST = "sha256:faa92552bd6032d3280753881ce991501013b2eaa2005e0f974820f01248d866"

BYTE_SIZE_CEILINGS: tuple[tuple[str, int], ...] = (
    ("tiny", 4_096),
    ("small", 65_536),
    ("medium", 1_048_576),
)
"""Inclusive upper bounds per size class, in order; above the last is ``large``."""

VOCABULARY = BucketVocabulary(
    interface_id=INTERFACE_ID,
    version=1,
    status="draft",
    description=(
        "Structure the bytes of one authorized workspace file read into a capture body. "
        "The two dimensions separate the text path (a UTF-8 decode, a line view) from "
        "the opaque-bytes path, and size the payload so a claim over a large file is "
        "visibly a different bucket from a claim over a small one."
    ),
    dimensions=(
        BucketDimension(
            name="content_kind",
            description="whether the bytes decode as text",
            classes=(
                BucketClass(
                    id="text",
                    description="strict UTF-8 with no NUL byte; an empty file is text",
                ),
                BucketClass(
                    id="binary",
                    description="anything that is not strict UTF-8, or that carries a NUL byte",
                ),
            ),
        ),
        BucketDimension(
            name="byte_size",
            description="length of the decoded bytes",
            classes=(
                BucketClass(id="tiny", description="at most 4096 bytes (4 KiB)"),
                BucketClass(id="small", description="4097 to 65536 bytes (64 KiB)"),
                BucketClass(id="medium", description="65537 to 1048576 bytes (1 MiB)"),
                BucketClass(
                    id="large",
                    description="more than 1048576 bytes (1 MiB); unclaimed by the built-in",
                ),
            ),
        ),
    ),
)


def decode_declared_bytes(payload: Mapping[str, Any]) -> bytes | None:
    """Decode the run input's payload, or ``None`` when it is not a base64 string.

    Shared by the classifier and the adapter so that the two cannot disagree
    about what the bytes are. Strict: the alphabet is RFC 4648 section 4 with
    padding, and a stray character (a line break included) is not a payload.
    """

    if payload.get("content_encoding") != CONTENT_ENCODING:
        return None
    encoded = payload.get("bytes")
    if not isinstance(encoded, str):
        return None
    try:
        return base64.b64decode(encoded.encode("ascii"), validate=True)
    except (UnicodeEncodeError, ValueError):
        return None


def content_kind_class(data: bytes) -> str:
    """``text`` for strict UTF-8 without a NUL byte, ``binary`` otherwise."""

    if b"\x00" in data:
        return "binary"
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return "binary"
    return "text"


def byte_size_class(length: int) -> str:
    for class_id, ceiling in BYTE_SIZE_CEILINGS:
        if length <= ceiling:
            return class_id
    return "large"


def classify(payload: Mapping[str, Any]) -> Mapping[str, str] | None:
    """Derive the bucket from the actual bytes, never from a declaration.

    Returns ``None`` when the payload carries nothing decodable, which the
    registry turns into an ``unclassified_input`` refusal rather than a guess.
    The declared ``byte_length`` and ``bytes_digest`` are deliberately not
    consulted here: they are checked by the adapter, and a bucket measured from
    a declaration would be a bucket the caller chose.
    """

    data = decode_declared_bytes(payload)
    if data is None:
        return None
    return {
        "content_kind": content_kind_class(data),
        "byte_size": byte_size_class(len(data)),
    }


def recompute_interface_digest() -> str:
    """Recompute the stub digest from its preimage (used by a drift test)."""

    return domain_digest(STUB_INTERFACE_DOMAIN_TAG, INTERFACE_PREIMAGE)


def registration() -> InterfaceRegistration:
    """The registration a stub registry is seeded with."""

    return InterfaceRegistration(
        interface_id=INTERFACE_ID,
        interface_digest=INTERFACE_DIGEST,
        bucket_vocabulary=VOCABULARY,
        classifier=classify,
        description="Structure one authorized workspace file read into a capture body.",
    )
