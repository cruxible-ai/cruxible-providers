"""A read-only view of one Capture a derive reads, and the wire for one it returns.

Core hands a derive each ``capture<K>`` input as Capture wire -- the same
``ProviderResultToExternalCapture`` shape a provider returns, dumped as JSON
with absent optionals left out::

    {"tag": "playbill-provider-result-to-external-capture-v1",
     "source_identity": "web.response", "coordinate_type": "http-response-v1",
     "coordinate": {...}, "selector_type": "whole-response-v1", "selector": {},
     "replayability": "attested_only", "content_encoding": "base64",
     "content_base64": "...", "byte_length": 1234, "bytes_digest": "sha256:...",
     "observed_at": "2026-11-04T08:30:00Z",
     "source_effective_time": {...}}            # only when the Capture states one

:class:`CaptureView` reads that wire once, checks that the bytes reproduce the
stated length and digest (:func:`.acquisition.capture_input`), and exposes the
bytes (``raw_bytes``, ``text``, ``json()``) and the metadata as attributes. A
wire that is not this shape is a broken transport and raises
:class:`.acquisition.CaptureInputError`; it is never repaired.

:func:`derived_capture` is the other direction: the wire for a document a pure
operation computes, for a ``capture<K>`` field of its result -- a derived kind,
or a ``procedure_output`` kind whose Capture core commits with its bytes as the
source. It encodes, measures and digests the body so the author does not. A pure
operation must give byte-identical output for identical input, the whole wire
included, so the observation time is never the clock: it is stated, or taken
from the Captures the document was derived from (:meth:`CaptureView.derive`).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from copy import deepcopy
from datetime import datetime
from types import MappingProxyType
from typing import Any, Final

from .acquisition import (
    CaptureInputError,
    capture_input,
    capture_observed_at,
    external_capture_result,
)
from .canonical import canonical_json

__all__ = [
    "DERIVED_COORDINATE_TYPE",
    "DERIVED_SELECTOR_TYPE",
    "DERIVED_SOURCE_IDENTITY",
    "REPLAYABILITIES",
    "CaptureView",
    "capture_body",
    "derived_capture",
]

DERIVED_SOURCE_IDENTITY: Final = "derived"
"""The source label :func:`derived_capture` writes unless told otherwise.

