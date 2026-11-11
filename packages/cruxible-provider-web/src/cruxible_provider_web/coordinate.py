"""The HTTP coordinate every Capture this plane produces carries.

A Capture's bytes are the document an operation returns, verbatim: the body an
origin sent for ``web.fetch`` and ``search.web``, the DOM a browser assembled
for ``web.render.dom``, the derived document for ``web.render``. How that
document was obtained is metadata, and it travels here, in the typed coordinate
(``web.capture.coordinate@1``), never inside the bytes:

``url`` / ``final_url``
    The URL the request named and the URL the exchange settled on after its
    redirect chain. Core's acquisition origin gate reads both.
``status`` / ``content_type``
    The 2xx status the origin answered with and the media type of the bytes.
``request_digest``
    ``sha256`` of the request URL (for ``web.fetch``, ``url`` with its typed
    ``query`` encoded on), so two Captures of one request group.
``headers``
    The response headers kept for freshness (``etag``, ``last-modified``,
    ``content-length``); absent when the origin sent none of them.
``source``
    ``network``, or ``packaged-recording`` when a recording shipped in this
    distribution served the exchange and no origin was contacted.
``renderer``
    The engine that made the bytes when they are not what an origin sent: the
    extractor of a derived document (``web.render``), or the browser that
    assembled a page (``web.render.dom``).
``derived_from``
    Present only on a derived document (``web.render``): the content digest of
    the Capture it was made from (M5).
``pages``
    For an operation that assembles the pages of one listing into one document
    (each page's url, status and body digest). No operation in this plane
    assembles pages; the field is declared so the HTTP coordinate has one shape.
``page_index`` / ``page_count``
    Only on a member of ``web.fetch.pages``'s ``captures`` list: which of the
    run's page requests this one is (0-based) and how many there were. Each page
    is its own request, so its ``url`` is that page's built URL and its
    ``request_digest`` covers it. A list member's coordinate is not typed by an
    operation Contract (the member is ``capture<*>``); the CaptureKind the kit
    binds governs it, so these two keys extend the shape of
    ``web.capture.coordinate@1`` without moving that Contract's digest.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

__all__ = [
    "COORDINATE_TYPE",
    "KEPT_HEADERS",
    "SELECTOR_TYPE",
    "content_digest",
    "http_coordinate",
]

COORDINATE_TYPE = "http-response-v1"
"""Core's coordinate schema name for an HTTP-shaped Capture."""

SELECTOR_TYPE = "whole-response-v1"
"""The whole document: a Capture of this plane selects no part of it."""

KEPT_HEADERS = {
    "etag": "etag",
    "last-modified": "last_modified",
    "content-length": "content_length",
}
"""Response header -> coordinate field. Everything else the origin sent is dropped."""


def content_digest(payload: bytes) -> str:
    """``sha256:<hex>`` of ``payload``: a Capture's content identity."""

    return "sha256:" + hashlib.sha256(payload).hexdigest()


def http_coordinate(
    *,
    url: str,
    final_url: str,
    status: int,
    content_type: str,
    response_headers: Mapping[str, str],
    from_recording: bool,
    renderer: str | None = None,
    derived_from: str | None = None,
    page_index: int | None = None,
    page_count: int | None = None,
) -> dict[str, Any]:
    """The coordinate of one exchange; optional fields are absent keys, never ``null``."""

    coordinate: dict[str, Any] = {
        "url": url,
        "final_url": final_url,
        "status": status,
        "content_type": content_type,
        "request_digest": content_digest(url.encode("utf-8")),
        "source": "packaged-recording" if from_recording else "network",
    }
    kept = {
        field: response_headers[header]
        for header, field in KEPT_HEADERS.items()
        if header in response_headers
    }
    if kept:
        coordinate["headers"] = kept
    if renderer is not None:
        coordinate["renderer"] = renderer
    if derived_from is not None:
        coordinate["derived_from"] = derived_from
    if (page_index is None) != (page_count is None):
        raise ValueError("a page coordinate names both its index and the page count")
    if page_index is not None and page_count is not None:
        coordinate["page_index"] = page_index
        coordinate["page_count"] = page_count
    return coordinate
