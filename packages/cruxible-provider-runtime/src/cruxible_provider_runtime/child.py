"""The provider-side process harness.

Run as ``python -m cruxible_provider_runtime.child``: reads a run context from
stdin, resolves the entrypoint, opens the secret channel the run context names,
invokes the provider, and writes a result envelope to stdout.

Everything leaving this process passes through the redactor first. A provider
cannot opt out of that, which is what makes redaction testable as a conformance
property rather than a review checklist item.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from typing import Any

from .egress import EgressRecorder
from .errors import ProviderErrorPayload, RefusalCode, RefusalError, refuse
from .protocol import PROTOCOL_VERSION, ProtocolVersion, ResultEnvelope, Trace, parse_run_context
from .provider_api import ProviderResult, ProviderRunContext
from .secrets import Redactor, read_secrets

__all__ = ["main", "resolve_entrypoint"]


def resolve_entrypoint(path: str) -> Any:
    """Resolve ``module:object``, instantiating a class with no arguments."""

    module_name, _, object_name = path.partition(":")
    if not module_name or not object_name:
        raise refuse(
            RefusalCode.PROVIDER_PROTOCOL_VIOLATION,
            f"entrypoint {path!r} is not 'module:object'",
            entrypoint=path,
        )
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise refuse(
            RefusalCode.PROVIDER_PROTOCOL_VIOLATION,
            f"entrypoint module {module_name!r} could not be imported",
            entrypoint=path,
            detail_message=str(exc),
        ) from exc
    target: Any = module
    for part in object_name.split("."):
        try:
            target = getattr(target, part)
        except AttributeError as exc:
            raise refuse(
                RefusalCode.PROVIDER_PROTOCOL_VIOLATION,
                f"entrypoint {path!r} does not resolve",
                entrypoint=path,
            ) from exc
    if isinstance(target, type):
        target = target()
    if not callable(target):
        raise refuse(
            RefusalCode.PROVIDER_PROTOCOL_VIOLATION,
            f"entrypoint {path!r} resolved to a non-callable",
            entrypoint=path,
        )
    return target


def _envelope(run_id: str, result: ProviderResult, trace: Trace) -> ResultEnvelope:
    return ResultEnvelope(
        protocol_version=PROTOCOL_VERSION.render(),
        run_id=run_id,
        status=result.status,
        output=result.output,
        refusal=result.refusal,
        error=result.error,
        trace=trace,
    )


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    raw = sys.stdin.buffer.read()
    run_id = "unknown"
    secrets: dict[str, str] = {}
    try:
        context = parse_run_context(raw)
        run_id = context.run_id
        requested = ProtocolVersion.parse(context.protocol_version)
        if requested.major != PROTOCOL_VERSION.major:
            raise refuse(
                RefusalCode.UNSUPPORTED_PROTOCOL,
                f"provider harness speaks protocol {PROTOCOL_VERSION.render()}, "
                f"run context asks for {context.protocol_version}",
                supported_major=PROTOCOL_VERSION.major,
                requested=context.protocol_version,
            )
        entrypoint = (
            args[args.index("--entrypoint") + 1] if "--entrypoint" in args else (context.entrypoint)
        )
        provider = resolve_entrypoint(entrypoint)
        declared_interface = getattr(provider, "interface_id", None)
        if declared_interface is not None and declared_interface != context.interface_id:
            raise refuse(
                RefusalCode.UNDECLARED_INTERFACE,
                f"entrypoint implements {declared_interface!r}, not {context.interface_id!r}",
                entrypoint=entrypoint,
                interface_id=context.interface_id,
            )
        if context.secret_channel is not None:
            secrets = read_secrets(context.secret_channel.fd)
            missing = [ref.ref for ref in context.secret_channel.refs if ref.ref not in secrets]
            if missing:
                raise refuse(
                    RefusalCode.UNRESOLVED_SECRET_REF,
                    f"secret channel is missing refs: {missing}",
                    refs=missing,
                )
        recorder = EgressRecorder()
        provider_context = ProviderRunContext(
            run_id=context.run_id,
            interface_id=context.interface_id,
            interface_digest=context.interface_digest,
            implementation_digest=context.implementation_digest,
            input_bucket=context.input_bucket,
            input=context.input,
            coordinates=context.coordinates,
            budgets=context.budgets,
            declared_endpoints=context.declared_endpoints,
            capture_contract=context.capture_contract,
            secrets=secrets,
            egress=recorder,
            additive=context.additive,
        )
        result = provider(provider_context)
        if not isinstance(result, ProviderResult):
            raise refuse(
                RefusalCode.PROVIDER_PROTOCOL_VIOLATION,
                f"provider returned {type(result).__name__}, not a ProviderResult",
                entrypoint=entrypoint,
            )
        trace = Trace(
            endpoints_contacted=recorder.observed(),
            events=list(result.events),
            metrics=dict(result.metrics),
        )
        envelope = _envelope(context.run_id, result, trace)
    except RefusalError as exc:
        envelope = ResultEnvelope(
            protocol_version=PROTOCOL_VERSION.render(),
            run_id=run_id,
            status="refused",
            refusal=exc.refusal,
        )
    except Exception as exc:
        envelope = ResultEnvelope(
            protocol_version=PROTOCOL_VERSION.render(),
            run_id=run_id,
            status="error",
            error=ProviderErrorPayload(kind=type(exc).__name__, message=str(exc)),
        )

    redactor = Redactor(secrets)
    document: dict[str, Any] = redactor.scrub(json.loads(envelope.model_dump_json()))
    sys.stdout.buffer.write(
        json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry
    os._exit(main())
