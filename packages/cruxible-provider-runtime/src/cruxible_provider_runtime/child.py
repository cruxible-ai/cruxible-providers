"""The provider-side process harness.

Run as ``python -m cruxible_provider_runtime.child``: reads a run context from
stdin, resolves the entrypoint, opens the secret channel the run context names,
invokes the provider, and writes a result envelope on the process's original
standard output.

Everything leaving this process passes through the redactor first. A provider
cannot opt out of that, which is what makes redaction testable as a conformance
property rather than a review checklist item.

**Standard output is reserved for the envelope, and the reservation is
enforced.** Real engines are chatty — a record-linkage library announces its
blocking time, a document converter reports its pipeline, a browser driver logs
to stdout on the way down — and a single stray line makes the envelope
unparseable, which the executor can only report as a protocol violation by a
provider that did nothing wrong. Politeness cannot fix this: the noise comes from
libraries nobody here controls. So the harness dups the real stdout to a private
descriptor before any provider code is imported, points file descriptor 1 at file
descriptor 2 for the whole run, and writes the envelope on the saved dup.
Anything a provider or its dependencies print lands in stderr, where trace
material belongs and where the executor's output-size budget still measures it.
"""

from __future__ import annotations

import contextlib
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

__all__ = ["main", "reserve_stdout", "resolve_entrypoint"]

STDOUT_FD = 1
STDERR_FD = 2


def reserve_stdout() -> int:
    """Take standard output for the envelope and give the provider stderr.

    Returns the descriptor the envelope must be written on. Called before the
    entrypoint module is imported, because import-time chatter is chatter too.
    """

    sys.stdout.flush()
    reserved = os.dup(STDOUT_FD)
    os.dup2(STDERR_FD, STDOUT_FD)
    return reserved


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
    envelope_fd = reserve_stdout()
    raw = sys.stdin.buffer.read()
    run_id = "unknown"
    secrets: dict[str, str] = {}
    # Constructed before the try, and read on EVERY exit path. An egress record
    # that survives only a successful return is a record a provider can shed by
    # failing: contact an undeclared endpoint, raise, and the comparison the
    # conformance law rests on would have nothing to compare.
    recorder = EgressRecorder()
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
            trace=Trace(endpoints_contacted=recorder.observed()),
        )
    except Exception as exc:
        envelope = ResultEnvelope(
            protocol_version=PROTOCOL_VERSION.render(),
            run_id=run_id,
            status="error",
            error=ProviderErrorPayload(kind=type(exc).__name__, message=str(exc)),
            trace=Trace(endpoints_contacted=recorder.observed()),
        )

    redactor = Redactor(secrets)
    document: dict[str, Any] = redactor.scrub(json.loads(envelope.model_dump_json()))
    payload = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")

    # Flush whatever the provider left buffered on the redirected stream first,
    # so its noise cannot arrive after the process has been reaped, and write the
    # envelope on the descriptor nothing else can reach.
    with contextlib.suppress(ValueError, OSError):
        sys.stdout.flush()
    with os.fdopen(envelope_fd, "wb", closefd=True) as out:
        out.write(payload)
        out.flush()
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry
    os._exit(main())
