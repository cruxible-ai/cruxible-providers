"""``web.render`` -- a derive: one fetched Capture read into text or JSON.

A fetched Capture's bytes are what an origin sent. A reading of them is a
different document, produced here as a derived Capture (D-R37, M5). The
operation is a **derive**: it declares ``effect: pure``, computes only over the
exact bytes of the Capture core hands it (``source: capture<*>``, a Capture this
run fetched), contacts nothing, reads no clock, and gives byte-identical output
for identical input, so a re-run makes the same Capture and its Claims
re-observe rather than revise.

``form: text``
    Main-content extraction over the cited bytes, emitted as Markdown
    (``text/markdown``).
``form: json``
    The same document as canonical JSON (``application/json``): sorted keys, no
    insignificant whitespace, UTF-8::

        {"final_url", "links": [...], "text", "title"?, "url"}

    ``url`` / ``final_url`` are the cited Capture's; ``title`` is the document's
    ``<title>`` (absent when it has none); ``text`` is its main content as plain
    text (``""`` when none is found); ``links`` are the http(s) targets of its
    ``<a href>`` elements, resolved against ``<base href>`` or ``final_url``,
    fragment dropped, first occurrence kept, in document order.

The derived Capture's coordinate is the cited document's HTTP coordinate -- its
URL, final URL, status and request digest -- with the derived bytes' media type,
the engine that made them (``renderer``) and the cited bytes' content digest
(``derived_from``). Its observation time is the cited Capture's, never the
clock. A browser-assembled DOM is not a derive: it fetches what the page pulls,
and is ``web.render.dom``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urldefrag, urljoin, urlsplit

from cruxible_provider_runtime.acquisition import (
    CaptureInputError,
    capture_input,
    capture_observed_at,
    external_capture_result,
)
from cruxible_provider_runtime.canonical import canonical_json
from cruxible_provider_runtime.errors import Declined, decline
from cruxible_provider_runtime.provider_api import ProviderResult, ProviderRunContext

from .coordinate import COORDINATE_TYPE, SELECTOR_TYPE, content_digest, http_coordinate
from .engines import HtmlExtractor, TrafilaturaExtractor
from .fetch import DEFAULT_MAX_BYTES, MAX_RESPONSE_BYTES, page_weight_class
from .operations import PROVIDER_DECLINED, RENDER_OPERATION

__all__ = ["FORMS", "WebRender", "document_json"]

FORMS = ("json", "text")
HTML_MEDIA = frozenset({"text/html", "application/xhtml+xml"})
TEXT_MEDIA = "text/markdown; charset=utf-8"
JSON_MEDIA = "application/json"


class WebRender:
    """Derive one document from a fetched Capture. Pure."""

    operations = (RENDER_OPERATION,)

    def __init__(self, *, extractor: HtmlExtractor | None = None) -> None:
        # The default is the production spelling; the seam lets a test hold the
        # extractor still.
        self._extractor = extractor or TrafilaturaExtractor()

    def __call__(self, context: ProviderRunContext) -> ProviderResult:
        try:
            request = _RenderRequest.parse(context)
            html = request.html()
            if request.form == "json":
                derived, media, engine = self._json(request, html)
            else:
                extraction = self._extractor.extract(html, url=request.final_url)
                derived, media, engine = (
                    extraction.text.encode("utf-8"),
                    TEXT_MEDIA,
                    extraction.engine,
                )
        except Declined as exc:
            return ProviderResult(status="refused", refusal=exc.refusal)

        if len(derived) > request.max_bytes:
            return ProviderResult.refused(
                PROVIDER_DECLINED,
                "the derived document is heavier than the max_bytes this run declared",
                url=request.final_url,
                cap_bytes=request.max_bytes,
                observed_weight=page_weight_class(len(derived)),
            )
        cited = request.coordinate
        wire = external_capture_result(
            derived,
            source_identity=request.logical_source,
            coordinate_type=COORDINATE_TYPE,
            selector_type=SELECTOR_TYPE,
            coordinate=http_coordinate(
                url=cited["url"],
                final_url=request.final_url,
                status=cited["status"],
                content_type=media,
                response_headers={},
                from_recording=cited["source"] == "packaged-recording",
                renderer=engine,
                derived_from=content_digest(request.document),
            ),
            observed_at=request.observed_at,
        )
        return ProviderResult.ok(
            {"capture": wire},
            metrics={"byte_count": float(len(derived))},
        )

    def _json(self, request: _RenderRequest, html: str) -> tuple[bytes, str, str]:
        extraction = self._extractor.extract(
            html, url=request.final_url, output="text", require_content=False
        )
        document = document_json(
            html,
            url=str(request.coordinate["url"]),
            final_url=request.final_url,
            text=extraction.text,
        )
        return document, JSON_MEDIA, extraction.engine


def document_json(html: str, *, url: str, final_url: str, text: str) -> bytes:
    """The ``form: json`` document: canonical JSON of one HTML page's reading."""

    parsed = _Outline(final_url)
    parsed.feed(html)
    parsed.close()
    document: dict[str, Any] = {
        "url": url,
        "final_url": final_url,
        "text": text,
        "links": parsed.links(),
    }
    title = " ".join("".join(parsed.title).split())
    if title:
        document["title"] = title
    return canonical_json(document)


