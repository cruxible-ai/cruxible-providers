"""Egress instrumentation: declared endpoints versus endpoints actually contacted.

Declared endpoints are a manifest contract. The cloud backend enforces them
structurally (default-deny plus an allowlist read from the accepted artifact);
local enforcement is best-effort and is explicitly **not** a containment
guarantee — a local provider runs with the operator's privileges and can open a
socket the runtime never sees.

What the runtime does guarantee, in both backends, is *recording*: the receipt
and exhaust carry the endpoints actually contacted (instrumented client locally,
proxy log in cloud). A contacted endpoint outside the accepted declaration is a
typed conformance violation attributed to the implementation digest and visible
on its track record.

So there are two separable claims, and only one of them is a containment claim:

* **recording conformance** — declared equals observed — holds in both backends
  and is what the conformance lane tests;
* **containment** — the provider *cannot* reach an undeclared endpoint — holds
  in the cloud backend only, structurally, via default-deny plus an allowlist.

The guard below serves the first claim, not the second. It is a test instrument
that makes an unrecorded socket fail loudly during conformance runs; a provider
determined to evade it locally can, because a local provider runs with the
operator's privileges. Nothing here is a substitute for the cloud policy.

Endpoints normalise to ``scheme://host[:port]``. Paths are deliberately dropped:
the contract is about who a provider talks to, not what it asks them.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .errors import RefusalCode, refuse

__all__ = [
    "DYNAMIC_ENDPOINT_FORMS",
    "DYNAMIC_TARGET_FROM_RUN_INPUT",
    "SITECUSTOMIZE_GUARD",
    "EgressComparison",
    "EgressRecorder",
    "compare_egress",
    "enforce_egress",
    "no_network",
    "normalize_endpoint",
    "partition_declared",
    "write_child_guard",
]

_DEFAULT_PORTS = {"http": 80, "https": 443, "ftp": 21}

DYNAMIC_TARGET_FROM_RUN_INPUT = "dynamic:target-from-run-input"
"""EXPERIMENTAL. Declares that the target is decided by the run input.

Some adapters have no fixed endpoint list to declare. ``web.fetch`` is the
canonical case: the whole point of the interface is to retrieve a resource the
*caller* names, and an allowlist enumerated at acceptance time could only ever
be wrong. Refusing the case outright would delete the interface; declaring
``[]`` and then contacting things would be a lie the conformance lane catches.

So the declaration says what is true: the endpoint set is dynamic, and what
governs it is the recording rather than the list. Under this form the runtime
does **not** compare observed against a list — there is none — but it still
records every endpoint actually contacted, and the receipt says the declaration
was dynamic so that nobody reads an empty ``unused`` set as an allowlist that
held.

This is a **pre-decided disposition, and an open vocabulary item**: the spelling
is a reserved string in ``declared_endpoints`` rather than a separate manifest
field, so the field stays homogeneous (``tuple[str, ...]``) for the run context,
the comparison, and the cloud allowlist reader. It is deliberately the ONLY
dynamic form; anything else under the ``dynamic:`` prefix refuses at manifest
load. Its cloud-backend consequence is unresolved and belongs to whoever ratifies
the vocabulary: a default-deny policy cannot be built from this declaration, so a
dynamically-targeted adapter needs either a per-run allowlist derived from the
run input or an egress proxy that records without allowlisting.
"""

DYNAMIC_ENDPOINT_FORMS = frozenset({DYNAMIC_TARGET_FROM_RUN_INPUT})


def partition_declared(declared: Iterable[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Split a declaration into (concrete endpoints, dynamic forms)."""

    endpoints: list[str] = []
    dynamic: list[str] = []
    for entry in declared:
        if entry in DYNAMIC_ENDPOINT_FORMS:
            dynamic.append(entry)
        else:
            endpoints.append(normalize_endpoint(entry))
    return tuple(sorted(set(endpoints))), tuple(sorted(set(dynamic)))


def normalize_endpoint(value: str) -> str:
    """Normalise a URL or ``host:port`` string to ``scheme://host[:port]``."""

    candidate = value.strip()
    if "//" not in candidate:
        candidate = f"https://{candidate}"
    parts = urlsplit(candidate)
    scheme = (parts.scheme or "https").lower()
    host = (parts.hostname or "").lower()
    if not host:
        raise refuse(
            RefusalCode.UNDECLARED_EGRESS,
            f"cannot normalise endpoint {value!r}: no host",
            endpoint=value,
        )
    port = parts.port
    if port is None or port == _DEFAULT_PORTS.get(scheme):
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


