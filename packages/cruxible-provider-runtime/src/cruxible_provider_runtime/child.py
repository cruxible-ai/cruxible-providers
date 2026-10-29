"""The provider-side process harness.

Run as ``python -m cruxible_provider_runtime.child``: reads a run context from
stdin, resolves the entrypoint, checks that it serves the run's operation,
opens the secret channel the run context names, invokes the provider, and
writes a result envelope on the process's original standard output.

**Only the harness produces runtime refusals.** A refusal the harness raises
from its own checks carries a runtime code; a refusal from provider code --
returned or raised, at import, construction, declaration access or call --
must use a code the operation declares, and one using a runtime code is
reported as a protocol violation. Every provider-controlled step runs inside
one guard (``_provider_code``), the result is copied into plain JSON values
inside it, and the harness's own refusals are raised outside it.

**Dispatch is by entrypoint, checked against the operation.** The run context
names one entrypoint and one operation. The entrypoint object declares the
operations it serves (``operations``); a context naming an operation it does
not declare refuses ``undeclared_operation`` before provider code is called,
and an entrypoint that declares none is a protocol violation. An ``--entrypoint``
argument, which an executor passes so the process listing names what runs, must
agree with the context's.

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
from collections.abc import Iterator, Mapping
from typing import Any, cast

from .egress import EgressRecorder
from .errors import Declined, ProviderErrorPayload, Refusal, RefusalCode, RefusalError, refuse
from .protocol import PROTOCOL_VERSION, ResultEnvelope, Trace, Unencodable, parse_run_context
from .provider_api import ProviderResult, ProviderRunContext, ProviderStatus
from .secrets import Redactor, read_secrets

__all__ = ["declared_operations", "main", "reserve_stdout", "resolve_entrypoint"]

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


def _describe(value: object, fallback: str) -> str:
    """``str(value)`` as an exact string, or ``fallback`` if provider code refuses to say."""

    try:
        text = str(value)
        return str.__str__(text) if isinstance(text, str) else fallback
    except BaseException:
        return fallback


def _asserted_code(exc: BaseException) -> str:
    """The code a provider-raised refusal names, read without trusting it."""

    try:
        code = exc.refusal.code  # type: ignore[attr-defined]
        return str.__str__(code) if isinstance(code, str) else "unreadable"
    except BaseException:
        return "unreadable"


@contextlib.contextmanager
def _provider_code(stage: str, entrypoint: str, operation: str | None) -> Iterator[None]:
    """Run provider code: its import, its construction, its declarations, its call.

    Runtime codes name conditions only the runtime's machinery observes, so a
    ``RefusalError`` escaping provider code -- at any of these stages -- is a
    provider asserting one, and is reported as a protocol violation naming the
    code it tried to assert. A ``Declined`` outside the call is too: an
    operation refuses only when it runs. The harness's own refusals are raised
    outside this block, so they keep their codes.
    """

    try:
        yield
    except RefusalError as exc:
        raise refuse(
            RefusalCode.PROVIDER_PROTOCOL_VIOLATION,
            f"provider code raised the runtime's own refusal code {_asserted_code(exc)!r} "
            f"during {stage}; runtime codes come from runtime machinery only, and an "
            "operation refuses under codes it declares",
            stage=stage,
            entrypoint=entrypoint,
            operation=operation,
            refused_code=_asserted_code(exc),
        ) from None
    except Declined as exc:
        if stage == "call":
            raise
        raise refuse(
            RefusalCode.PROVIDER_PROTOCOL_VIOLATION,
            f"provider code declined during {stage}; an operation refuses only when it runs",
            stage=stage,
            entrypoint=entrypoint,
            operation=operation,
            refused_code=_asserted_code(exc),
        ) from None


def resolve_entrypoint(path: str, *, operation: str | None = None) -> Any:
    """Resolve ``module:object``, instantiating a class with no arguments.

    The import, the attribute walk and the construction are provider code and
    run inside :func:`_provider_code`; what the harness concludes from them is
    refused outside it.
    """

    module_name, _, object_name = path.partition(":")
    if not module_name or not object_name:
        raise refuse(
            RefusalCode.PROVIDER_PROTOCOL_VIOLATION,
            f"entrypoint {path!r} is not 'module:object'",
            entrypoint=path,
        )
    import_failure: str | None = None
    with _provider_code("import", path, operation):
        try:
            module = importlib.import_module(module_name)
        except ImportError as exc:
            import_failure = _describe(exc, "ImportError")
    if import_failure is not None:
        raise refuse(
            RefusalCode.PROVIDER_PROTOCOL_VIOLATION,
            f"entrypoint module {module_name!r} could not be imported",
            entrypoint=path,
            detail_message=import_failure,
        )
    target: Any = module
    unresolved = False
    with _provider_code("entrypoint resolution", path, operation):
        for part in object_name.split("."):
            try:
                target = getattr(target, part)
            except AttributeError:
                unresolved = True
                break
    if unresolved:
        raise refuse(
            RefusalCode.PROVIDER_PROTOCOL_VIOLATION,
            f"entrypoint {path!r} does not resolve",
            entrypoint=path,
        )
    with _provider_code("construction", path, operation):
        if isinstance(target, type):
            target = target()
        is_callable = callable(target)
    if not is_callable:
        raise refuse(
            RefusalCode.PROVIDER_PROTOCOL_VIOLATION,
            f"entrypoint {path!r} resolved to a non-callable",
            entrypoint=path,
        )
    return target


def declared_operations(
    provider: Any, entrypoint: str, *, operation: str | None = None
) -> tuple[str, ...]:
    """The operations an entrypoint object serves, or a protocol-violation refusal.

    Read from its ``operations`` attribute: a non-empty tuple, list or frozenset
    of operation names. Absent or malformed is not "serves everything". The
    attribute is the provider's, so it is read -- and copied into exact strings
    -- inside :func:`_provider_code`.
    """

    names: tuple[str, ...] | None = None
    with _provider_code("operation declaration", entrypoint, operation):
        declared = getattr(provider, "operations", None)
        if isinstance(declared, tuple | list | frozenset):
            items = tuple(declared)
            if items and all(isinstance(item, str) for item in items):
                names = tuple(str.__str__(item) for item in items)
    if not names:
        raise refuse(
            RefusalCode.PROVIDER_PROTOCOL_VIOLATION,
            f"entrypoint {entrypoint!r} declares no operations it serves",
            entrypoint=entrypoint,
        )
    return tuple(sorted(names))


def _plain(value: Any) -> Any:
    """``value`` copied into exact JSON types; anything else becomes :class:`Unencodable`.

    Called inside :func:`_provider_code` on everything a provider returned, so
    that no provider-defined method -- a mapping's ``items``, a string
    subclass's ``__hash__``, a ``__class__`` property -- runs once the harness
    starts judging the result.
    """

    if value is None or value is True or value is False:
        return value
    if isinstance(value, str):
        return str.__str__(value)
    if isinstance(value, int):
        return int.__int__(value)
    if isinstance(value, float):
        return float.__float__(value)
    if isinstance(value, Mapping):
        return {_plain(key): _plain(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(item) for item in value]
    name = type(value).__name__
    return Unencodable(str.__str__(name) if isinstance(name, str) else "object")


def _plain_detail(value: Any) -> dict[str, Any]:
    copied = _plain(value)
    return copied if isinstance(copied, dict) else {"detail": copied}


def _captured(returned: Any) -> tuple[ProviderResult | None, str | None]:
    """The provider's result rebuilt from plain values, or why it cannot be.

    Runs inside :func:`_provider_code`. Never raises a runtime refusal itself:
    a problem is returned as a message for the harness to refuse with outside.
    """

    if type(returned) is not ProviderResult:
        return (
            None,
            f"provider returned {_describe(type(returned).__name__, 'an object')}, "
            "not a ProviderResult",
        )
    status: Any = returned.status
    if type(status) is not str or status not in ("ok", "refused", "error"):
        return None, "provider returned a result with no valid status"
    refusal: Refusal | None = None
    if returned.refusal is not None:
        if type(returned.refusal) is not Refusal:
            return None, "provider returned a refusal that is not a Refusal"
        refusal = Refusal(
            code=_plain(returned.refusal.code),
            message=_plain(returned.refusal.message),
            detail=_plain_detail(returned.refusal.detail),
        )
    error: ProviderErrorPayload | None = None
    if returned.error is not None:
        if type(returned.error) is not ProviderErrorPayload:
            return None, "provider returned an error that is not a ProviderErrorPayload"
        error = ProviderErrorPayload(
            kind=_plain(returned.error.kind),
            message=_plain(returned.error.message),
            detail=_plain_detail(returned.error.detail),
        )
    output = _plain(returned.output) if returned.output is not None else None
    events = _plain(returned.events)
    metrics = _plain(returned.metrics)
    result = ProviderResult(
        status=cast(ProviderStatus, status),
        output=output,
        refusal=refusal,
        error=error,
        metrics=metrics if isinstance(metrics, dict) else {},
        events=events if isinstance(events, list) else [],
    )
    return result, None


def _declined(exc: Declined) -> ProviderResult:
    """A refusal raised from inside the call, copied out of provider code."""

    raised = exc.refusal
    if type(raised) is not Refusal:
        raise TypeError("a Declined carries a Refusal")
    return ProviderResult(
        status="refused",
        refusal=Refusal(
            code=_plain(raised.code),
            message=_plain(raised.message),
            detail=_plain_detail(raised.detail),
        ),
    )


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


def _declared_run_id(raw: bytes) -> str:
    """The run id a context names, read before the context is validated.

    So that a context refused whole -- a 1.x context, an unknown field -- is
    still answered under the run it asked about, which is what lets the
    executor read the refusal rather than a run-id mismatch.
    """

    try:
        document = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return "unknown"
    if isinstance(document, dict) and isinstance(document.get("run_id"), str):
        return str(document["run_id"])
    return "unknown"


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    envelope_fd = reserve_stdout()
    raw = sys.stdin.buffer.read()
    secrets: dict[str, str] = {}
    # Constructed before the try, and read on EVERY exit path. An egress record
    # that survives only a successful return is a record a provider can shed by
    # failing: contact an undeclared endpoint, raise, and the comparison the
    # conformance law rests on would have nothing to compare.
    recorder = EgressRecorder()
    run_id = _declared_run_id(raw)
    try:
        context = parse_run_context(raw)
        run_id = context.run_id
        if "--entrypoint" in args:
            named = args[args.index("--entrypoint") + 1]
            if named != context.entrypoint:
                raise refuse(
                    RefusalCode.PROVIDER_PROTOCOL_VIOLATION,
                    f"the harness was started for {named!r} but the run context names "
                    f"{context.entrypoint!r}",
                    argv_entrypoint=named,
                    context_entrypoint=context.entrypoint,
                )
        entrypoint = context.entrypoint
        provider = resolve_entrypoint(entrypoint, operation=context.operation)
        operations = declared_operations(provider, entrypoint, operation=context.operation)
        if context.operation not in operations:
            raise refuse(
                RefusalCode.UNDECLARED_OPERATION,
                f"entrypoint {entrypoint!r} serves {list(operations)}, not {context.operation!r}",
                entrypoint=entrypoint,
                operation=context.operation,
                declared=list(operations),
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
            operation=context.operation,
            operation_signature_digest=context.operation_signature_digest,
            build_digest=context.build_digest,
            implementation_digest=context.implementation_digest,
            input=context.input,
            coordinates=context.coordinates,
            budgets=context.budgets,
            declared_endpoints=context.declared_endpoints,
            capture_contract=context.capture_contract,
            secrets=secrets,
            egress=recorder,
            additive=context.additive,
        )
        with _provider_code("call", entrypoint, context.operation):
            try:
                result, problem = _captured(provider(provider_context))
            except Declined as exc:
                result, problem = _declined(exc), None
        if result is None:
            raise refuse(
                RefusalCode.PROVIDER_PROTOCOL_VIOLATION,
                problem or "provider returned no result",
                entrypoint=entrypoint,
            )
        if result.refusal is not None and result.refusal.is_runtime:
            raise refuse(
                RefusalCode.PROVIDER_PROTOCOL_VIOLATION,
                f"operation {context.operation!r} returned the runtime's own refusal code "
                f"{result.refusal.code!r}; an operation refuses under codes it declares",
                operation=context.operation,
                refused_code=result.refusal.code,
            )
        trace = Trace(
            endpoints_contacted=recorder.observed(),
            events=list(result.events),
            metrics=dict(result.metrics),
        )
        envelope = _envelope(context.run_id, result, trace)
    except RefusalError as exc:
        # Only the harness's own refusals reach here: provider code runs inside
        # _provider_code, which turns a refusal it raises into a protocol
        # violation.
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
            error=ProviderErrorPayload(
                kind=_describe(type(exc).__name__, "Exception"),
                message=_describe(exc, "the provider raised an exception it would not describe"),
            ),
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
