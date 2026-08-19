"""The surface a provider package implements.

A provider is a callable object resolved from ``module:object``. It receives a
:class:`ProviderRunContext` and returns a :class:`ProviderResult`. It never sees
raw JSON, never opens the secret channel itself, and never enforces its own
budgets — all three are the executor's job, and a provider that did them itself
would be self-certifying.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from .egress import EgressRecorder
from .errors import ProviderErrorPayload, Refusal, RefusalCode
from .protocol import Budgets

__all__ = ["ProviderRunContext", "ProviderResult", "Provider"]


@dataclass(frozen=True)
class ProviderRunContext:
    """Everything a provider is given for one run."""

    run_id: str
    interface_id: str
    interface_digest: str
    implementation_digest: str
    input_bucket: str
    input: Mapping[str, Any]
    coordinates: Mapping[str, Any]
    budgets: Budgets
    declared_endpoints: tuple[str, ...]
    capture_contract: str | None
    secrets: Mapping[str, str]
    egress: EgressRecorder
    additive: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderResult:
    """What a provider returns: exactly one of output, refusal, or error."""

    status: str
    output: dict[str, Any] | None = None
    refusal: Refusal | None = None
    error: ProviderErrorPayload | None = None
    metrics: dict[str, float] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def ok(
        cls,
        output: dict[str, Any],
        *,
        metrics: dict[str, float] | None = None,
        events: list[dict[str, Any]] | None = None,
    ) -> ProviderResult:
        return cls(
            status="ok",
            output=output,
            metrics=metrics or {},
            events=events or [],
        )

    @classmethod
    def refused(
        cls, code: RefusalCode, message: str, **detail: Any
    ) -> ProviderResult:
        return cls(status="refused", refusal=Refusal(code=code, message=message, detail=detail))

    @classmethod
    def failed(cls, kind: str, message: str, **detail: Any) -> ProviderResult:
        return cls(
            status="error",
            error=ProviderErrorPayload(kind=kind, message=message, detail=detail),
        )


@runtime_checkable
class Provider(Protocol):
    """The structural contract an entrypoint object satisfies."""

    interface_id: str

    def __call__(self, context: ProviderRunContext) -> ProviderResult: ...