Under a ``procedure_output`` kind the Capture's source is its own bytes and core
keeps the wire's labels only in the output the invocation receipt digests. A
derived kind with an external source pins its own labels; pass those instead.
"""
DERIVED_COORDINATE_TYPE: Final = "derived-v1"
DERIVED_SELECTOR_TYPE: Final = "whole-document-v1"
REPLAYABILITIES: Final = frozenset({"exact", "attested_only"})

#: Core's spelling of a logical source identity and of a coordinate/selector type.
_SOURCE_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,255}$")
_SCHEMA_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")


def _label(wire: Mapping[str, Any], key: str, pattern: re.Pattern[str]) -> str:
    value = wire.get(key)
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise CaptureInputError(f"a capture input's {key} is not a canonical identifier")
    return value


def _check_effective_time(value: object) -> None:
    if value is None:
        return
    if not isinstance(value, Mapping) or not isinstance(value.get("effective_from"), str):
        raise CaptureInputError("a capture input's source_effective_time is not an interval")
    for key in ("effective_from", "effective_until"):
        if value.get(key) is None:
            continue
        try:
            moment = datetime.fromisoformat(str(value[key]))
        except ValueError as exc:
            raise CaptureInputError(
                f"a capture input's source_effective_time.{key} does not parse"
            ) from exc
        if moment.utcoffset() is None:
            raise CaptureInputError(f"a capture input's source_effective_time.{key} has no offset")


class CaptureView:
    """One ``capture<K>`` input, read and checked: its exact bytes and their metadata.

    Construct it from the wire core sends (``CaptureView(wire)``); a ``@derive``
    operation receives one already built for each parameter annotated
    ``CaptureView`` (or named in ``capture_inputs``). The view is immutable and
    holds a private copy of the wire, so nothing the derive does changes what it
    read.

    Attributes are the wire's: ``source_identity``, ``coordinate_type``,
    ``selector_type``, ``replayability``, ``byte_length``, ``bytes_digest`` and
    ``observed_at`` (an aware ``datetime``). ``coordinate``, ``selector`` and
    ``source_effective_time`` (the stated interval, or ``None``) are plain JSON
    values, a fresh copy on every access, so they compare equal to the JSON they
    came from and changing one changes nothing in the view.
    """

    __slots__ = (
        "_bytes",
        "_wire",
        "byte_length",
        "bytes_digest",
        "coordinate_type",
        "observed_at",
        "replayability",
        "selector_type",
        "source_identity",
    )

    _bytes: bytes
    _wire: dict[str, Any]
    source_identity: str
    coordinate_type: str
    selector_type: str
    replayability: str
    byte_length: int
    bytes_digest: str
    observed_at: datetime

    def __init__(self, wire: Mapping[str, Any]) -> None:
        content, _coordinate, _coordinate_type = capture_input(wire)
        observed_at = capture_observed_at(wire)
        source_identity = _label(wire, "source_identity", _SOURCE_ID_RE)
        coordinate_type = _label(wire, "coordinate_type", _SCHEMA_ID_RE)
        selector_type = _label(wire, "selector_type", _SCHEMA_ID_RE)
        if "selector" not in wire:
            raise CaptureInputError("a capture input carries no selector")
        replayability = wire.get("replayability")
        if replayability not in REPLAYABILITIES:
            raise CaptureInputError(
                f"a capture input's replayability is one of {sorted(REPLAYABILITIES)}"
            )
        _check_effective_time(wire.get("source_effective_time"))
        assign = object.__setattr__
        assign(self, "_bytes", content)
        assign(self, "_wire", deepcopy(dict(wire)))
        assign(self, "source_identity", source_identity)
        assign(self, "coordinate_type", coordinate_type)
        assign(self, "selector_type", selector_type)
        assign(self, "replayability", str(replayability))
        assign(self, "byte_length", len(content))
        assign(self, "bytes_digest", str(wire["bytes_digest"]))
        assign(self, "observed_at", observed_at)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError(f"a CaptureView is read-only; cannot set {name!r}")

    def __delattr__(self, name: str) -> None:
        raise AttributeError(f"a CaptureView is read-only; cannot delete {name!r}")

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, CaptureView):
            return NotImplemented
        return self._wire == other._wire

    __hash__ = None  # type: ignore[assignment]

    def __repr__(self) -> str:
        return (
            f"CaptureView(source_identity={self.source_identity!r}, "
            f"coordinate_type={self.coordinate_type!r}, byte_length={self.byte_length}, "
            f"bytes_digest={self.bytes_digest!r})"
        )

    @property
    def coordinate(self) -> Any:
        """How the Capture was obtained, as its kind's coordinate type spells it."""

        return deepcopy(self._wire["coordinate"])

    @property
    def selector(self) -> Any:
        return deepcopy(self._wire["selector"])

    @property
    def source_effective_time(self) -> dict[str, Any] | None:
        """The validity window the Capture states, or ``None``."""

        value = self._wire.get("source_effective_time")
        return None if value is None else dict(deepcopy(value))

    @property
    def raw(self) -> Mapping[str, Any]:
        """The wire exactly as it arrived: a read-only mapping over a fresh copy."""

        return MappingProxyType(deepcopy(self._wire))

    @property
    def raw_bytes(self) -> bytes:
        """The Capture's exact bytes, decoded from the transport and digest-checked."""

        return self._bytes

    @property
    def text(self) -> str:
        """The bytes as UTF-8 text; :class:`CaptureInputError` if they are not UTF-8."""

        try:
            return self._bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CaptureInputError(
                f"the Capture's bytes are not UTF-8 text ({self.bytes_digest})"
            ) from exc

    def json(self) -> Any:
        """The bytes parsed as one JSON document; :class:`CaptureInputError` if they are not."""

        try:
            return json.loads(self.text)
        except ValueError as exc:
            raise CaptureInputError(
                f"the Capture's bytes are not one JSON document ({self.bytes_digest})"
            ) from exc

    def wire(self) -> dict[str, Any]:
        """A fresh, mutable copy of the wire, for returning or re-sending it."""

        return deepcopy(self._wire)

    def derive(
        self,
        body: object,
        *,
        coordinate: object = None,
        source_identity: str = DERIVED_SOURCE_IDENTITY,
        coordinate_type: str = DERIVED_COORDINATE_TYPE,
        selector_type: str = DERIVED_SELECTOR_TYPE,
        selector: object = None,
    ) -> dict[str, Any]:
        """The wire for a document derived from this Capture, observed when it was.

        Shorthand for ``derived_capture(body, derived_from=self, ...)``.
        """

        return derived_capture(
            body,
            derived_from=self,
            coordinate=coordinate,
            source_identity=source_identity,
            coordinate_type=coordinate_type,
            selector_type=selector_type,
            selector=selector,
        )


