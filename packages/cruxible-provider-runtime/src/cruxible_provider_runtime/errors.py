"""Typed refusal taxonomy.

Everything in this runtime fails closed: an unexpected condition raises a
:class:`RefusalError` carrying a machine-readable :class:`RefusalCode`, never a
bare exception and never a silent fallback.

Refusals are distinct from provider *errors*. A refusal means the runtime (or
the provider, deliberately) declined to produce an answer under a named rule; an
error means an attempted answer failed. Budget breaches are refusals, not
errors, per the RP-0 contract.

There are two refusal vocabularies and they never overlap. The runtime's own is
:class:`RefusalCode`, closed, raised as :class:`RefusalError`. An operation's is
whatever its registration declares, raised by provider code as
:class:`Declined` (or returned as ``ProviderResult.refused``).
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "REFUSAL_CODE_RE",
    "RUNTIME_REFUSAL_CODES",
    "Declined",
    "ProviderErrorPayload",
    "Refusal",
    "RefusalCode",
    "RefusalError",
    "check_operation_refusal_code",
    "decline",
    "refuse",
]


class RefusalCode(StrEnum):
    """The runtime's own closed refusal codes.

    These are the refusals of the runtime *machinery*: the manifest and
    registration readers, the protocol, resolution and materialization, the
    cache, budgets, egress and secret delivery. A provider's own judgement about
    its input is not here. Each operation declares the refusals it may return
    (``registration.json`` ``operations[].refusals``), and those codes are the
    operation's vocabulary, not this one's.

    The two vocabularies are kept disjoint by construction: an operation may not
    declare a code spelled like one of these (registration refuses it), and a
    provider's operation refusal using one is a protocol violation. So a code
    on the wire says by its spelling whose refusal it is.
    """

    # --- registration / manifest -------------------------------------------
    UNKNOWN_MANIFEST_FIELD = "unknown_manifest_field"
    MANIFEST_DIVERGENCE = "manifest_divergence"
    ACCEPTANCE_DIVERGENCE = "acceptance_divergence"
    UNACCEPTED_PROVIDER = "unaccepted_provider"
    UNDECLARED_OPERATION = "undeclared_operation"

    # --- protocol ----------------------------------------------------------
    UNSUPPORTED_PROTOCOL = "unsupported_protocol"
    UNKNOWN_RUN_CONTEXT_FIELD = "unknown_run_context_field"
    PROVIDER_PROTOCOL_VIOLATION = "provider_protocol_violation"
    UNDECLARED_REFUSAL = "undeclared_refusal"
    UNSUPPORTED_BACKEND = "unsupported_backend"

    # --- resolution / distribution -----------------------------------------
    LOCK_MISMATCH = "lock_mismatch"
    LOCK_BYTES_MISMATCH = "lock_bytes_mismatch"
    LOCK_MISSING_HASH = "lock_missing_hash"
    LOCK_AMBIGUOUS_FORK = "lock_ambiguous_fork"
    NO_COMPATIBLE_ARTIFACT = "no_compatible_artifact"
    UNRESOLVABLE_SOURCE = "unresolvable_source"
    UNKNOWN_EXTRA = "unknown_extra"
    INDEX_NOT_PINNED = "index_not_pinned"
    INDEX_REDIRECT = "index_redirect"
    ARTIFACT_HASH_MISMATCH = "artifact_hash_mismatch"
    AIR_GAPPED_CACHE_MISS = "air_gapped_cache_miss"
    NETWORK_DISABLED = "network_disabled"

    # --- cache -------------------------------------------------------------
    CACHE_PERMISSIONS = "cache_permissions"
    CACHE_INTEGRITY = "cache_integrity"
    ENVIRONMENT_DIVERGENCE = "environment_divergence"

    # --- execution ---------------------------------------------------------
    BUDGET_WALL_CLOCK = "budget_wall_clock"
    BUDGET_OUTPUT_SIZE = "budget_output_size"
    BUDGET_COST = "budget_cost"
    UNDECLARED_EGRESS = "undeclared_egress"
    SECRET_LEAK = "secret_leak"
    UNRESOLVED_SECRET_REF = "unresolved_secret_ref"
    SECRET_BUNDLE_TOO_LARGE = "secret_bundle_too_large"
    NON_FINITE_OUTPUT = "non_finite_output"

    # --- container ---------------------------------------------------------
    IMAGE_PROVENANCE_MISMATCH = "image_provenance_mismatch"


RUNTIME_REFUSAL_CODES: frozenset[str] = frozenset(code.value for code in RefusalCode)
"""Every code the runtime reserves; no operation may declare one of these."""

REFUSAL_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
"""The spelling of any refusal code, the runtime's or an operation's."""


def check_operation_refusal_code(code: str) -> str:
    """``code`` if an operation may declare it; ``ValueError`` otherwise."""

    if not REFUSAL_CODE_RE.fullmatch(code):
        raise ValueError(f"refusal code {code!r} is not a canonical identifier")
    if code in RUNTIME_REFUSAL_CODES:
        raise ValueError(
            f"refusal code {code!r} is reserved by the runtime; an operation declares its own"
        )
    return code


class Refusal(BaseModel):
    """A typed refusal, serialisable into receipts and result envelopes.

    ``code`` is either one of the runtime's :class:`RefusalCode` values or a
    code the bound operation declares. Which one is decided by the spelling,
    because the two vocabularies are disjoint.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    message: str
    detail: dict[str, Any] = Field(default_factory=dict)

    @field_validator("code")
    @classmethod
    def _canonical_code(cls, value: str) -> str:
        if not REFUSAL_CODE_RE.fullmatch(value):
            raise ValueError(f"refusal code {value!r} is not a canonical identifier")
        return value

    @property
    def is_runtime(self) -> bool:
        """Whether this is one of the runtime's own refusals."""

        return self.code in RUNTIME_REFUSAL_CODES

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.code}: {self.message}"


class ProviderErrorPayload(BaseModel):
    """A provider-reported failure (an attempted answer that did not succeed)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str
    message: str
    detail: dict[str, Any] = Field(default_factory=dict)


class RefusalError(Exception):
    """Raised whenever the runtime declines to proceed, under one of its own codes."""

    def __init__(
        self,
        code: RefusalCode,
        message: str,
        **detail: Any,
    ) -> None:
        self.refusal = Refusal(code=RefusalCode(code).value, message=message, detail=detail)
        super().__init__(str(self.refusal))

    @property
    def code(self) -> RefusalCode:
        return RefusalCode(self.refusal.code)


def refuse(code: RefusalCode, message: str, **detail: Any) -> RefusalError:
    """Build a :class:`RefusalError` (call sites ``raise refuse(...)``)."""

    return RefusalError(code, message, **detail)


class Declined(Exception):
    """An operation refusal, raised from anywhere inside provider code.

    The same thing ``ProviderResult.refused`` returns, for a provider that
    detects the condition deep in a call stack. ``code`` is one the operation
    declares; a runtime code here is refused as a protocol violation.
    """

    def __init__(self, code: str, message: str, **detail: Any) -> None:
        check_operation_refusal_code(code)
        self.refusal = Refusal(code=code, message=message, detail=detail)
        super().__init__(str(self.refusal))

    @property
    def code(self) -> str:
        return self.refusal.code


def decline(code: str, message: str, **detail: Any) -> Declined:
    """Build a :class:`Declined` (call sites ``raise decline(...)``)."""

    return Declined(code, message, **detail)