@dataclass
class EgressRecorder:
    """Run-scoped record of endpoints a provider actually contacted.

    Providers call :meth:`record` from whatever client they use. The hook is
    deliberately client-agnostic: RP-0 vendors no HTTP library, and each plane
    package wires its own client's event hook to this recorder.
    """

    contacted: list[str] = field(default_factory=list)

    def record(self, url: str) -> str:
        endpoint = normalize_endpoint(url)
        if endpoint not in self.contacted:
            self.contacted.append(endpoint)
        return endpoint

    def observed(self) -> list[str]:
        return sorted(self.contacted)


@dataclass(frozen=True)
class EgressComparison:
    declared: tuple[str, ...]
    observed: tuple[str, ...]
    undeclared: tuple[str, ...]
    unused: tuple[str, ...]
    dynamic_forms: tuple[str, ...] = ()
    """Dynamic declaration forms in force for this comparison.

    Always rendered, both ways. An empty tuple means the declaration was a
    concrete list and ``undeclared`` is a real finding; a non-empty one means
    ``undeclared`` is empty *because nothing was compared*, which a reader must
    not mistake for an allowlist that held.
    """

    @property
    def conformant(self) -> bool:
        return not self.undeclared


def compare_egress(declared: Iterable[str], observed: Iterable[str]) -> EgressComparison:
    """Compare a declaration against what was observed.

    A dynamic form in the declaration suspends the comparison rather than
    widening it: there is no list to be outside of, so every observed endpoint
    is admitted and the form is carried on the comparison so that the receipt
    says so.
    """

    declared_set, dynamic = partition_declared(declared)
    observed_set = {normalize_endpoint(value) for value in observed}
    undeclared = () if dynamic else tuple(sorted(observed_set - set(declared_set)))
    return EgressComparison(
        declared=declared_set,
        observed=tuple(sorted(observed_set)),
        undeclared=undeclared,
        unused=tuple(sorted(set(declared_set) - observed_set)),
        dynamic_forms=dynamic,
    )


def enforce_egress(
    declared: Iterable[str],
    observed: Iterable[str],
    *,
    implementation_digest: str,
) -> EgressComparison:
    """Refuse when an endpoint outside the accepted declaration was contacted."""

    comparison = compare_egress(declared, observed)
    if comparison.undeclared:
        raise refuse(
            RefusalCode.UNDECLARED_EGRESS,
            "provider contacted endpoints outside its accepted declaration",
            implementation_digest=implementation_digest,
            declared=list(comparison.declared),
            observed=list(comparison.observed),
            undeclared=list(comparison.undeclared),
        )
    return comparison


SITECUSTOMIZE_GUARD = '''"""Injected by the Cruxible egress-conformance lane.

Blocks outbound sockets in a provider child process. This is a TEST INSTRUMENT,
not containment: it only patches this interpreter, and a provider running with
the operator's privileges can go around it. Its job is to make an unrecorded
socket fail loudly during a conformance run.
"""

import socket


def _blocked(*args, **kwargs):
    target = args[1] if len(args) > 1 else args
    raise OSError(
        "cruxible egress-conformance lane: outbound network access is blocked "
        f"(attempted {target!r})"
    )


socket.socket.connect = _blocked
socket.socket.connect_ex = _blocked
socket.create_connection = _blocked
'''


def write_child_guard(directory: Path) -> Path:
    """Write the child-process egress guard and return the path to prepend.

    The in-process :func:`no_network` guard patches the *executor's*
    interpreter. Providers run in a child process, which that patch never
    reaches — so before this existed, the conformance lane asserted a property
    about the wrong process. Putting a ``sitecustomize`` on the child's
    ``PYTHONPATH`` gets the guard into the interpreter that actually runs
    provider code.
    """

    directory.mkdir(parents=True, exist_ok=True)
    guard = directory / "sitecustomize.py"
    guard.write_text(SITECUSTOMIZE_GUARD, encoding="utf-8")
    return directory


@contextmanager
def no_network() -> Iterator[None]:
    """Block outbound sockets in *this* interpreter for the duration of the block.

    Covers the executor process. For the child process that actually runs
    provider code, see :func:`write_child_guard` — the two together are what the
    conformance lane needs, and neither is containment.
    """

    import socket

    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_create_connection = socket.create_connection

    def _blocked(*args: object, **kwargs: object) -> None:
        raise refuse(
            RefusalCode.UNDECLARED_EGRESS,
            "outbound network access is blocked in the egress-conformance lane",
            target=repr(args[1] if len(args) > 1 else args),
        )

    socket.socket.connect = _blocked  # type: ignore[method-assign]
    socket.socket.connect_ex = _blocked  # type: ignore[assignment]
    socket.create_connection = _blocked  # type: ignore[assignment]
    try:
        yield
    finally:
        socket.socket.connect = original_connect  # type: ignore[method-assign]
        socket.socket.connect_ex = original_connect_ex  # type: ignore[method-assign]
        socket.create_connection = original_create_connection
