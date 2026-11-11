"""Core's Capture wire, both ways, without minting a Capture.

Out: :func:`external_capture_result` builds the wire for one payload a provider
returns, and :func:`external_capture_results` the wire for each member of a
``list<capture<...>>`` out field. In: :func:`capture_input` reads the wire Core
hands a derive for a ``capture<K>`` input field, so no provider decodes base64
or re-checks a digest itself.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any, NamedTuple

from .canonical import canonical_json

__all__ = [
    "ACQUISITION_RESULT_TAG",
    "CaptureInput",
    "CaptureInputError",
    "capture_input",
    "capture_observed_at",
    "external_capture_result",
    "external_capture_results",
]

ACQUISITION_RESULT_TAG = "playbill-provider-result-to-external-capture-v1"


def external_capture_result(
    content: bytes,
    *,
    source_identity: str,
    coordinate_type: str,
    coordinate: object,
    observed_at: datetime,
    selector_type: str,
    selector: object = None,
) -> dict[str, Any]:
    """Build the Capture wire for ``content``; Core commits exactly these bytes.

    ``content`` is the payload itself, verbatim: the body an origin sent, the
    file read, the document a conversion produced -- in whatever format it is
    (JSON, CSV, gzip, HTML, binary). Never wrap it: how the payload was obtained
    (URLs, status, media type, request digest, kept headers, pages, the source a
    derived document cites) belongs in ``coordinate``, which the operation's out
    Contract types. Core never parses the bytes when it admits them; a
    Procedure reads them only through a declared ``extract`` under its
    CaptureKind's decoding. Source identity and coordinate/selector names must
    match the CaptureKind.

    External observations are attested-only: retaining a response does not make
    a live endpoint replayable. This helper is wire construction, not authority.

    ``source_effective_time`` is omitted: this helper states no validity window,
    and an optional field is an absent key, never a ``null``.
    """
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("observed_at must be timezone-aware")
    return {
        "tag": ACQUISITION_RESULT_TAG,
        "source_identity": source_identity,
        "coordinate_type": coordinate_type,
        "coordinate": coordinate,
        "selector_type": selector_type,
        "selector": {} if selector is None else selector,
        "replayability": "attested_only",
        "content_encoding": "base64",
        "content_base64": base64.b64encode(content).decode("ascii"),
        "byte_length": len(content),
        "bytes_digest": "sha256:" + hashlib.sha256(content).hexdigest(),
        "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
    }


def external_capture_results(
    members: Iterable[tuple[bytes, object]],
    *,
    source_identity: str,
    coordinate_type: str,
    observed_at: datetime,
    selector_type: str,
    selector: object = None,
) -> list[dict[str, Any]]:
    """The Capture wire for each member of a ``list<capture<...>>`` out field.

    ``members`` is ``(content, coordinate)`` per document, in the order the
    operation returns them. Each member is one document, verbatim (one origin
    response, one file): the list is never one payload split up, and no member
    wraps another. Core commits every member as its own Capture and identifies
    it by its coordinate (its request), never by its index, so two members with
    the same coordinate are refused here rather than left for Core to read as a
    duplicate request.
    """

    results: list[dict[str, Any]] = []
    seen: set[bytes] = set()
    for index, (content, coordinate) in enumerate(members):
        identity = canonical_json(coordinate)
        if identity in seen:
            raise ValueError(
                f"member {index} repeats an earlier member's coordinate; each member of a "
                "Capture list is one request with its own coordinate"
            )
        seen.add(identity)
        results.append(
            external_capture_result(
                content,
                source_identity=source_identity,
                coordinate_type=coordinate_type,
                coordinate=coordinate,
                observed_at=observed_at,
                selector_type=selector_type,
                selector=selector,
            )
        )
    return results


class CaptureInputError(ValueError):
    """A ``capture<K>`` input that is not the Capture wire Core sends.

    Core builds that wire from a Capture it committed, so this is a broken
    transport, not a document the operation declines to read. A provider still
    answers it with one of its own declared refusals: the runtime's codes are not
    a provider's to return.
    """


class CaptureInput(NamedTuple):
    """One Capture a derive reads: its exact bytes and how they were obtained."""

    content: bytes
    coordinate: Any
    coordinate_type: str


def capture_input(view: object) -> CaptureInput:
    """The bytes, coordinate and coordinate type of one ``capture<K>`` input.

    Core passes each Capture a derive names as the same wire a provider returns
    (``ProviderResultToExternalCapture``): base64 transport of the exact stored
    bytes, their length and digest, and the Capture's coordinate. This reads it
    and checks that the bytes reproduce the length and digest Core stated, so a
    derive computes over exactly the Capture it cites and never decodes the
    transport itself. Raises :class:`CaptureInputError` on anything else.
    """

    if not isinstance(view, Mapping):
        raise CaptureInputError("a capture input is Capture wire, a record")
    if view.get("tag", ACQUISITION_RESULT_TAG) != ACQUISITION_RESULT_TAG:
        raise CaptureInputError("a capture input carries an unknown wire tag")
    if view.get("content_encoding", "base64") != "base64":
        raise CaptureInputError("a capture input's content is base64 transport")
    encoded = view.get("content_base64")
    if not isinstance(encoded, str):
        raise CaptureInputError("a capture input carries no content")
    try:
        content = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise CaptureInputError("a capture input's content is not base64") from exc
    if base64.b64encode(content).decode("ascii") != encoded:
        raise CaptureInputError("a capture input's content is not canonical base64")
    length = view.get("byte_length")
    if not isinstance(length, int) or isinstance(length, bool) or length != len(content):
        raise CaptureInputError("a capture input's bytes do not reproduce its byte_length")
    if view.get("bytes_digest") != "sha256:" + hashlib.sha256(content).hexdigest():
        raise CaptureInputError("a capture input's bytes do not reproduce its bytes_digest")
    coordinate_type = view.get("coordinate_type")
    if not isinstance(coordinate_type, str) or not coordinate_type:
        raise CaptureInputError("a capture input names no coordinate type")
    if "coordinate" not in view:
        raise CaptureInputError("a capture input carries no coordinate")
    return CaptureInput(content, view["coordinate"], coordinate_type)


def capture_observed_at(view: object) -> datetime:
    """When the Capture behind one ``capture<K>`` input was observed.

    A derive is pure: identical inputs must give byte-identical output, the whole
    wire included, so that Core takes it as the same Capture and its Claims
    re-observe. A derive therefore stamps its result with its input's observation
    time, never with the clock.
    """

    if not isinstance(view, Mapping) or not isinstance(view.get("observed_at"), str):
        raise CaptureInputError("a capture input carries no observation time")
    try:
        moment = datetime.fromisoformat(str(view["observed_at"]))
    except ValueError as exc:
        raise CaptureInputError("a capture input's observation time does not parse") from exc
    if moment.utcoffset() is None:
        raise CaptureInputError("a capture input's observation time has no offset")
    return moment
