"""Secret delivery over an inherited descriptor, and redaction.

The executor resolves secret-refs and delivers credential material to the
provider process over an inherited pipe/fd named by the run context — never
argv, never inherited env, never the cache directory, never any digest preimage,
never trace or exhaust.

The local isolated environment is a **dependency-isolation mechanism, not a
security boundary**: a local provider runs with the operator's privileges.
Third-party providers are contained only in the cloud container backend, and
marketplace surfaces must label local execution of third-party providers
accordingly. Delivering over a descriptor still buys something real — the
material stays out of the process table, out of the environment block children
inherit, and out of everything that gets persisted — but it is not containment.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from .errors import RefusalCode, refuse

__all__ = [
    "MAX_SECRET_BUNDLE_BYTES",
    "REDACTION_PLACEHOLDER",
    "Redactor",
    "SecretBundle",
    "assert_no_secret_leak",
    "open_secret_channel",
    "read_secrets",
]

REDACTION_PLACEHOLDER = "[redacted]"

MAX_SECRET_BUNDLE_BYTES = 65_536
"""The largest credential bundle the channel will carry.

Credentials are small. A caller asking to deliver megabytes of "credential" is
doing something the contract does not describe, and the honest answer is a typed
refusal rather than a channel that grows to fit whatever it is handed.
"""

SecretBundle = Mapping[str, str]
"""ref -> credential material."""


def _write_bundle(write_fd: int, payload: bytes) -> None:
    """Deliver the whole bundle, then close the write end.

    Runs off the calling thread. ``os.write`` on a blocking pipe returns short
    when the kernel buffer fills, so the loop is the delivery guarantee, not a
    formality — and running it on its own thread is what keeps a bundle larger
    than the pipe buffer from parking the executor before the reader exists.
    """

    try:
        offset = 0
        while offset < len(payload):
            offset += os.write(write_fd, payload[offset:])
    except OSError:
        # The reader went away before the bundle was delivered. The provider side
        # reports the refs it did not receive; there is nothing to say here that
        # would not be a duplicate of that.
        pass
    finally:
        with contextlib.suppress(OSError):
            os.close(write_fd)


@contextmanager
def open_secret_channel(secrets: SecretBundle) -> Iterator[int]:
    """Yield a read descriptor carrying ``secrets``, for the child to inherit.

    The descriptor is handed back immediately and the material is delivered by a
    writer thread, because the child that drains the pipe is spawned *after* this
    yields. Writing the payload inline first — the original shape — deadlocks the
    executor whenever the bundle exceeds the pipe buffer, and it deadlocks it
    outside the wall-clock supervisor, which only watches a child that by then
    does not exist.
    """

    payload = json.dumps(dict(secrets), sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_SECRET_BUNDLE_BYTES:
        raise refuse(
            RefusalCode.SECRET_BUNDLE_TOO_LARGE,
            f"credential bundle is {len(payload)} bytes, over the "
            f"{MAX_SECRET_BUNDLE_BYTES}-byte channel limit",
            size_bytes=len(payload),
            limit_bytes=MAX_SECRET_BUNDLE_BYTES,
            refs=sorted(secrets),
        )
    read_fd, write_fd = os.pipe()
    os.set_inheritable(read_fd, True)
    writer = threading.Thread(target=_write_bundle, args=(write_fd, payload), daemon=True)
    writer.start()
    try:
        yield read_fd
    finally:
        # The read end closes first on purpose: a writer still holding an
        # undrained bundle then fails with EPIPE and exits, where joining it
        # first would wait for a reader that is never coming.
        with contextlib.suppress(OSError):
            os.close(read_fd)
        writer.join(timeout=5)


def read_secrets(fd: int) -> dict[str, str]:
    """Read the credential bundle from the inherited descriptor (provider side)."""

    chunks: list[bytes] = []
    with os.fdopen(fd, "rb", closefd=True) as handle:
        while True:
            chunk = handle.read(65536)
            if not chunk:
                break
            chunks.append(chunk)
    raw = b"".join(chunks)
    if not raw:
        return {}
    try:
        document = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise refuse(
            RefusalCode.UNRESOLVED_SECRET_REF,
            "secret channel did not carry valid UTF-8 JSON",
        ) from exc
    if not isinstance(document, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in document.items()
    ):
        raise refuse(
            RefusalCode.UNRESOLVED_SECRET_REF,
            "secret channel payload is not a flat ref->material mapping",
        )
    return dict(document)


class Redactor:
    """Scrubs known credential material out of anything about to be persisted."""

    def __init__(self, secrets: SecretBundle) -> None:
        # Longest first, so an overlapping shorter value cannot leave a tail.
        self._values = sorted({value for value in secrets.values() if value}, key=len, reverse=True)

    def text(self, value: str) -> str:
        for secret in self._values:
            value = value.replace(secret, REDACTION_PLACEHOLDER)
        return value

    def scrub(self, value: Any) -> Any:
        """Recursively redact strings inside JSON-able structures, keys included."""

        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {self.scrub(key): self.scrub(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.scrub(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self.scrub(item) for item in value)
        if isinstance(value, (bytes, bytearray)):
            scrubbed = bytes(value)
            for secret in self._values:
                scrubbed = scrubbed.replace(
                    secret.encode("utf-8", "surrogatepass"),
                    REDACTION_PLACEHOLDER.encode("utf-8"),
                )
            return scrubbed
        return value

    def leaks(self, value: Any) -> list[str]:
        """Which credential values survive anywhere inside ``value``.

        The walk is structural, mirroring :meth:`scrub`. An earlier version
        serialised to JSON and substring-searched the result, which silently
        missed every credential containing a character JSON escapes — a quote, a
        backslash, a newline — and every non-ASCII credential, because
        ``json.dumps`` escapes those by default. A detector that fails on
        exactly the values an attacker would choose is worse than no detector,
        because it reads as coverage.
        """

        found: list[str] = []
        for secret in self._values:
            if self._contains(value, secret):
                found.append(secret)
        return found

    def _contains(self, value: Any, secret: str) -> bool:
        if isinstance(value, str):
            return secret in value
        if isinstance(value, dict):
            return any(
                self._contains(key, secret) or self._contains(item, secret)
                for key, item in value.items()
            )
        if isinstance(value, (list, tuple, set, frozenset)):
            return any(self._contains(item, secret) for item in value)
        if isinstance(value, (bytes, bytearray)):
            return secret.encode("utf-8", "surrogatepass") in bytes(value)
        if value is None or isinstance(value, (bool, int, float)):
            return False
        # Anything else is compared by its own repr, which is what would end up
        # in exhaust if it were serialised with a fallback encoder.
        return secret in repr(value)


def assert_no_secret_leak(payload: Any, secrets: SecretBundle, *, where: str) -> None:
    """Refuse if credential material survives into ``payload``."""

    redactor = Redactor(secrets)
    leaked = redactor.leaks(payload)
    if leaked:
        refs = [ref for ref, value in secrets.items() if value in leaked]
        raise refuse(
            RefusalCode.SECRET_LEAK,
            f"credential material reached {where}",
            where=where,
            refs=sorted(refs),
        )
