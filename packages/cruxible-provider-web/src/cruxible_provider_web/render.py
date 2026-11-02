"""``web.render`` — a derived form of a fetched Capture, itself a Capture citing it.

A fetched Capture's bytes are what an origin sent. A reading of them — the DOM a
browser assembles from them, the main content an extractor finds in them — is a
different document, and it is produced here, by a separate operation, as a
derived Capture (M5):

``form: dom``
    The browser assembles the cited document: its main frame is served from the
    cited bytes, never fetched again; the subresources the page pulls are fetched
    live, and every origin they contact is recorded. The bytes are the serialized
    DOM (``text/html``).
``form: text``
    Main-content extraction over the cited bytes, emitted as Markdown
    (``text/markdown``). It contacts nothing.

The derived Capture's coordinate is the cited document's HTTP coordinate — its
URL, final URL, status and request digest — with the derived bytes' media type,
the engine that made them (``renderer``) and the cited bytes' content digest
(``derived_from``). The fetched Capture's metadata never enters the derived
bytes, and the derived bytes never enter the fetched Capture.

The input carries the cited bytes as base64 transport beside their coordinate;
the content digest is computed here from those bytes, so a render can only cite
what it actually read.
"""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from cruxible_provider_runtime.acquisition import external_capture_result
from cruxible_provider_runtime.egress import EgressRecorder
from cruxible_provider_runtime.errors import Declined, decline
from cruxible_provider_runtime.provider_api import ProviderResult, ProviderRunContext

from .coordinate import COORDINATE_TYPE, SELECTOR_TYPE, content_digest, http_coordinate
from .engines import (
    HtmlExtractor,
    PageRenderer,
    PlaywrightRenderer,
    RenderedPage,
    TrafilaturaExtractor,
)
from .fetch import DEFAULT_MAX_BYTES, MAX_RESPONSE_BYTES, page_weight_class
from .operations import PROVIDER_DECLINED, RENDER_OPERATION
from .recordings import is_fixture_url, recording_for

__all__ = ["FORMS", "WebRender"]

FORMS = ("dom", "text")
HTML_MEDIA = frozenset({"text/html", "application/xhtml+xml"})
DOM_MEDIA = "text/html; charset=utf-8"
TEXT_MEDIA = "text/markdown; charset=utf-8"


class _RecordedRenderer:
    """Replays the post-assembly DOM a packaged recording captured.

    A recorded render is not a render, and the label says so: the engine name it
    reports is ``recorded:<id>``, never ``playwright``. It replays only for the
    document the recording was made from: cited bytes that differ from the
    recorded response are declined, so a recording can never stand in for a
    rendering of some other document. It contacts nothing, so it records nothing.
    """

    name = "recorded"

    def render(
        self,
        url: str,
        *,
        document: bytes,
        content_type: str,
        timeout_seconds: float,
        recorder: EgressRecorder,
    ) -> RenderedPage:
        del content_type, timeout_seconds, recorder
        recording = recording_for(url)
        if recording is None or recording.rendered_body is None:
            raise decline(
                PROVIDER_DECLINED,
                f"no packaged recording carries a rendered document for {url!r}",
                url=url,
            )
        if document != recording.response.body.encode("utf-8"):
            raise decline(
                PROVIDER_DECLINED,
                "the cited document is not the one the packaged recording rendered",
                url=url,
            )
        return RenderedPage(
            engine=f"recorded:{recording.id}",
            html=recording.rendered_body,
            final_url=url,
            status_code=200,
            headers={"content-type": DOM_MEDIA},
            body=document,
        )


