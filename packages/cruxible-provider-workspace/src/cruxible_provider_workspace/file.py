"""``workspace.file`` — structure one authorized workspace file read.

Core performs the read. The G5 law puts everything with authority on core's
side of the process boundary: the workspace binding, the allowed roots, the
symlink walk, the ``O_NOFOLLOW`` open, the containment re-check, the policy-carried
size cap, and the ``source-read-receipt`` that attests the read was authorized.
What reaches this adapter is the *outcome* of that read — bytes, base64-encoded,
with their declared length and digest — and the adapter's whole job is to turn
those bytes into a structured capture body.

It serves one operation, ``workspace.file``: ``workspace.file.request@1`` in,
``workspace.file.result@1`` out, effect ``pure``, refusing under the three codes
its registration declares (``invalid_parameter``, ``mismatched_lengths``,
``provider_declined``) — the same signature and refusals core seeds for its own
built-in port of this adapter.

So this module is **pure** (RAT-9). It opens no file, contacts no endpoint,
reads no clock, and consults no secret. Its output is a function of its input,
which is what lets the same input replay to the same body under a later
evaluation. A conformance test asserts the purity structurally (the module's
imports) and at runtime (an ``open`` that would raise, an egress lane that would
refuse).

Three declarations, three checks. The run input declares the encoding, the byte
length, and the bytes digest of what it carries, and each is verified against
the decoded payload rather than trusted: a length that disagrees refuses, a digest
that disagrees refuses, and only a digest that agrees is echoed into the body as
the source digest. The echo is the point — a reader of the Capture gets the
digest core's receipt names, re-verified by the process that structured the bytes.

Two body shapes, selected by what the bytes are. Strict UTF-8 without a NUL is
text, and gets the decoded text plus a line view; anything else is bytes, and
gets the canonical base64 of the payload and nothing else. The text path never
normalises: the BOM stays in the text and is reported, the newline style is
reported rather than rewritten, and a line is what stands between two line feeds
with one carriage return before the feed excluded.
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Mapping
from typing import Any

from cruxible_provider_runtime.canonical import SHA256_RE
from cruxible_provider_runtime.provider_api import ProviderResult, ProviderRunContext

__all__ = [
    "BYTE_SIZE_CEILINGS",
    "CONTENT_ENCODING",
    "INPUT_FIELDS",
    "INVALID_PARAMETER",
    "MAX_STRUCTURED_BYTES",
    "MISMATCHED_LENGTHS",
    "OPERATION",
    "PROVIDER_DECLINED",
    "WorkspaceFile",
    "byte_size_class",
    "content_kind_class",
    "decode_declared_bytes",
    "input_bucket_label",
    "structure_bytes",
]

OPERATION = "workspace.file"

INVALID_PARAMETER = "invalid_parameter"
MISMATCHED_LENGTHS = "mismatched_lengths"
PROVIDER_DECLINED = "provider_declined"
"""The refusals ``workspace.file`` declares (registration.json), each absorbable."""

CONTENT_ENCODING = "base64"
"""The only content encoding the operation admits: RFC 4648 section 4, padded, no line breaks."""

BYTE_SIZE_CEILINGS: tuple[tuple[str, int], ...] = (
    ("tiny", 4_096),
    ("small", 65_536),
    ("medium", 1_048_576),
)
"""Inclusive upper bounds per size class, in order; above the last is ``large``."""

MAX_STRUCTURED_BYTES = BYTE_SIZE_CEILINGS[-1][1]
"""The most bytes the adapter structures (1 MiB). A larger payload refuses, typed.

