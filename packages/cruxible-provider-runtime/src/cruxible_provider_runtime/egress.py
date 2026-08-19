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

Endpoints normalise to ``scheme://host[:port]``. Paths are deliberately dropped:
the contract is about who a provider talks to, not what it asks them.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .errors import RefusalCode, refuse

__all__ = [
    "EgressComparison",
    "EgressRecorder",
    "compare_egress",
    "enforce_egress",
    "no_network",
    "normalize_endpoint",
]

_DEFAULT_PORTS = {"http": 80, "https": 443, "ftp": 21}


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

    @property
    def conformant(self) -> bool:
        return not self.undeclared


def compare_egress(declared: Iterable[str], observed: Iterable[str]) -> EgressComparison:
    """Compare a declaration against what was observed."""

    declared_set = {normalize_endpoint(value) for value in declared}
    observed_set = {normalize_endpoint(value) for value in observed}
    return EgressComparison(
        declared=tuple(sorted(declared_set)),
        observed=tuple(sorted(observed_set)),
        undeclared=tuple(sorted(observed_set - declared_set)),
        unused=tuple(sorted(declared_set - observed_set)),
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


@contextmanager
def no_network() -> Iterator[None]:
    """Block outbound sockets for the duration of the block.

    Used by the no-network egress-conformance lane so that "this adapter
    declares zero endpoints" is a tested property rather than a claim. It is a
    test instrument, not a containment mechanism: it only patches this
    interpreter's socket module.
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
