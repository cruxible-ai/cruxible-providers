"""Typed refusal taxonomy.

Everything in this runtime fails closed: an unexpected condition raises a
:class:`RefusalError` carrying a machine-readable :class:`RefusalCode`, never a
bare exception and never a silent fallback.

Refusals are distinct from provider *errors*. A refusal means the runtime (or
the provider, deliberately) declined to produce an answer under a named rule; an
error means an attempted answer failed. Budget breaches are refusals, not
errors, per the RP-0 contract.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["ProviderErrorPayload", "Refusal", "RefusalCode", "RefusalError"]


class RefusalCode(StrEnum):
    """The closed set of refusal codes RP-0 defines."""

    # --- registration / manifest -------------------------------------------
    UNKNOWN_MANIFEST_FIELD = "unknown_manifest_field"
    MANIFEST_DIVERGENCE = "manifest_divergence"
    UNACCEPTED_PROVIDER = "unaccepted_provider"
    UNDECLARED_INTERFACE = "undeclared_interface"
    AMBIGUOUS_IMPLEMENTATION = "ambiguous_implementation"
    UNKNOWN_INTERFACE = "unknown_interface"
    INTERFACE_DIGEST_MISMATCH = "interface_digest_mismatch"
    BUCKET_FIXTURE_MISSING = "bucket_fixture_missing"
    INVALID_BUCKET_VOCABULARY = "invalid_bucket_vocabulary"

    # --- protocol ----------------------------------------------------------
    UNSUPPORTED_PROTOCOL = "unsupported_protocol"
    UNKNOWN_RUN_CONTEXT_FIELD = "unknown_run_context_field"
    PROVIDER_PROTOCOL_VIOLATION = "provider_protocol_violation"
    UNSUPPORTED_BACKEND = "unsupported_backend"

    # --- resolution / distribution -----------------------------------------
    LOCK_MISMATCH = "lock_mismatch"
    LOCK_BYTES_MISMATCH = "lock_bytes_mismatch"
    LOCK_MISSING_HASH = "lock_missing_hash"
    LOCK_AMBIGUOUS_FORK = "lock_ambiguous_fork"
    NO_COMPATIBLE_ARTIFACT = "no_compatible_artifact"
    UNRESOLVABLE_SOURCE = "unresolvable_source"
    INDEX_NOT_PINNED = "index_not_pinned"
    INDEX_REDIRECT = "index_redirect"
    ARTIFACT_HASH_MISMATCH = "artifact_hash_mismatch"
    AIR_GAPPED_CACHE_MISS = "air_gapped_cache_miss"
    NETWORK_DISABLED = "network_disabled"

    # --- cache -------------------------------------------------------------
    CACHE_PERMISSIONS = "cache_permissions"
    CACHE_INTEGRITY = "cache_integrity"
    ENVIRONMENT_DIVERGENCE = "environment_divergence"

    # --- admission ---------------------------------------------------------
    UNCLAIMED_BUCKET = "unclaimed_bucket"
    UNCLASSIFIED_INPUT = "unclassified_input"

    # --- execution ---------------------------------------------------------
    BUDGET_WALL_CLOCK = "budget_wall_clock"
    BUDGET_OUTPUT_SIZE = "budget_output_size"
    BUDGET_COST = "budget_cost"
    UNDECLARED_EGRESS = "undeclared_egress"
    SECRET_LEAK = "secret_leak"
    PROVIDER_DECLINED = "provider_declined"
    UNRESOLVED_SECRET_REF = "unresolved_secret_ref"

    # --- container ---------------------------------------------------------
    IMAGE_PROVENANCE_MISMATCH = "image_provenance_mismatch"


class Refusal(BaseModel):
    """A typed refusal, serialisable into receipts and result envelopes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: RefusalCode
    message: str
    detail: dict[str, Any] = Field(default_factory=dict)

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.code.value}: {self.message}"


class ProviderErrorPayload(BaseModel):
    """A provider-reported failure (an attempted answer that did not succeed)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str
    message: str
    detail: dict[str, Any] = Field(default_factory=dict)


class RefusalError(Exception):
    """Raised whenever the runtime declines to proceed."""

    def __init__(
        self,
        code: RefusalCode,
        message: str,
        **detail: Any,
    ) -> None:
        self.refusal = Refusal(code=code, message=message, detail=detail)
        super().__init__(str(self.refusal))

    @property
    def code(self) -> RefusalCode:
        return self.refusal.code


def refuse(code: RefusalCode, message: str, **detail: Any) -> RefusalError:
    """Build a :class:`RefusalError` (call sites ``raise refuse(...)``)."""

    return RefusalError(code, message, **detail)
