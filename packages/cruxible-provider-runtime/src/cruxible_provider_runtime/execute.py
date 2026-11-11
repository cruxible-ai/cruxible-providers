"""Invoke: admission, secret delivery, budgeted execution, egress conformance.

The order is load-bearing:

1. **revalidate** — the accepted build, the package-side manifest, and the
   sealed environment are re-checked against what the binding was made under; a
   binding is reusable and all three live somewhere mutable, so a stale one
   refuses rather than executing under a withdrawn acceptance;
2. **deliver** — credential material goes over an inherited descriptor, never
   argv and never the environment block;
3. **execute** — the run context names the bound operation, its signature
   digest and the build, under executor-enforced wall-clock and output-size
   caps, a breach of which is a typed refusal rather than a provider error;
4. **check** — endpoints actually contacted are compared against the accepted
   declaration, credential material is asserted absent from everything
   destined for exhaust, an operation refusal must be one the bound
   operation declares (``undeclared_refusal``), and a runtime refusal must be
   one the child harness produces.

No input is classified. What an operation admits is its input Contract, which
core validates before the run and this library never interprets.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from contextlib import ExitStack
from dataclasses import dataclass
from typing import Any

from .backends import ContainerBackend, LocalEnvBackend
from .binding import Binding
from .budget import ProcessOutcome
from .egress import EgressComparison, compare_egress, enforce_egress
from .errors import HARNESS_REFUSAL_CODES, Refusal, RefusalCode, RefusalError, refuse
from .protocol import (
    PROTOCOL_VERSION,
    Budgets,
    ResultEnvelope,
    RunContext,
    SecretChannelSpec,
    SecretRef,
    parse_result_envelope,
)
from .registry import StubRegistry
from .secrets import Redactor, SecretBundle, assert_no_secret_leak, open_secret_channel

__all__ = [
    "InvocationOutcome",
    "check_declared_refusal",
    "invoke",
    "observed_vs_declared",
    "refusal_envelope",
]


@dataclass(frozen=True)
class InvocationOutcome:
    """What one provider run produced, after every executor-side check."""

    envelope: ResultEnvelope
    egress: EgressComparison
    build_digest: str
    operation: str
    operation_signature_digest: str
    implementation_digest: str
    materialization_digest: str
    duration_seconds: float
    stderr: str

    @property
    def status(self) -> str:
        return self.envelope.status

    def receipt_fields(self) -> dict[str, Any]:
        """The fields a receipt records about this run."""

        return {
            "build_digest": self.build_digest,
            "operation": self.operation,
            "operation_signature_digest": self.operation_signature_digest,
            "implementation_digest": self.implementation_digest,
            "materialization_digest": self.materialization_digest,
            "protocol_version": self.envelope.protocol_version,
            "status": self.envelope.status,
            "endpoints_contacted": list(self.egress.observed),
            # Always present. An empty list means the declaration was a concrete
            # allowlist that held; a non-empty one means there was no list to
            # hold, and the recording is the whole of the guarantee.
            "dynamic_endpoint_forms": list(self.egress.dynamic_forms),
            "duration_seconds": round(self.duration_seconds, 4),
        }


def invoke(
    binding: Binding,
    *,
    registry: StubRegistry,
    payload: Mapping[str, Any],
    budgets: Budgets,
    coordinates: Mapping[str, Any] | None = None,
    secrets: SecretBundle | None = None,
    capture_contract: str | None = None,
    run_id: str | None = None,
    local_backend: LocalEnvBackend | None = None,
    container_backend: ContainerBackend | None = None,
    additive: Mapping[str, Any] | None = None,
) -> InvocationOutcome:
    secrets = dict(secrets or {})
    run_id = run_id or f"run-{uuid.uuid4().hex[:12]}"

    binding.revalidate(registry)

    with ExitStack() as stack:
        # The descriptor is opened before the run context is built so that the
        # context names the fd number the child will actually see. Nothing
        # renumbers descriptors on the way into a child process.
        secret_fd = stack.enter_context(open_secret_channel(secrets)) if secrets else None
        secret_channel = (
            SecretChannelSpec(
                fd=secret_fd, refs=tuple(SecretRef(ref=ref) for ref in sorted(secrets))
            )
            if secret_fd is not None
            else None
        )
        context = RunContext(
            protocol_version=PROTOCOL_VERSION.render(),
            run_id=run_id,
            operation=binding.operation.name,
            operation_signature_digest=binding.operation_signature_digest,
            build_digest=binding.build_digest,
            implementation_digest=binding.implementation_digest,
            entrypoint=binding.implementation.entrypoint,
            coordinates=dict(coordinates or {}),
            input=dict(payload),
            capture_contract=capture_contract,
            budgets=budgets,
            declared_endpoints=binding.implementation.declared_endpoints,
            secret_channel=secret_channel,
            additive=dict(additive or {}),
        )

        # The serialised run context must never carry credential material: that
        # is the whole point of the descriptor channel, so it is asserted rather
        # than assumed.
        assert_no_secret_leak(json.loads(context.model_dump_json()), secrets, where="run context")

        outcome = _execute(
            binding,
            context,
            budgets,
            secret_fd=secret_fd,
            local_backend=local_backend,
            container_backend=container_backend,
        )

    envelope = parse_result_envelope(outcome.stdout)
    if envelope.run_id != run_id:
        raise refuse(
            RefusalCode.PROVIDER_PROTOCOL_VIOLATION,
            "provider returned a result for a different run",
            expected=run_id,
            returned=envelope.run_id,
        )

    redactor = Redactor(secrets)
    stderr = redactor.text(outcome.stderr.decode("utf-8", "replace"))
    assert_no_secret_leak(json.loads(envelope.model_dump_json()), secrets, where="result envelope")
    assert_no_secret_leak(stderr, secrets, where="provider stderr")

    egress = enforce_egress(
        binding.implementation.declared_endpoints,
        envelope.trace.endpoints_contacted,
        implementation_digest=binding.implementation_digest,
    )
    check_declared_refusal(binding, envelope)

    return InvocationOutcome(
        envelope=envelope,
        egress=egress,
        build_digest=binding.build_digest,
        operation=binding.operation.name,
        operation_signature_digest=binding.operation_signature_digest,
        implementation_digest=binding.implementation_digest,
        materialization_digest=binding.materialization_digest,
        duration_seconds=outcome.duration_seconds,
        stderr=stderr,
    )


def check_declared_refusal(binding: Binding, envelope: ResultEnvelope) -> None:
    """Refuse a refusal the envelope has no standing to carry.

    An operation refusal must be one the bound operation declares: its
    ``refusals`` are its whole vocabulary, and an undeclared code is not passed
    through as if it meant something (``undeclared_refusal``). A runtime
    refusal must be one the child harness produces (``HARNESS_REFUSAL_CODES``);
    every other runtime code names a condition only this executor observes, so
    an envelope reporting one is a protocol violation, not an observation.
    """

    refusal = envelope.refusal
    if envelope.status != "refused" or refusal is None:
        return
    if refusal.is_runtime:
        if refusal.code not in HARNESS_REFUSAL_CODES:
            raise refuse(
                RefusalCode.PROVIDER_PROTOCOL_VIOLATION,
                f"the provider envelope reports {refusal.code!r}, a condition only the "
                "executor observes",
                operation=binding.operation.name,
                refused_code=refusal.code,
            )
        return
    if not binding.operation.declares_refusal(refusal.code):
        raise refuse(
            RefusalCode.UNDECLARED_REFUSAL,
            f"operation {binding.operation.name!r} declares no refusal {refusal.code!r}",
            operation=binding.operation.name,
            refused_code=refusal.code,
            declared=sorted(binding.operation.refusals),
        )


def _execute(
    binding: Binding,
    context: RunContext,
    budgets: Budgets,
    *,
    secret_fd: int | None,
    local_backend: LocalEnvBackend | None,
    container_backend: ContainerBackend | None,
) -> ProcessOutcome:
    stdin_bytes = context.to_json()
    pass_fds = (secret_fd,) if secret_fd is not None else ()
    if binding.backend_kind == "local_env":
        if local_backend is None or binding.env_path is None:
            raise refuse(
                RefusalCode.UNSUPPORTED_BACKEND,
                "a local_env invocation needs a local backend and a materialized environment",
                provider_id=binding.provider_id,
            )
        return local_backend.invoke(
            binding.env_path,
            entrypoint=binding.implementation.entrypoint,
            stdin_bytes=stdin_bytes,
            budgets=budgets,
            pass_fds=pass_fds,
        )
    if container_backend is None or binding.image_digest is None:
        raise refuse(
            RefusalCode.UNSUPPORTED_BACKEND,
            "a container invocation needs a container backend and a pinned image",
            provider_id=binding.provider_id,
        )
    return container_backend.invoke(
        binding.image_digest,
        entrypoint=binding.implementation.entrypoint,
        stdin_bytes=stdin_bytes,
        budgets=budgets,
        pass_fds=pass_fds,
    )


def refusal_envelope(run_id: str, error: RefusalError) -> ResultEnvelope:
    """Render an executor-side refusal in the same shape a provider would."""

    refusal: Refusal = error.refusal
    return ResultEnvelope(
        protocol_version=PROTOCOL_VERSION.render(),
        run_id=run_id,
        status="refused",
        refusal=refusal,
    )


def observed_vs_declared(binding: Binding, envelope: ResultEnvelope) -> EgressComparison:
    """Compare without enforcing — for reporting surfaces and CI lanes."""

    return compare_egress(
        binding.implementation.declared_endpoints, envelope.trace.endpoints_contacted
    )
