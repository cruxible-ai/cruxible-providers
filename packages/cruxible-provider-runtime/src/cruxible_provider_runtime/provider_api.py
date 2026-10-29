"""The surface a provider package implements.

A provider entrypoint is a callable object resolved from ``module:object``. It
declares the operations it serves in ``operations``, receives a
:class:`ProviderRunContext` naming the one operation this run executes, and
returns a :class:`ProviderResult`. It never sees raw JSON, never opens the
secret channel itself, and never enforces its own budgets — all three are the
executor's job, and a provider that did them itself would be self-certifying.

One entrypoint may serve several operations; it reads ``context.operation`` to
tell which. The child harness refuses a run whose operation the entrypoint does
not declare before any provider code runs.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from .egress import EgressRecorder
from .errors import ProviderErrorPayload, Refusal, check_operation_refusal_code
from .protocol import Budgets

__all__ = ["Provider", "ProviderResult", "ProviderRunContext", "ProviderStatus"]


@dataclass(frozen=True)
class ProviderRunContext:
    """Everything a provider is given for one run."""

    run_id: str
    operation: str
    operation_signature_digest: str
    build_digest: str
    implementation_digest: str
    input: Mapping[str, Any]
    coordinates: Mapping[str, Any]
    budgets: Budgets
    declared_endpoints: tuple[str, ...]
    capture_contract: str | None
    secrets: Mapping[str, str] = field(repr=False)
    """Resolved credential material, keyed by ref.

    ``repr=False`` is not cosmetic. A dataclass repr is what lands in a
    traceback, a log line, or a debugger transcript, and the default one printed
    every credential the run was given.
    """

    egress: EgressRecorder
    additive: Mapping[str, Any] = field(default_factory=dict)


ProviderStatus = Literal["ok", "refused", "error"]


@dataclass(frozen=True)
class ProviderResult:
    """What a provider returns: exactly one of output, refusal, or error."""

    status: ProviderStatus
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
    def refused(cls, code: str, message: str, **detail: Any) -> ProviderResult:
        """An operation refusal under ``code``, which the operation declares.

        A runtime code is not an operation's to return; it raises here, and the
        harness reports it as a protocol violation.
        """

        check_operation_refusal_code(code)
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

    operations: tuple[str, ...]

    def __call__(self, context: ProviderRunContext) -> ProviderResult: ...
