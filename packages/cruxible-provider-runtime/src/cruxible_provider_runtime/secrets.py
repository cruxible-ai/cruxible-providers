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

import json
import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from .errors import RefusalCode, refuse

__all__ = [
    "SecretBundle",
    "open_secret_channel",
    "read_secrets",
    "Redactor",
    "assert_no_secret_leak",
    "REDACTION_PLACEHOLDER",
]

REDACTION_PLACEHOLDER = "[redacted]"

SecretBundle = Mapping[str, str]
"""ref -> credential material."""


@contextmanager
def open_secret_channel(secrets: SecretBundle) -> Iterator[int]:
    """Yield a read descriptor carrying ``secrets``, for the child to inherit.

    The material is written into the pipe and the write end closed before the
    child is spawned, so the payload is bounded by the pipe buffer. That bound
    is deliberate: credentials are small, and a provider needing megabytes of
    "credential" is doing something the contract does not describe.
    """

    payload = json.dumps(dict(secrets), sort_keys=True, separators=(",", ":")).encode("utf-8")
    read_fd, write_fd = os.pipe()
    try:
        os.set_inheritable(read_fd, True)
        os.write(write_fd, payload)
        os.close(write_fd)
        write_fd = -1
        yield read_fd
    finally:
        if write_fd != -1:
            os.close(write_fd)
        try:
            os.close(read_fd)
        except OSError:
            pass


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
        self._values = sorted(
            {value for value in secrets.values() if value}, key=len, reverse=True
        )

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
        return value

    def leaks(self, value: Any) -> list[str]:
        rendered = json.dumps(value, default=str)
        return [secret for secret in self._values if secret in rendered]


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
