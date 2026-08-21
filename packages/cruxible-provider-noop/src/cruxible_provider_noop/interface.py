"""The stub ``noop.echo`` interface.

This is a **stub**. Real slot interfaces are registered in core with a digest
over their input/output/refusal schema. ``noop.echo`` exists so that the RP-0
conformance suite has something to bind against before core's registry exists,
and so that the launch vocabularies under ``vocab/`` have a worked example of
the format.

The interface digest is a literal, not a value recomputed at import time: an
identity that recomputes itself is an identity that can drift silently. A test
asserts the literal still matches the preimage below.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from cruxible_provider_runtime.buckets import BucketClass, BucketDimension, BucketVocabulary
from cruxible_provider_runtime.canonical import domain_digest
from cruxible_provider_runtime.registry import InterfaceRegistration

__all__ = [
    "INTERFACE_DIGEST",
    "INTERFACE_ID",
    "INTERFACE_PREIMAGE",
    "STUB_INTERFACE_DOMAIN_TAG",
    "VOCABULARY",
    "classify",
    "registration",
]

INTERFACE_ID = "noop.echo"
STUB_INTERFACE_DOMAIN_TAG = "cruxible.interface.stub.v1"

INTERFACE_PREIMAGE: dict[str, Any] = {
    "interface_id": INTERFACE_ID,
    "version": 1,
    "input": {
        "text": {"type": "string", "required": True},
        "mode": {"type": "string", "required": False, "default": "echo"},
    },
    "output": {
        "echo": {"type": "string"},
        "input_bucket": {"type": "string"},
    },
    "refusals": ["provider_declined", "unresolved_secret_ref"],
}

INTERFACE_DIGEST = "sha256:e72546b97fdcb8875c4fa3d8828909db60809d98d55e7a2450d0c6043113cb87"

VOCABULARY = BucketVocabulary(
    interface_id=INTERFACE_ID,
    version=1,
    status="draft",
    description="Stub vocabulary for the reference no-op interface.",
    dimensions=(
        BucketDimension(
            name="payload_size",
            description="length of the input text in characters",
            classes=(
                BucketClass(id="tiny", description="at most 16 characters"),
                BucketClass(id="small", description="17 to 1024 characters"),
                BucketClass(id="large", description="more than 1024 characters"),
            ),
        ),
        BucketDimension(
            name="charset",
            description="whether the input stays inside ASCII",
            classes=(
                BucketClass(id="ascii", description="every character is ASCII"),
                BucketClass(id="unicode", description="at least one non-ASCII character"),
            ),
        ),
    ),
)


def classify(payload: Mapping[str, Any]) -> Mapping[str, str] | None:
    """Derive the bucket from the actual input.

    Returns ``None`` when the input carries nothing classifiable, which the
    registry turns into an ``unclassified_input`` refusal rather than guessing.
    """

    text = payload.get("text")
    if not isinstance(text, str):
        return None
    if len(text) <= 16:
        payload_size = "tiny"
    elif len(text) <= 1024:
        payload_size = "small"
    else:
        payload_size = "large"
    return {
        "payload_size": payload_size,
        "charset": "ascii" if text.isascii() else "unicode",
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
        description="Reference no-op interface: echoes its input back.",
    )
