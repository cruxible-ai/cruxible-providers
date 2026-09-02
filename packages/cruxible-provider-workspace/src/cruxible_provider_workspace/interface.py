"""The ``workspace.file`` interface: identity, vocabulary, and classifier.

This is the interface vocabulary the built-in Source adapter speaks. Core
registers the interface (B4 seed bundle); what ships here is the preimage core
transcribes, the bucket vocabulary as the same document the repository publishes
under ``vocab/interfaces/``, and the reference classifier that core's own
classifier is re-proved against over the committed fixtures.

The digest is a **stub**, minted under ``cruxible.interface.stub.v1`` like every
other interface in this repository until core registers the real one. It is a
literal, not a value recomputed at import time: an identity that recomputes
itself is an identity that can drift silently, and a test asserts the literal
still matches the preimage below.

Nothing in this module reads a file or opens a socket. The vocabulary is built in
code and asserted equal to the shipped YAML by a test, so that importing the
adapter touches nothing but the interpreter.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from typing import Any

from cruxible_provider_runtime.buckets import BucketClass, BucketDimension, BucketVocabulary
from cruxible_provider_runtime.canonical import domain_digest
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

_DIGEST_SCHEMA = {"type": "string", "required": True, "pattern": "^sha256:[0-9a-f]{64}$"}

INTERFACE_PREIMAGE: dict[str, Any] = {
    "interface_id": INTERFACE_ID,
    "version": 1,
    # RAT-9: the built-in is PURE. It reads nothing, contacts nothing, and its
    # output is a function of its input alone. The manifest spells the same fact
    # as declared_endpoints=[], deterministic=true, side_effects=false.
    "effect_class": "pure",
    "input": {
        # The logical source id core resolved the read for. Opaque here: the
        # locator ban keeps host paths out of governed state, and this adapter
        # is on the governed side of that line.
        "logical_source": {"type": "string", "required": True},
        # G4: the derived-request commitment the read was bound to before spawn.
        "commitment_digest": _DIGEST_SCHEMA,
        "content_encoding": {"type": "string", "required": True, "enum": [CONTENT_ENCODING]},
        "bytes": {"type": "string", "required": True},
        "byte_length": {"type": "integer", "required": True, "minimum": 0},
        "bytes_digest": _DIGEST_SCHEMA,
    },
    "output": {
        "input_bucket": {"type": "string"},
        # What was structured, with the source digest echoed back after it was
        # verified against the decoded bytes.
        "source": {
            "type": "object",
            "properties": {
                "logical_source": {"type": "string"},
                "commitment_digest": {"type": "string"},
                "bytes_digest": {"type": "string"},
                "byte_length": {"type": "integer"},
            },
        },
        # The structured capture body: one of two shapes, selected by
        # content_kind. Text carries the decoded text and a line view; bytes
        # carries the canonical base64 of the payload and nothing else.
        "content": {
            "type": "object",
            "one_of": [
                {
                    "kind": "text",
                    "encoding": "utf-8",
                    "bom": {"type": "boolean"},
                    "newline": {"type": "string", "enum": ["lf", "crlf", "cr", "mixed", "none"]},
                    "trailing_newline": {"type": "boolean"},
                    "line_count": {"type": "integer"},
                    "character_count": {"type": "integer"},
                    "text": {"type": "string"},
                    "lines": {"type": "array", "items": {"type": "string"}},
                },
                {
                    "kind": "bytes",
                    "encoding": "base64",
                    "byte_length": {"type": "integer"},
                    "bytes": {"type": "string"},
                },
            ],
        },
    },
    "refusals": [
        "invalid_parameter",
        "mismatched_lengths",
        "provider_declined",
    ],
}

INTERFACE_DIGEST = "sha256:372bc808d6bd77627bdda7bc67586300e2eb812bf0a4fb3769283a26cc021f88"

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