class _Outline(HTMLParser):
    """The ``<title>`` and the ``<a href>`` targets of one document, in order."""

    def __init__(self, final_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self._base = final_url
        self._based = False
        self._hrefs: list[str] = []
        self._in_title = False
        self._titled = False
        self.title: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        href = next((value for name, value in attrs if name == "href"), None)
        if tag == "base" and href is not None and not self._based:
            # The first <base href> resolves every relative reference after it.
            self._base = urljoin(self._base, href.strip())
            self._based = True
        elif tag == "a" and href is not None:
            self._hrefs.append(urljoin(self._base, href.strip()))
        elif tag == "title" and not self._titled:
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "title" and self._in_title:
            self._in_title = False
            self._titled = True

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title.append(data)

    def links(self) -> list[str]:
        seen: set[str] = set()
        links: list[str] = []
        for href in self._hrefs:
            target = urldefrag(href).url
            if urlsplit(target).scheme in {"http", "https"} and target not in seen:
                seen.add(target)
                links.append(target)
        return links


class _RenderRequest:
    """The validated run input."""

    __slots__ = ("coordinate", "document", "form", "logical_source", "max_bytes", "observed_at")

    def __init__(
        self,
        *,
        document: bytes,
        coordinate: Mapping[str, Any],
        form: str,
        logical_source: str,
        max_bytes: int,
        observed_at: Any,
    ) -> None:
        self.document = document
        self.coordinate = dict(coordinate)
        self.form = form
        self.logical_source = logical_source
        self.max_bytes = max_bytes
        self.observed_at = observed_at

    @property
    def final_url(self) -> str:
        return str(self.coordinate["final_url"])

    def html(self) -> str:
        """The cited bytes as the HTML text both forms read."""

        media = str(self.coordinate["content_type"]).partition(";")[0].strip().lower()
        if media not in HTML_MEDIA:
            raise decline(
                PROVIDER_DECLINED,
                f"web.render reads an HTML document, and the cited one is {media or 'untyped'}",
                url=self.final_url,
            )
        try:
            return self.document.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise decline(
                PROVIDER_DECLINED,
                "the cited document is not UTF-8 text",
                url=self.final_url,
            ) from exc

    @classmethod
    def parse(cls, context: ProviderRunContext) -> _RenderRequest:
        payload = context.input
        form = payload.get("form")
        if form not in FORMS:
            raise decline(PROVIDER_DECLINED, "form must be json or text")
        logical_source = payload.get("logical_source", "web.rendered")
        if not isinstance(logical_source, str) or not re.fullmatch(
            r"[a-z][a-z0-9_.-]{0,255}", logical_source
        ):
            raise decline(PROVIDER_DECLINED, "logical_source must be a logical name")
        max_bytes = payload.get("max_bytes", DEFAULT_MAX_BYTES)
        if (
            not isinstance(max_bytes, int)
            or isinstance(max_bytes, bool)
            or not 0 < max_bytes <= MAX_RESPONSE_BYTES
        ):
            raise decline(PROVIDER_DECLINED, "max_bytes must be between 1 and 33554432")
        source = payload.get("source")
        try:
            document, coordinate, coordinate_type = capture_input(source)
            observed_at = capture_observed_at(source)
        except CaptureInputError as exc:
            raise decline(
                PROVIDER_DECLINED, f"web.render needs the fetched Capture as source: {exc}"
            ) from exc
        if coordinate_type != COORDINATE_TYPE or not isinstance(coordinate, Mapping):
            raise decline(PROVIDER_DECLINED, "the source is not an HTTP Capture")
        if not all(
            isinstance(coordinate.get(name), str) for name in ("url", "final_url", "content_type")
        ):
            raise decline(PROVIDER_DECLINED, "the source coordinate is not an HTTP coordinate")
        if not isinstance(coordinate.get("status"), int) or coordinate.get("source") not in {
            "network",
            "packaged-recording",
        }:
            raise decline(PROVIDER_DECLINED, "the source coordinate is not an HTTP coordinate")
        if not document:
            raise decline(PROVIDER_DECLINED, "the source carries no bytes to render")
        return cls(
            document=document,
            coordinate=coordinate,
            form=form,
            logical_source=logical_source,
            max_bytes=max_bytes,
            observed_at=observed_at,
        )
