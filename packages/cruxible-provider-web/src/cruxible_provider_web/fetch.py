"""``web.fetch`` — retrieve one web resource, and optionally extract its content.

The output is deliberately split in two, and the split is the contract rather
than a formatting choice:

``retrieved``
    What came off the wire — the final URL, the status, the content type, the
    byte count, the digest of the body, and where it came from. This is the
    material a CaptureContract may grade as observed-shaped, because it is a
    record of an exchange that happened.

``derived``
    What an extractor made of it — Markdown, a title, an author, a date. This is
    derived under every contract, whatever the extractor's confidence, because
    it is a reading of the document rather than the document.

The adapter never mints a Capture. It returns a typed payload plus trace and the
executor carries both to the CaptureContract, which decides the grade. That
ordering is what keeps a provider from being able to certify itself.

**Egress.** This implementation cannot enumerate its endpoints: the resource is
named by the caller, which is the whole point of the interface. Its manifest
therefore declares the experimental ``dynamic:target-from-run-input`` form, and
what governs is the recording — every request the client issues reaches the run's
egress recorder through an httpx event hook, redirect hops included.

One reading note about that recording. A request to the reserved
``fixture.invalid`` host is served from a recording shipped in this distribution
rather than from a socket; it is still recorded, because the recorder's subject
is the request the adapter issued. Nobody can misread the receipt: ``.invalid``
resolves nowhere, and ``retrieved.source`` says ``packaged-recording`` in so many
words.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from cruxible_provider_runtime.errors import RefusalCode, RefusalError, refuse
from cruxible_provider_runtime.provider_api import ProviderResult, ProviderRunContext

from .engines import (
    Extraction,
    HtmlExtractor,
    PageRenderer,
    PlaywrightRenderer,
    RenderedPage,
    TrafilaturaExtractor,
)
from .http import ClientFactory, HttpResponse, ResponseTooLarge, default_client_factory
from .interfaces import DEFAULT_MAX_BYTES, FETCH_INTERFACE_ID, page_weight_class
from .recordings import is_fixture_url, recording_for

__all__ = ["WebFetch"]

RETAINED_HEADERS = ("content-type", "content-length", "last-modified", "etag")


class _RecordedRenderer:
    """Replays the post-assembly DOM a recording captured.

    A recorded render is not a render, and the label says so: the engine name it
    reports is ``recorded:<id>``, never ``playwright``. The engine-marked lane is
    what keeps the recording honest — it runs the real browser over the same URL
    and asserts the recording still describes what a browser produces.
    """

    name = "recorded"

    def render(self, url: str, *, timeout_seconds: float) -> RenderedPage:
        del timeout_seconds
        recording = recording_for(url)
        if recording is None or recording.rendered_body is None:
            raise refuse(
                RefusalCode.PROVIDER_DECLINED,
                f"no packaged recording carries a rendered document for {url!r}",
                url=url,
            )
        return RenderedPage(engine=f"recorded:{recording.id}", html=recording.rendered_body)


class WebFetch:
    """Retrieve a resource the run input names."""

    interface_id = FETCH_INTERFACE_ID

    def __init__(
        self,
        *,
        client_factory: ClientFactory | None = None,
        extractor: HtmlExtractor | None = None,
        renderer: PageRenderer | None = None,
    ) -> None:
        # Every default is the production spelling. The seams exist so a test can
        # hold one variable still, not so a test can replace the thing under
        # test: the conformance suite drives these same defaults.
        self._client_factory = client_factory or default_client_factory
        self._extractor = extractor or TrafilaturaExtractor()
        self._renderer = renderer

    def __call__(self, context: ProviderRunContext) -> ProviderResult:
        try:
            request = _Request.parse(context)
        except RefusalError as exc:
            return ProviderResult(status="refused", refusal=exc.refusal)

        timeout_seconds = max(1.0, context.budgets.wall_clock_seconds * 0.8)
        try:
            if request.render:
                retrieved, rendered = self._render(context, request, timeout_seconds)
            else:
                retrieved, rendered = self._get(context, request, timeout_seconds), None
        except RefusalError as exc:
            return ProviderResult(status="refused", refusal=exc.refusal)
        except ResponseTooLarge as exc:
            return ProviderResult.refused(
                RefusalCode.PROVIDER_DECLINED,
                "the origin sent more than the byte cap this run was admitted under",
                url=request.url,
                declared_bucket=context.input_bucket,
                cap_bytes=exc.cap_bytes,
                observed_weight=page_weight_class(exc.read_bytes),
            )

        if rendered is None and not 200 <= retrieved.status_code < 300:
            # A status outside 2xx is a failed attempt at an answer, not a
            # declined one: the interface's product is a retrieved resource, and
            # this run did not get one.
            return ProviderResult.failed(
                "HttpStatus",
                f"origin answered {retrieved.status_code}",
                url=request.url,
                status_code=retrieved.status_code,
            )

        body_text = rendered.html if rendered is not None else retrieved.text
        byte_count = len(body_text.encode("utf-8")) if rendered is not None else len(retrieved.body)
        if byte_count > request.max_bytes:
            return ProviderResult.refused(
                RefusalCode.PROVIDER_DECLINED,
                "the assembled document is heavier than the bucket this run was admitted under",
                url=request.url,
                declared_bucket=context.input_bucket,
                cap_bytes=request.max_bytes,
                observed_weight=page_weight_class(byte_count),
            )

        derived = self._derive(request, retrieved, body_text)
        source = (
            "packaged-recording"
            if retrieved.from_recording
            or (rendered is not None and rendered.engine.startswith("recorded:"))
            else "network"
        )
        events: list[dict[str, Any]] = []
        if source == "packaged-recording":
            events.append(
                {
                    "kind": "packaged_recording",
                    "url": request.url,
                    "note": "served from a recording shipped in this distribution; no origin "
                    "was contacted",
                }
            )
        return ProviderResult.ok(
            {
                "input_bucket": context.input_bucket,
                "retrieved": {
                    "url": request.url,
                    "final_url": retrieved.final_url,
                    "status_code": retrieved.status_code,
                    "headers": {
                        key: value
                        for key, value in retrieved.headers.items()
                        if key in RETAINED_HEADERS
                    },
                    "byte_count": byte_count,
                    "body_sha256": "sha256:"
                    + hashlib.sha256(body_text.encode("utf-8")).hexdigest(),
                    "source": source,
                    "renderer": rendered.engine if rendered is not None else None,
                },
                "derived": derived,
            },
            metrics={"byte_count": float(byte_count)},
            events=events,
        )

    # -- retrieval ---------------------------------------------------------

    def _get(
        self, context: ProviderRunContext, request: _Request, timeout_seconds: float
    ) -> HttpResponse:
        client = self._client_factory(
            context.egress, url=request.url, timeout_seconds=timeout_seconds
        )
        with client:
            return client.get(request.url, headers=request.headers, cap_bytes=request.max_bytes)

    def _render(
        self, context: ProviderRunContext, request: _Request, timeout_seconds: float
    ) -> tuple[HttpResponse, RenderedPage]:
        renderer = self._renderer or self._default_renderer(request.url)
        if renderer.name != "recorded":
            # A browser contacts the origin itself, outside the instrumented
            # client, so the request it is about to issue is recorded here.
            context.egress.record(request.url)
        page = renderer.render(request.url, timeout_seconds=timeout_seconds)
        placeholder = HttpResponse(
            status_code=200,
            headers={"content-type": "text/html; charset=utf-8"},
            body=page.html.encode("utf-8"),
            final_url=request.url,
            from_recording=None,
        )
        return placeholder, page

    @staticmethod
    def _default_renderer(url: str) -> PageRenderer:
        if is_fixture_url(url):
            return _RecordedRenderer()
        return PlaywrightRenderer()

    # -- derivation --------------------------------------------------------

    def _derive(self, request: _Request, retrieved: HttpResponse, body_text: str) -> dict[str, Any]:
        if not request.extract:
            return {"kind": "none", "engine": None, "text": None, "metadata": {}}
        if request.source_kind in {"api_json", "binary"}:
            # Extraction is for documents. A structured endpoint is carried
            # through verbatim rather than run through a main-content heuristic
            # that would find "main content" in a JSON array.
            return {
                "kind": "verbatim",
                "engine": None,
                "text": body_text,
                "metadata": {"content_type": retrieved.content_type},
            }
        extraction: Extraction = self._extractor.extract(body_text, url=request.url)
        return {
            "kind": extraction.kind,
            "engine": extraction.engine,
            "text": extraction.text,
            "metadata": extraction.metadata,
        }


class _Request:
    """The validated run input."""

    __slots__ = ("extract", "headers", "max_bytes", "render", "source_kind", "url")

    def __init__(
        self,
        *,
        url: str,
        render: bool,
        max_bytes: int,
        extract: bool,
        headers: Mapping[str, str],
        source_kind: str,
    ) -> None:
        self.url = url
        self.render = render
        self.max_bytes = max_bytes
        self.extract = extract
        self.headers = dict(headers)
        self.source_kind = source_kind

    @classmethod
    def parse(cls, context: ProviderRunContext) -> _Request:
        payload = context.input
        url = payload.get("url")
        if not isinstance(url, str) or urlsplit(url).scheme not in {"http", "https"}:
            raise refuse(
                RefusalCode.PROVIDER_DECLINED,
                "web.fetch needs an http or https url",
                url=url if isinstance(url, str) else None,
            )
        max_bytes = payload.get("max_bytes", DEFAULT_MAX_BYTES)
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0:
            raise refuse(RefusalCode.PROVIDER_DECLINED, "max_bytes must be a positive integer")

        headers: dict[str, str] = {}
        credential_ref = payload.get("credential_ref")
        if credential_ref is not None:
            if not isinstance(credential_ref, str):
                raise refuse(RefusalCode.PROVIDER_DECLINED, "credential_ref must be a string")
            material = context.secrets.get(credential_ref)
            if material is None:
                raise refuse(
                    RefusalCode.UNRESOLVED_SECRET_REF,
                    f"the run names credential {credential_ref!r} and it was not delivered",
                    ref=credential_ref,
                )
            header = payload.get("credential_header", "authorization")
            if not isinstance(header, str) or not header:
                raise refuse(RefusalCode.PROVIDER_DECLINED, "credential_header must be a string")
            headers[header] = material

        # The bucket the run was admitted into is the executor's classification
        # of this same input; reading the source kind back off it keeps the
        # adapter's behaviour and the recorded bucket from ever disagreeing.
        source_kind = dict(
            segment.split("=", 1) for segment in context.input_bucket.split(";")
        ).get("source_kind", "static_html")
        return cls(
            url=url,
            render=bool(payload.get("render", False)),
            max_bytes=max_bytes,
            extract=bool(payload.get("extract", True)),
            headers=headers,
            source_kind=source_kind,
        )