Admission used to hold larger files back (the ``large`` size class was never
claimed); with no input classification, the adapter refuses them itself, the
way core's built-in port does.
"""

INPUT_FIELDS: tuple[str, ...] = (
    "logical_source",
    "commitment_digest",
    "content_encoding",
    "bytes",
    "byte_length",
    "bytes_digest",
)
"""The closed set of run-input fields. Anything else refuses rather than being ignored."""

_BOM = "\ufeff"


def decode_declared_bytes(payload: Mapping[str, Any]) -> bytes | None:
    """Decode the run input's payload, or ``None`` when it is not a base64 string.

    Strict: the alphabet is RFC 4648 section 4 with padding, and a stray
    character (a line break included) is not a payload.
    """

    if payload.get("content_encoding") != CONTENT_ENCODING:
        return None
    encoded = payload.get("bytes")
    if not isinstance(encoded, str):
        return None
    try:
        return base64.b64decode(encoded.encode("ascii"), validate=True)
    except (UnicodeEncodeError, ValueError):
        return None


def content_kind_class(data: bytes) -> str:
    """``text`` for strict UTF-8 without a NUL byte, ``binary`` otherwise."""

    if b"\x00" in data:
        return "binary"
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return "binary"
    return "text"


def byte_size_class(length: int) -> str:
    for class_id, ceiling in BYTE_SIZE_CEILINGS:
        if length <= ceiling:
            return class_id
    return "large"


def input_bucket_label(data: bytes) -> str:
    """The ``input_bucket`` string the result has always carried, from the bytes alone.

    Nothing classifies the input any more, so nothing hands the adapter a
    measured bucket. The result keeps the field (``workspace.file.result@1``
    declares it), derived here exactly as the retired classifier measured it,
    so the result for every input previously admitted is unchanged.
    """

    return f"content_kind={content_kind_class(data)};byte_size={byte_size_class(len(data))}"


class WorkspaceFile:
    """Structure the bytes core read from one workspace file."""

    operations = (OPERATION,)

    def __call__(self, context: ProviderRunContext) -> ProviderResult:
        # Secrets are neither needed nor read. The context carries whatever the
        # secret plan delivered; this adapter never looks, so nothing it emits
        # can carry one -- and a test asserts that with a secret delivered anyway.
        payload = context.input
        refusal = _validate_shape(payload)
        if refusal is not None:
            return refusal

        data = decode_declared_bytes(payload)
        if data is None:
            return ProviderResult.refused(
                INVALID_PARAMETER,
                "bytes is not a base64 payload under the declared content encoding",
                field="bytes",
                content_encoding=CONTENT_ENCODING,
            )

        declared_length = payload["byte_length"]
        if len(data) != declared_length:
            return ProviderResult.refused(
                MISMATCHED_LENGTHS,
                "the declared byte_length does not match the decoded payload",
                declared=declared_length,
                decoded=len(data),
            )

        computed_digest = "sha256:" + hashlib.sha256(data).hexdigest()
        declared_digest = payload["bytes_digest"]
        if computed_digest != declared_digest:
            return ProviderResult.refused(
                PROVIDER_DECLINED,
                "the declared bytes_digest does not match the decoded payload; the adapter "
                "will not structure bytes whose identity disagrees with the read receipt",
                declared=declared_digest,
                computed=computed_digest,
            )

        if len(data) > MAX_STRUCTURED_BYTES:
            return ProviderResult.refused(
                PROVIDER_DECLINED,
                "the adapter structures at most 1 MiB; read a smaller file",
                byte_length=len(data),
                max_bytes=MAX_STRUCTURED_BYTES,
            )

        content = structure_bytes(data)
        metrics: dict[str, float] = {"byte_length": float(len(data))}
        if content["kind"] == "text":
            metrics["line_count"] = float(content["line_count"])
            metrics["character_count"] = float(content["character_count"])
        return ProviderResult.ok(
            {
                "input_bucket": input_bucket_label(data),
                "source": {
                    "logical_source": payload["logical_source"],
                    "commitment_digest": payload["commitment_digest"],
                    "bytes_digest": computed_digest,
                    "byte_length": len(data),
                },
                "content": content,
            },
            metrics=metrics,
        )


def _validate_shape(payload: Mapping[str, Any]) -> ProviderResult | None:
    """Check the run input's shape, field by field, failing closed on any surprise."""

    unknown = sorted(set(payload) - set(INPUT_FIELDS))
    if unknown:
        return ProviderResult.refused(
            INVALID_PARAMETER,
            "the run input carries fields the interface does not declare",
            unknown=unknown,
            declared=list(INPUT_FIELDS),
        )
    missing = [name for name in INPUT_FIELDS if name not in payload]
    if missing:
        return ProviderResult.refused(
            INVALID_PARAMETER,
            "the run input is missing required fields",
            missing=missing,
        )

    logical_source = payload["logical_source"]
    if (
        not isinstance(logical_source, str)
        or not logical_source
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in logical_source)
    ):
        return ProviderResult.refused(
            INVALID_PARAMETER,
            "logical_source must be a non-empty string without control characters",
            field="logical_source",
        )
    for field in ("commitment_digest", "bytes_digest"):
        value = payload[field]
        if not isinstance(value, str) or not SHA256_RE.match(value):
            return ProviderResult.refused(
                INVALID_PARAMETER,
                f"{field} must be spelled sha256:<64 lowercase hex>",
                field=field,
            )
    if payload["content_encoding"] != CONTENT_ENCODING:
        return ProviderResult.refused(
            INVALID_PARAMETER,
            f"content_encoding must be {CONTENT_ENCODING!r}",
            field="content_encoding",
            declared=payload["content_encoding"],
            supported=[CONTENT_ENCODING],
        )
    if not isinstance(payload["bytes"], str):
        return ProviderResult.refused(INVALID_PARAMETER, "bytes must be a string", field="bytes")
    length = payload["byte_length"]
    if not isinstance(length, int) or isinstance(length, bool) or length < 0:
        return ProviderResult.refused(
            INVALID_PARAMETER,
            "byte_length must be a non-negative integer",
            field="byte_length",
        )
    return None


def structure_bytes(data: bytes) -> dict[str, Any]:
    """The capture body for ``data``: a text view when it is text, bytes otherwise.

    A pure function of the bytes, exposed so that the conformance fixtures can
    pin its output digest and core's classifier re-proof can call it directly.
    """

    if content_kind_class(data) != "text":
        return {
            "kind": "bytes",
            "encoding": "base64",
            "byte_length": len(data),
            "bytes": base64.b64encode(data).decode("ascii"),
        }
    text = data.decode("utf-8")
    return {
        "kind": "text",
        "encoding": "utf-8",
        "bom": text.startswith(_BOM),
        "newline": _newline_style(text),
        "trailing_newline": text.endswith(("\n", "\r")),
        "line_count": len(_lines(text)),
        "character_count": len(text),
        "text": text,
        "lines": _lines(text),
    }


def _newline_style(text: str) -> str:
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    cr = text.count("\r") - crlf
    present = [name for name, count in (("lf", lf), ("crlf", crlf), ("cr", cr)) if count]
    if not present:
        return "none"
    if len(present) == 1:
        return present[0]
    return "mixed"


def _lines(text: str) -> list[str]:
    """Split at line feeds; drop one carriage return before each feed and the empty tail.

    Deliberately not ``str.splitlines``: that also splits on form feeds, vertical
    tabs, and the Unicode separators, which a line-numbered citation into a
    source file does not expect. A lone carriage return is not a line break here
    either -- it stays inside the line, and the newline style says ``cr``.
    """

    *terminated, tail = text.split("\n")
    lines = [part[:-1] if part.endswith("\r") else part for part in terminated]
    if tail:
        # The unterminated last line. Nothing follows it, so nothing is excluded
        # from it: a carriage return at its end is content, not a separator.
        lines.append(tail)
    return lines
