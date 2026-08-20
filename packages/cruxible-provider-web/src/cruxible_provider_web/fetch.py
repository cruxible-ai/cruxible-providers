"""``web.fetch`` — retrieve one web resource, and optionally extract its content.

The output is deliberately split in two, and the split is the contract rather
than a formatting choice:

``retrieved``
    What came off the wire — the final URL, the status, the headers, the byte
    count and digest of the **body an origin sent**, and where it came from. This
    is the material a CaptureContract may grade as observed-shaped, because it is
    a record of an exchange that happened.

``derived``
    What an extractor made of it — Markdown, a title, an author, a date. This is
    derived under every contract, whatever the extractor's confidence, because
    it is a reading of the document rather than the document.

A rendered run is where that line is easiest to lose, so it is drawn twice. The
document a browser assembles is not a body any origin sent: it is script output
over one, and it is reported under ``derived.assembled_document`` — its own byte
count, its own digest, and the assembly that produced it. ``retrieved`` keeps
describing the main-frame response the browser actually received, down to the
status. A page that redirects across origins and settles on a 404 a script
repaints is a failed retrieval, and this adapter reports it as one rather than
as a 200 for the URL that was asked for.

The adapter never mints a Capture. It returns a typed payload plus trace and the
executor carries both to the CaptureContract, which decides the grade. That
ordering is what keeps a provider from being able to certify itself.

**Egress.** This implementation cannot enumerate its endpoints: the resource is
named by the caller, which is the whole point of the interface. Its manifest
therefore declares the experimental ``dynamic:target-from-run-input`` form, and
what governs is the recording — every request the client issues reaches the run's
egress recorder through an httpx event hook, redirect hops included. A rendered
run does not go through that client, so the browser gets its own hooks: every
origin a page contacts, subresources and redirect hops alike, lands in the same
recorder.

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
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from cruxible_provider_runtime.egress import EgressRecorder
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
from .http import ClientFactory, ResponseTooLarge, default_client_factory
from .interfaces import DEFAULT_MAX_BYTES, FETCH_INTERFACE_ID, page_weight_class
from .recordings import is_fixture_url, recording_for

__all__ = ["WebFetch"]

RETAINED_HEADERS = ("content-type", "content-length", "last-modified", "etag")

CLIENT_SIDE_RENDER = "client_side_render"
"""How a document was assembled, when a browser assembled it."""


@dataclass(frozen=True)
class _Exchange:
    """One retrieval: what the wire said, and what this run ended up reading.

    A plain GET reads the body an origin sent, so the two coincide and only the
    first half is populated. A rendered run reads a DOM a browser built, which no
    origin sent — so the wire half stays the main-frame response and the
    assembled document travels beside it. Holding both in one object is what lets
    the output say which is which instead of presenting the second as the first.
    """

    final_url: str
    status_code: int | None
    headers: Mapping[str, str]
    wire_body: bytes | None
    document: str
    from_recording: bool
    renderer: str | None

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "")

    @property
    def assembled(self) -> bool:
        return self.renderer is not None


class _RecordedRenderer:
    """Replays the post-assembly DOM a recording captured.

    A recorded render is not a render, and the label says so: the engine name it
    reports is ``recorded:<id>``, never ``playwright``. The engine-marked lane is
    what keeps the recording honest — it runs the real browser over the same URL
    and asserts the recording still describes what a browser produces.

    It contacts nothing, so it records nothing. The recorder it is handed goes
    unused rather than being given a hopeful entry for an origin that was never
    reached.
    """

    name = "recorded"

    def render(self, url: str, *, timeout_seconds: float, recorder: EgressRecorder) -> RenderedPage:
        del timeout_seconds, recorder
        recording = recording_for(url)
        if recording is None or recording.rendered_body is None:
            raise refuse(
                RefusalCode.PROVIDER_DECLINED,
                f"no packaged recording carries a rendered document for {url!r}",
                url=url,
            )
        return RenderedPage(
            engine=f"recorded:{recording.id}",
            html=recording.rendered_body,
            # The recorded exchange, not the recorded DOM: a replay reports the
            # response the recording captured, exactly as a live render reports
            # the one the browser received.
            final_url=recording.request_url,
            status_code=recording.response.status_code,
            headers={key.lower(): value for key, value in recording.response.headers.items()},
            body=recording.response.body.encode("utf-8"),
        )


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
                exchange = self._render(context, request, timeout_seconds)
            else:
                exchange = self._get(context, request, timeout_seconds)
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

        if exchange.status_code is not None and not 200 <= exchange.status_code < 300:
            # A status outside 2xx is a failed attempt at an answer, not a
            # declined one: the interface's product is a retrieved resource, and
            # this run did not get one. A rendered run is judged on the same rule
            # now that it reports the status a browser actually got — a script
            # that repaints a 404 into something readable has not turned it into
            # a retrieved resource.
            return ProviderResult.failed(
                "HttpStatus",
                f"origin answered {exchange.status_code}",
                url=request.url,
                final_url=exchange.final_url,
                status_code=exchange.status_code,
            )

        document = exchange.document.encode("utf-8")
        if len(document) > request.max_bytes:
            return ProviderResult.refused(
                RefusalCode.PROVIDER_DECLINED,
                "the assembled document is heavier than the bucket this run was admitted under",
                url=request.url,
                declared_bucket=context.input_bucket,
                cap_bytes=request.max_bytes,
                observed_weight=page_weight_class(len(document)),
            )

        derived = self._derive(request, exchange)
        source = "packaged-recording" if exchange.from_recording else "network"
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
                    "final_url": exchange.final_url,
                    "status_code": exchange.status_code,
                    "headers": {
                        key: value
                        for key, value in exchange.headers.items()
                        if key in RETAINED_HEADERS
                    },
                    # The body an origin sent, never the document this run read:
                    # on a rendered run those are two artefacts, and the second
                    # is reported under derived.assembled_document.
                    "byte_count": None if exchange.wire_body is None else len(exchange.wire_body),
                    "body_sha256": _digest(exchange.wire_body),
                    "source": source,
                    # Which client performed the exchange, in the sense a
                    # user-agent names one. What it built is derived.
                    "renderer": exchange.renderer,
                },
                "derived": derived,
            },
            metrics={"byte_count": float(len(document))},
            events=events,
        )

    # -- retrieval ---------------------------------------------------------

    def _get(
        self, context: ProviderRunContext, request: _Request, timeout_seconds: float
    ) -> _Exchange:
        client = self._client_factory(
            context.egress, url=request.url, timeout_seconds=timeout_seconds
        )
        with client:
            response = client.get(request.url, headers=request.headers, cap_bytes=request.max_bytes)
        return _Exchange(
            final_url=response.final_url,
            status_code=response.status_code,
            headers=response.headers,
            wire_body=response.body,
            document=response.text,
            from_recording=response.from_recording is not None,
            renderer=None,
        )

    def _render(
        self, context: ProviderRunContext, request: _Request, timeout_seconds: float
    ) -> _Exchange:
        renderer = self._renderer or self._default_renderer(request.url)
        # The recorder goes to the renderer rather than being written here: a
        # browser contacts the origin itself, outside the instrumented client,
        # and it contacts every host the page pulls from as well. Recording only
        # the URL the run named would leave the receipt describing the request
        # rather than the run.
        page = renderer.render(
            request.url, timeout_seconds=timeout_seconds, recorder=context.egress
        )
        return _Exchange(
            final_url=page.final_url,
            status_code=page.status_code,
            headers=page.headers,
            wire_body=page.body,
            document=page.html,
            from_recording=page.engine.startswith("recorded:"),
            renderer=page.engine,
        )

    @staticmethod
    def _default_renderer(url: str) -> PageRenderer:
        if is_fixture_url(url):
            return _RecordedRenderer()
        return PlaywrightRenderer()

    # -- derivation --------------------------------------------------------

    def _derive(self, request: _Request, exchange: _Exchange) -> dict[str, Any]:
        derived = self._extraction(request, exchange)
        if exchange.assembled:
            # Stated in the derived half, and stated completely enough to stand
            # on its own: a CaptureContract may grade these two blocks under
            # different families, and a block that has to be read next to
            # another one to be understood is a block that will be read alone.
            document = exchange.document.encode("utf-8")
            derived["assembled_document"] = {
                "assembly": CLIENT_SIDE_RENDER,
                "engine": exchange.renderer,
                "byte_count": len(document),
                "sha256": _digest(document),
            }
        return derived

    def _extraction(self, request: _Request, exchange: _Exchange) -> dict[str, Any]:
        if not request.extract:
            return {"kind": "none", "engine": None, "text": None, "metadata": {}}
        if request.source_kind in {"api_json", "binary"}:
            # Extraction is for documents. A structured endpoint is carried
            # through verbatim rather than run through a main-content heuristic
            # that would find "main content" in a JSON array.
            return {
                "kind": "verbatim",
                "engine": None,
                "text": exchange.document,
                "metadata": {"content_type": exchange.content_type},
            }
        extraction: Extraction = self._extractor.extract(exchange.document, url=request.url)
        return {
            "kind": extraction.kind,
            "engine": extraction.engine,
            "text": extraction.text,
            "metadata": extraction.metadata,
        }


def _digest(payload: bytes | None) -> str | None:
    """The digest of ``payload``, or ``None`` when there is nothing to digest."""

    if payload is None:
        return None
    return "sha256:" + hashlib.sha256(payload).hexdigest()


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
