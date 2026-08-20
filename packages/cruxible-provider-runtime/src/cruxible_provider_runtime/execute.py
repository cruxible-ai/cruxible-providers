"""Invoke: admission, secret delivery, budgeted execution, egress conformance.

The order is load-bearing:

1. **admit** — the bucket is derived from the actual input by the interface's
   registered classifier and matched against what the implementation claims; an
   unclaimed bucket refuses before any process is started;
2. **deliver** — credential material goes over an inherited descriptor, never
   argv and never the environment block;
3. **execute** — under executor-enforced wall-clock and output-size caps, a
   breach of which is a typed refusal rather than a provider error;
4. **check** — endpoints actually contacted are compared against the accepted
   declaration, and credential material is asserted absent from everything
   destined for exhaust.
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
from .errors import Refusal, RefusalCode, RefusalError, refuse
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

__all__ = ["InvocationOutcome", "invoke", "observed_vs_declared", "refusal_envelope"]


@dataclass(frozen=True)
class InvocationOutcome:
    """What one provider run produced, after every executor-side check."""

    envelope: ResultEnvelope
    egress: EgressComparison
    input_bucket: str
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
            "implementation_digest": self.implementation_digest,
            "materialization_digest": self.materialization_digest,
            "protocol_version": self.envelope.protocol_version,
            "input_bucket": self.input_bucket,
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

    input_bucket = registry.admit(
        binding.interface_id, binding.implementation.declared_input_buckets, payload
    )

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
            interface_id=binding.interface_id,
            interface_digest=binding.interface_digest,
            implementation_digest=binding.implementation_digest,
            entrypoint=binding.implementation.entrypoint,
            coordinates=dict(coordinates or {}),
            input=dict(payload),
            input_bucket=input_bucket,
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

    return InvocationOutcome(
        envelope=envelope,
        egress=egress,
        input_bucket=input_bucket,
        implementation_digest=binding.implementation_digest,
        materialization_digest=binding.materialization_digest,
        duration_seconds=outcome.duration_seconds,
        stderr=stderr,
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
