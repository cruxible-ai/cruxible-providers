"""Resolving the run input's document, and describing it honestly.

Both implementations take the same ``source`` shape and both answer the same two
questions about it: what bytes are we converting, and where did they come from.
The second question is not bookkeeping — it decides whether a real engine runs or
a recorded response is replayed, and it is the field a reader of the Capture
needs in order to know which they are looking at.
"""

from __future__ import annotations

import hashlib
import posixpath
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from cruxible_provider_runtime.errors import RefusalCode, refuse

from .recordings import Recording, inline_document, load_document, load_recordings

__all__ = ["DEFAULT_SUFFIX", "ResolvedSource", "document_suffix", "resolve_source"]

_SAFE_SUFFIX = re.compile(r"^\.[A-Za-z0-9]{1,12}$")

DEFAULT_SUFFIX = ".bin"
"""What a filename with no usable extension contributes to a temporary name."""


def _validate_filename(filename: str) -> str:
    """Refuse a declared filename that is anything other than a plain name.

    The filename is caller-supplied metadata, and an OCR engine needs the bytes
    on disk before it can read them, so the name reaches a path join.
    ``Path(directory) / name`` discards the directory entirely when ``name`` is
    absolute and walks out of it when ``name`` contains ``..`` — which on the
    local backend, a dependency-isolation mechanism and not a security boundary,
    means a caller choosing where the operator's own process writes a file.

    A document's name has no legitimate reason to carry a separator, so this
    refuses one rather than sanitising it. A sanitiser answers a different
    request than the one that was made, and does it quietly.

    ``:`` is refused with the separators, for the case that carries none:
    ``C:evil.png`` is drive-relative on Windows and names somewhere other than
    the directory it was joined to.
    """

    if filename in {".", ".."} or filename.startswith("."):
        raise refuse(
            RefusalCode.PROVIDER_DECLINED,
            "a document filename must be a plain name, not a relative path",
            filename=filename,
        )
    if posixpath.isabs(filename) or any(part in filename for part in ("/", "\\", ":", "\x00")):
        raise refuse(
            RefusalCode.PROVIDER_DECLINED,
            "a document filename must carry no path separator, drive letter, or absolute path",
            filename=filename,
        )
    return filename


def document_suffix(filename: str) -> str:
    """The only part of a caller's filename a temporary file may inherit.

    An engine that dispatches on extension needs the extension; it does not need
    the caller's name for the file. So the temporary name is generated
    internally and this supplies a suffix that has been checked to be one —
    exactly one dot, alphanumeric, short — falling back to a fixed suffix rather
    than passing through anything that failed the check.
    """

    suffix = posixpath.splitext(filename)[1]
    return suffix if _SAFE_SUFFIX.match(suffix) else DEFAULT_SUFFIX


@dataclass(frozen=True)
class ResolvedSource:
    data: bytes
    filename: str
    media_type: str
    origin: str
    recording: Recording | None

    @property
    def sha256(self) -> str:
        return "sha256:" + hashlib.sha256(self.data).hexdigest()

    def describe(self, *, declared_pages: int) -> dict[str, Any]:
        return {
            "filename": self.filename,
            "media_type": self.media_type,
            "byte_count": len(self.data),
            "sha256": self.sha256,
            "origin": self.origin,
            "declared_page_count": declared_pages,
        }


def _check_declared(source: Mapping[str, Any], filename: str, media_type: str) -> None:
    declared_name = source.get("filename")
    declared_type = source.get("media_type")
    if declared_name is not None and declared_name != filename:
        raise refuse(
            RefusalCode.PROVIDER_DECLINED,
            "the run declares a different filename than the packaged document carries",
            declared=declared_name,
            packaged=filename,
        )
    if declared_type is not None and declared_type != media_type:
        raise refuse(
            RefusalCode.PROVIDER_DECLINED,
            "the run declares a different media type than the packaged document carries",
            declared=declared_type,
            packaged=media_type,
        )


def resolve_source(payload: Mapping[str, Any], *, interface_id: str) -> ResolvedSource:
    """Turn the run input's ``source`` into bytes, or refuse.

    ``inline`` is the production shape: the caller supplies the document, and
    only a real engine ever converts it. ``packaged_fixture`` can name nothing
    but a document shipped inside this distribution, which is what keeps a
    recorded response from ever standing in for a caller's document.
    """

    source = payload.get("source")
    if not isinstance(source, Mapping):
        raise refuse(RefusalCode.PROVIDER_DECLINED, "the run input carries no source")
    kind = source.get("kind")

    if kind == "packaged_fixture":
        identifier = source.get("id")
        if not isinstance(identifier, str) or identifier not in load_recordings():
            raise refuse(
                RefusalCode.PROVIDER_DECLINED,
                "no packaged fixture carries that id",
                fixture_id=identifier if isinstance(identifier, str) else None,
                known=sorted(load_recordings()),
            )
        recording, data = load_document(identifier)
        if recording.interface_id != interface_id:
            raise refuse(
                RefusalCode.PROVIDER_DECLINED,
                "the packaged fixture was recorded for a different interface",
                fixture_id=identifier,
                recorded_for=recording.interface_id,
                requested=interface_id,
            )
        # A packaged source still declares its filename and media type, because
        # that is what the bucket classifier reads: admission happens before any
        # file is opened, and a classifier that reached into this distribution's
        # data files would be classifying from something the run did not say.
        # Declared, then checked — a declaration that disagrees with the
        # packaged document refuses rather than being quietly corrected.
        _check_declared(source, recording.document.filename, recording.document.media_type)
        return ResolvedSource(
            data=data,
            filename=recording.document.filename,
            media_type=recording.document.media_type,
            origin="packaged-recording",
            recording=recording,
        )

    if kind != "inline":
        raise refuse(
            RefusalCode.PROVIDER_DECLINED,
            f"source kind {kind!r} is not one this adapter understands",
            known=["inline", "packaged_fixture"],
        )

    content = source.get("content_base64")
    if not isinstance(content, str) or not content:
        raise refuse(RefusalCode.PROVIDER_DECLINED, "an inline source needs content_base64")
    try:
        data = inline_document(content)
    except Exception as exc:
        raise refuse(RefusalCode.PROVIDER_DECLINED, "content_base64 is not valid base64") from exc
    filename = source.get("filename")
    media_type = source.get("media_type")
    if not isinstance(filename, str) or not filename:
        raise refuse(RefusalCode.PROVIDER_DECLINED, "an inline source needs a filename")
    return ResolvedSource(
        data=data,
        filename=_validate_filename(filename),
        media_type=media_type if isinstance(media_type, str) else "",
        origin="inline",
        recording=None,
    )