def capture_body(body: object) -> bytes:
    """The exact bytes a derived Capture commits for ``body``.

    ``bytes`` (or ``bytearray`` / ``memoryview``) are taken verbatim; a ``str``
    is encoded as UTF-8; anything else is one JSON document, written as
    canonical JSON (UTF-8, sorted keys, no insignificant whitespace, no
    NaN/Infinity), so that the same value always commits the same bytes.
    """

    if isinstance(body, bytes):
        return body
    if isinstance(body, bytearray | memoryview):
        return bytes(body)
    if isinstance(body, str):
        return body.encode("utf-8")
    try:
        return canonical_json(body)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"a derived Capture's body is bytes, str or one JSON value, not {type(body).__name__}"
        ) from exc


def derived_capture(
    body: object,
    *,
    observed_at: datetime | None = None,
    derived_from: CaptureView | Iterable[CaptureView] = (),
    coordinate: object = None,
    source_identity: str = DERIVED_SOURCE_IDENTITY,
    coordinate_type: str = DERIVED_COORDINATE_TYPE,
    selector_type: str = DERIVED_SELECTOR_TYPE,
    selector: object = None,
) -> dict[str, Any]:
    """The Capture wire for a document a pure operation computes.

    ``body`` becomes the committed bytes (:func:`capture_body`). The wire is the
    one :func:`.acquisition.external_capture_result` writes -- base64 transport,
    ``byte_length``, a ``sha256:`` ``bytes_digest`` -- so it is exactly the
    ``ProviderResultToExternalCapture`` shape core reads.

    The observation time is ``observed_at`` when given (timezone-aware), and
    otherwise the latest ``observed_at`` of the Captures named in
    ``derived_from``. With neither, this raises: a pure operation never reads
    the clock, because a re-run must produce the same wire.

    ``coordinate`` defaults to ``{}``; give each member of a ``list<capture<K>>``
    field its own. The source labels default to :data:`DERIVED_SOURCE_IDENTITY`,
    :data:`DERIVED_COORDINATE_TYPE` and :data:`DERIVED_SELECTOR_TYPE`, which a
    ``procedure_output`` kind accepts; a derived kind that pins its own labels
    needs those passed.
    """

    sources = (derived_from,) if isinstance(derived_from, CaptureView) else tuple(derived_from)
    for source in sources:
        if not isinstance(source, CaptureView):
            raise TypeError(f"derived_from names CaptureViews, not {type(source).__name__}")
    if observed_at is None:
        if not sources:
            raise ValueError(
                "a derived Capture states when it was observed: pass observed_at, or the "
                "Captures it was derived_from (a pure operation never reads the clock)"
            )
        observed_at = max(source.observed_at for source in sources)
    if not _SOURCE_ID_RE.fullmatch(source_identity):
        raise ValueError(f"source_identity {source_identity!r} is not a logical name")
    for label, value in (("coordinate_type", coordinate_type), ("selector_type", selector_type)):
        if not _SCHEMA_ID_RE.fullmatch(value):
            raise ValueError(f"{label} {value!r} is not a canonical identifier")
    return external_capture_result(
        capture_body(body),
        source_identity=source_identity,
        coordinate_type=coordinate_type,
        coordinate={} if coordinate is None else coordinate,
        observed_at=observed_at,
        selector_type=selector_type,
        selector=selector,
    )