class WebRender:
    """Derive one document from a fetched Capture."""

    operations = (RENDER_OPERATION,)

    def __init__(
        self,
        *,
        extractor: HtmlExtractor | None = None,
        renderer: PageRenderer | None = None,
    ) -> None:
        # Every default is the production spelling; the seams let a test hold
        # one variable still.
        self._extractor = extractor or TrafilaturaExtractor()
        self._renderer = renderer

    def __call__(self, context: ProviderRunContext) -> ProviderResult:
        try:
            request = _RenderRequest.parse(context)
            if request.form == "dom":
                derived, media, engine = self._dom(context, request)
            else:
                derived, media, engine = self._text(request)
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
        replayed = engine.startswith("recorded:") or cited["source"] == "packaged-recording"
        return ProviderResult.ok(
            external_capture_result(
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
                    from_recording=replayed,
                    renderer=engine,
                    derived_from=content_digest(request.document),
                ),
                observed_at=datetime.now(UTC),
            ),
            metrics={"byte_count": float(len(derived))},
            events=(
                [{"kind": "recorded_render", "url": request.final_url}]
                if engine.startswith("recorded:")
                else []
            ),
        )

    def _dom(self, context: ProviderRunContext, request: _RenderRequest) -> tuple[bytes, str, str]:
        renderer = self._renderer or (
            _RecordedRenderer() if is_fixture_url(request.final_url) else PlaywrightRenderer()
        )
        page = renderer.render(
            request.final_url,
            document=request.document,
            content_type=request.content_type,
            timeout_seconds=max(1.0, context.budgets.wall_clock_seconds * 0.8),
            recorder=context.egress,
        )
        return page.html.encode("utf-8"), DOM_MEDIA, page.engine

    def _text(self, request: _RenderRequest) -> tuple[bytes, str, str]:
        media = request.content_type.partition(";")[0].strip().lower()
        if media not in HTML_MEDIA:
            raise decline(
                PROVIDER_DECLINED,
                f"a text render reads an HTML document, and the cited one is {media or 'untyped'}",
                url=request.final_url,
            )
        try:
            html = request.document.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise decline(
                PROVIDER_DECLINED,
                "the cited document is not UTF-8 text",
                url=request.final_url,
            ) from exc
        extraction = self._extractor.extract(html, url=request.final_url)
        return extraction.text.encode("utf-8"), TEXT_MEDIA, extraction.engine


class _RenderRequest:
    """The validated run input."""

    __slots__ = ("coordinate", "document", "form", "logical_source", "max_bytes")

    def __init__(
        self,
        *,
        document: bytes,
        coordinate: Mapping[str, Any],
        form: str,
        logical_source: str,
        max_bytes: int,
    ) -> None:
        self.document = document
        self.coordinate = dict(coordinate)
        self.form = form
        self.logical_source = logical_source
        self.max_bytes = max_bytes

    @property
    def final_url(self) -> str:
        return str(self.coordinate["final_url"])

    @property
    def content_type(self) -> str:
        return str(self.coordinate["content_type"])

    @classmethod
    def parse(cls, context: ProviderRunContext) -> _RenderRequest:
        payload = context.input
        form = payload.get("form")
        if form not in FORMS:
            raise decline(PROVIDER_DECLINED, "form must be dom or text")
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
        if not isinstance(source, Mapping):
            raise decline(PROVIDER_DECLINED, "web.render needs the fetched Capture as source")
        coordinate = source.get("coordinate")
        if not isinstance(coordinate, Mapping) or not all(
            isinstance(coordinate.get(name), str) for name in ("url", "final_url", "content_type")
        ):
            raise decline(PROVIDER_DECLINED, "the source coordinate is not an HTTP coordinate")
        if not isinstance(coordinate.get("status"), int) or coordinate.get("source") not in {
            "network",
            "packaged-recording",
        }:
            raise decline(PROVIDER_DECLINED, "the source coordinate is not an HTTP coordinate")
        try:
            document = base64.b64decode(str(source.get("content_base64", "")), validate=True)
        except (ValueError, binascii.Error) as exc:
            raise decline(PROVIDER_DECLINED, "the source content is not base64") from exc
        if not document:
            raise decline(PROVIDER_DECLINED, "the source carries no bytes to render")
        return cls(
            document=document,
            coordinate=coordinate,
            form=form,
            logical_source=logical_source,
            max_bytes=max_bytes,
        )
