"""The invocation protocol: run context in, result envelope out.

``protocol_version`` is ``major.minor`` and governs the transport *envelope*.
The interface digest governs a slot's input/output/refusal *schema*. They are
independent axes and neither is derivable from the other.

Versioning rules:

* the manifest declares the protocol majors it supports;
* bind refuses an unsupported or absent major (``unsupported_protocol``);
* minor is additive-only — a provider ignores unknown additive run-context
  fields within its major, and refuses unknown fields outside the additive
  region (``unknown_run_context_field``).

The additive region is the explicit ``additive`` mapping. Everything else in the
envelope is closed, so "additive-only" is a structural property rather than a
promise to be careful.

**Nothing non-finite leaves a provider as a success.** A NaN or an infinity
anywhere inside a result envelope refuses, recursively, at this layer. It is a
protocol rule rather than a numerical one: canonical JSON has no spelling for
either value, so a NaN in an output is a value no receipt can record and no
digest can cover, whatever the implementation meant by it. Each plane still owes
its own domain check — a degenerate sample deserves a decline that says so, not
a generic one — and this is the floor underneath all of them.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterator, Mapping
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .errors import ProviderErrorPayload, Refusal, RefusalCode, refuse

__all__ = [
    "PROTOCOL_VERSION",
    "Budgets",
    "ProtocolVersion",
    "ResultEnvelope",
    "RunContext",
    "SecretChannelSpec",
    "SecretRef",
    "Trace",
    "parse_result_envelope",
    "parse_run_context",
    "reject_non_finite",
]

_MAX_REPORTED_PATHS = 10


def _non_finite_paths(value: Any, path: str) -> Iterator[str]:
    """Every path inside ``value`` holding a NaN or an infinity."""

    if isinstance(value, bool):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            yield path or "<root>"
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield from _non_finite_paths(item, f"{path}.{key}" if path else str(key))
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            yield from _non_finite_paths(item, f"{path}[{index}]")


def reject_non_finite(value: Any, *, where: str) -> None:
    """Refuse a payload carrying a non-finite float anywhere inside it.

    The walk is recursive because the shallow version of this check is the one
    that passes: a NaN is almost never at the top of an output, it is the third
    element of an interval inside a list of forecasts.
    """

    found = sorted(set(_non_finite_paths(value, "")))
    if found:
        raise refuse(
            RefusalCode.NON_FINITE_OUTPUT,
            f"{where} carries a non-finite number, which canonical JSON cannot represent",
            where=where,
            paths=found[:_MAX_REPORTED_PATHS],
            count=len(found),
        )


class ProtocolVersion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    major: int
    minor: int

    @classmethod
    def parse(cls, value: str) -> ProtocolVersion:
        major, _, minor = value.partition(".")
        try:
            return cls(major=int(major), minor=int(minor))
        except ValueError as exc:
            raise refuse(
                RefusalCode.UNSUPPORTED_PROTOCOL,
                f"protocol version {value!r} is not major.minor",
                protocol_version=value,
            ) from exc

    def render(self) -> str:
        return f"{self.major}.{self.minor}"


PROTOCOL_VERSION = ProtocolVersion(major=1, minor=0)
"""The protocol version this runtime speaks."""


class Budgets(BaseModel):
    """Caps the executor enforces out-of-process.

    ``cost_units`` travels with the run so a provider can report against it, but
    RP-0 enforces only the two caps it can enforce locally: wall clock and output
    size. Cost enforcement belongs to the metering substrate.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    wall_clock_seconds: float
    output_bytes: int
    cost_units: float | None = None

    @field_validator("wall_clock_seconds")
    @classmethod
    def _positive_time(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("wall_clock_seconds must be positive")
        return value

    @field_validator("output_bytes")
    @classmethod
    def _positive_size(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("output_bytes must be positive")
        return value


class SecretRef(BaseModel):
    """Names a credential without carrying it.

    Only the ref travels in the serialised run context. The material arrives
    separately over the inherited descriptor named by :class:`SecretChannelSpec`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    ref: str
    purpose: str = ""


class SecretChannelSpec(BaseModel):
    """Where the provider process reads credential material from."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["fd"] = "fd"
    fd: int
    refs: tuple[SecretRef, ...] = ()

    @field_validator("fd")
    @classmethod
    def _not_standard_stream(cls, value: int) -> int:
        if value <= 2:
            raise ValueError("the secret channel must not reuse a standard stream")
        return value


class RunContext(BaseModel):
    """Everything the executor passes into a provider process."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: str
    run_id: str
    interface_id: str
    interface_digest: str
    implementation_digest: str
    entrypoint: str
    coordinates: dict[str, Any] = Field(default_factory=dict)
    input: dict[str, Any] = Field(default_factory=dict)
    input_bucket: str
    capture_contract: str | None = None
    budgets: Budgets
    declared_endpoints: tuple[str, ...] = ()
    secret_channel: SecretChannelSpec | None = None
    additive: dict[str, Any] = Field(
        default_factory=dict,
        description="the additive region: unknown keys here are ignored by a provider",
    )

    @model_validator(mode="after")
    def _version_shape(self) -> Self:
        ProtocolVersion.parse(self.protocol_version)
        return self

    def to_json(self) -> bytes:
        return self.model_dump_json().encode("utf-8")


class Trace(BaseModel):
    """Trace material destined for exhaust.

    ``endpoints_contacted`` is what was *actually* contacted, recorded by the
    instrumented client locally and by the proxy log in cloud. It is compared
    against the accepted declaration; a contacted endpoint outside the
    declaration is a typed conformance violation attributed to the
    implementation digest.
    """

    model_config = ConfigDict(extra="forbid")

    endpoints_contacted: list[str] = Field(default_factory=list)
    events: list[dict[str, Any]] = Field(default_factory=list)
    metrics: dict[str, float] = Field(default_factory=dict)


class ResultEnvelope(BaseModel):
    """Everything a provider process returns."""

    model_config = ConfigDict(extra="forbid")

    protocol_version: str
    run_id: str
    status: Literal["ok", "refused", "error"]
    output: dict[str, Any] | None = None
    refusal: Refusal | None = None
    error: ProviderErrorPayload | None = None
    trace: Trace = Field(default_factory=Trace)

    @model_validator(mode="after")
    def _status_consistency(self) -> Self:
        if self.status == "ok" and self.output is None:
            raise ValueError("an ok result must carry output")
        if self.status == "refused" and self.refusal is None:
            raise ValueError("a refused result must carry a refusal")
        if self.status == "error" and self.error is None:
            raise ValueError("an error result must carry an error")
        if self.status != "ok" and self.output is not None:
            raise ValueError("only an ok result may carry output")
        ProtocolVersion.parse(self.protocol_version)
        # A typed refusal rather than a ValueError, and therefore not wrapped
        # into a validation error: this is the runtime declining under a named
        # rule, and it must arrive at both ends -- the child cannot build such an
        # envelope, and the executor will not accept one built elsewhere.
        reject_non_finite(self.output, where="provider output")
        reject_non_finite(self.trace.metrics, where="provider trace metrics")
        reject_non_finite(self.trace.events, where="provider trace events")
        if self.refusal is not None:
            reject_non_finite(self.refusal.detail, where="refusal detail")
        if self.error is not None:
            reject_non_finite(self.error.detail, where="provider error detail")
        return self

    def to_json(self) -> bytes:
        return self.model_dump_json().encode("utf-8")


def _extra_fields(exc: ValidationError) -> list[str]:
    return [
        ".".join(str(part) for part in error["loc"])
        for error in exc.errors()
        if error["type"] == "extra_forbidden"
    ]


def parse_run_context(raw: bytes) -> RunContext:
    """Parse a run context on the provider side, failing closed on unknown fields."""

    try:
        document = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise refuse(
            RefusalCode.PROVIDER_PROTOCOL_VIOLATION,
            "run context is not valid UTF-8 JSON",
        ) from exc
    try:
        return RunContext.model_validate(document)
    except ValidationError as exc:
        extras = _extra_fields(exc)
        if extras:
            raise refuse(
                RefusalCode.UNKNOWN_RUN_CONTEXT_FIELD,
                f"run context carries fields outside the additive region: {extras}",
                fields=extras,
            ) from exc
        raise refuse(
            RefusalCode.PROVIDER_PROTOCOL_VIOLATION,
            "run context failed schema validation",
            errors=json.loads(exc.json()),
        ) from exc


def parse_result_envelope(raw: bytes) -> ResultEnvelope:
    """Parse a result envelope on the executor side, failing closed."""

    try:
        document = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise refuse(
            RefusalCode.PROVIDER_PROTOCOL_VIOLATION,
            "provider did not return valid UTF-8 JSON",
            head=raw[:200].decode("utf-8", "replace"),
        ) from exc
    try:
        return ResultEnvelope.model_validate(document)
    except ValidationError as exc:
        raise refuse(
            RefusalCode.PROVIDER_PROTOCOL_VIOLATION,
            "provider returned a malformed result envelope",
            errors=json.loads(exc.json()),
        ) from exc
