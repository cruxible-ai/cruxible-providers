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

**No rendering.** This implementation does not run a browser, so a run asking
for ``render: true`` is refused with ``provider_declined``.
``retrieved.renderer`` is therefore always ``null``.

**What it declines.** Core runs no input classifier, so this adapter refuses
for itself every input it cannot serve, before contacting anything, with
``provider_declined`` and a ``reason`` in the refusal detail: a binary resource
(``expected_format: bytes``, or a URL whose suffix names a PDF, image, archive,
media or Office file: the document plane's work, not an HTML extractor's), and
an HTML page whose ``max_bytes`` cap is above the medium weight class (2 MiB).
Structured endpoints are served at any cap up to 32 MiB.

The adapter never mints a Capture. It returns a typed payload plus trace and the
executor carries both to the CaptureContract, which decides the grade. That
ordering is what keeps a provider from being able to certify itself.

**Egress.** This implementation cannot enumerate its endpoints: the resource is
named by the caller, which is the whole point of the interface. Its manifest
therefore declares the experimental ``dynamic:target-from-run-input`` form, and
what governs is the recording — every request the client issues reaches the run's
egress recorder through an httpx event hook, redirect hops included.

**Private targets.** ``web.fetch`` refuses, with ``provider_declined``, any
target whose addresses are loopback, private, link-local, or otherwise not
publicly routable — on the first request and on every redirect hop — and
connects only to the addresses it checked,
so a name cannot be re-pointed between the check and the connection. See
:mod:`cruxible_provider_web.addresses`. The guard always applies; there is no
opt-out.

One reading note about that recording. A request to the reserved
``fixture.invalid`` host is served from a recording shipped in this distribution
rather than from a socket; it is still recorded, because the recorder's subject
is the request the adapter issued. Nobody can misread the receipt: ``.invalid``
resolves nowhere, and ``retrieved.source`` says ``packaged-recording`` in so many
words.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from cruxible_provider_runtime.acquisition import external_capture_result
from cruxible_provider_runtime.canonical import canonical_json
from cruxible_provider_runtime.errors import RefusalCode, RefusalError, refuse
from cruxible_provider_runtime.provider_api import ProviderResult, ProviderRunContext

from .engines import Extraction, HtmlExtractor, TrafilaturaExtractor
from .http import ClientFactory, ResponseTooLarge, guarded_client_factory
from .interfaces import (
    DEFAULT_MAX_BYTES,
    FETCH_INTERFACE_ID,
    MAX_RESPONSE_BYTES,
    MEDIUM_CEILING_BYTES,
    classify_web_fetch,
    page_weight_class,
)

__all__ = ["WebFetch"]

RETAINED_HEADERS = ("content-type", "content-length", "last-modified", "etag")


@dataclass(frozen=True)
class _Exchange:
    """One retrieval: what the wire said, and the body the origin sent."""

    final_url: str
    status_code: int
    headers: Mapping[str, str]
    wire_body: bytes
    document: str
    from_recording: bool

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "")


class WebFetch:
    """Retrieve a resource the run input names."""

    interface_id = FETCH_INTERFACE_ID

    def __init__(
        self,
        *,
        client_factory: ClientFactory | None = None,
        extractor: HtmlExtractor | None = None,
    ) -> None:
        # Every default is the production spelling. The seams exist so a test can
        # hold one variable still, not so a test can replace the thing under
        # test: the conformance suite drives these same defaults.
        self._client_factory = client_factory or guarded_client_factory
        self._extractor = extractor or TrafilaturaExtractor()

    def __call__(self, context: ProviderRunContext) -> ProviderResult:
        try:
            request = _Request.parse(context)
        except RefusalError as exc:
            return ProviderResult(status="refused", refusal=exc.refusal)

        timeout_seconds = max(1.0, context.budgets.wall_clock_seconds * 0.8)
        try:
            exchange = self._get(context, request, timeout_seconds)
        except RefusalError as exc:
            return ProviderResult(status="refused", refusal=exc.refusal)
        except ResponseTooLarge as exc:
            return ProviderResult.refused(
                RefusalCode.PROVIDER_DECLINED,
                "the origin sent more than the byte cap this run was admitted under",
                url=request.url,
                cap_bytes=exc.cap_bytes,
                observed_weight=page_weight_class(exc.read_bytes),
            )

        if not 200 <= exchange.status_code < 300:
            # A status outside 2xx is a failed attempt at an answer, not a
            # declined one: the interface's product is a retrieved resource, and
            # this run did not get one.
            return ProviderResult.failed(
                "HttpStatus",
                f"origin answered {exchange.status_code}",
                url=request.url,
                final_url=exchange.final_url,
                status_code=exchange.status_code,
            )

        if len(exchange.wire_body) > request.max_bytes:
            return ProviderResult.refused(
                RefusalCode.PROVIDER_DECLINED, "origin body exceeds max_bytes"
            )
        try:
            request.validate_format(exchange)
            derived = self._derive(request, exchange)
        except (ValueError, UnicodeError, csv.Error) as exc:
            return ProviderResult.failed("ResponseFormat", str(exc))
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
        material = {
            # Required by the interface's pinned material contract, so it stays
            # until the definition succeeds; core now sends a placeholder here.
            "input_bucket": context.input_bucket,
            "retrieved": {
                "url": request.url,
                "final_url": exchange.final_url,
                "status_code": exchange.status_code,
                "headers": {
                    key: value for key, value in exchange.headers.items() if key in RETAINED_HEADERS
                },
                # The body an origin sent, exactly as it arrived.
                "byte_count": len(exchange.wire_body),
                "body_sha256": _digest(exchange.wire_body),
                "body_base64": base64.b64encode(exchange.wire_body).decode("ascii"),
                "source": source,
                # Which client performed the exchange, in the sense a
                # user-agent names one. What it built is derived.
                "renderer": None,
            },
            "derived": derived,
        }
        return ProviderResult.ok(
            external_capture_result(
                canonical_json(material),
                source_identity=request.logical_source,
                coordinate_type="http-response-v1",
                selector_type="whole-response-v1",
                coordinate={
                    "request_digest": _digest(request.url.encode()),
                    "status": exchange.status_code,
                    "body_sha256": _digest(exchange.wire_body),
                    "source": source,
                },
                observed_at=datetime.now(UTC),
            ),
            metrics={"byte_count": float(len(exchange.document.encode("utf-8")))},
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
        )

    # -- derivation --------------------------------------------------------

    def _derive(self, request: _Request, exchange: _Exchange) -> dict[str, Any]:
        if not request.extract:
            return {"kind": "none", "engine": None, "text": None, "metadata": {}}
        if request.expected_format != "html":
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


def _decline_unserved(
    payload: Mapping[str, Any], url: str, expected_format: str, max_bytes: int
) -> None:
    """Refuse the source kinds and weights this adapter does not serve.

    The kind is the package's own reading of the input (the same function its
    registration names), so what is refused here is exactly what the manifest
    leaves unclaimed: binary resources, and HTML pages heavier than the medium
    weight class. Called only after the input is otherwise valid.
    """

    measured = classify_web_fetch(payload)
    source_kind = "static_html" if measured is None else measured["source_kind"]
    if source_kind == "binary":
        raise refuse(
            RefusalCode.PROVIDER_DECLINED,
            "web.fetch does not fetch binary resources; a PDF, image, archive or Office "
            "file is the document plane's work",
            reason="binary_resource",
            url=url,
            expected_format=expected_format,
        )
    if source_kind == "static_html" and max_bytes > MEDIUM_CEILING_BYTES:
        raise refuse(
            RefusalCode.PROVIDER_DECLINED,
            "web.fetch serves HTML pages up to 2 MiB; a larger max_bytes is a heavy page",
            reason="heavy_page",
            url=url,
            max_bytes=max_bytes,
            ceiling_bytes=MEDIUM_CEILING_BYTES,
        )


def _digest(payload: bytes) -> str:
    """The ``sha256:`` digest of ``payload``."""

    return "sha256:" + hashlib.sha256(payload).hexdigest()


class _Request:
    """The validated run input."""

    __slots__ = (
        "expected_format",
        "extract",
        "headers",
        "logical_source",
        "max_bytes",
        "url",
    )

    def __init__(
        self,
        *,
        url: str,
        max_bytes: int,
        extract: bool,
        headers: Mapping[str, str],
        expected_format: str,
        logical_source: str,
    ) -> None:
        self.url = url
        self.max_bytes = max_bytes
        self.extract = extract
        self.headers = dict(headers)
        self.expected_format = expected_format
        self.logical_source = logical_source

    @classmethod
    def parse(cls, context: ProviderRunContext) -> _Request:
        payload = context.input
        logical_source = payload.get("logical_source", "web.response")
        if not isinstance(logical_source, str) or not re.fullmatch(
            r"[a-z][a-z0-9_.-]{0,255}", logical_source
        ):
            raise refuse(
                RefusalCode.PROVIDER_DECLINED,
                "logical_source must be a logical name, not a locator",
            )
        url = payload.get("url")
        if not isinstance(url, str) or urlsplit(url).scheme not in {"http", "https"}:
            raise refuse(
                RefusalCode.PROVIDER_DECLINED,
                "web.fetch needs an http or https url",
                url=url if isinstance(url, str) else None,
            )
        max_bytes = payload.get("max_bytes", DEFAULT_MAX_BYTES)
        if (
            not isinstance(max_bytes, int)
            or isinstance(max_bytes, bool)
            or not 0 < max_bytes <= MAX_RESPONSE_BYTES
        ):
            raise refuse(RefusalCode.PROVIDER_DECLINED, "max_bytes must be between 1 and 33554432")

        for flag in ("render", "extract", "paced"):
            if flag in payload and not isinstance(payload[flag], bool):
                raise refuse(RefusalCode.PROVIDER_DECLINED, f"{flag} must be a bool")
        if payload.get("render", False):
            # This implementation runs no browser, so a rendered fetch has no
            # code path to reach.
            raise refuse(
                RefusalCode.PROVIDER_DECLINED,
                "web.fetch does not render pages; the js_rendered bucket is not claimed",
            )
        expected_format = payload.get("expected_format", "auto")
        if expected_format not in {"auto", "html", "json", "csv", "text", "bytes"}:
            raise refuse(RefusalCode.PROVIDER_DECLINED, "unsupported expected_format")
        _decline_unserved(payload, url, expected_format, max_bytes)
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
            if not isinstance(header, str) or not re.fullmatch(
                r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", header
            ):
                raise refuse(RefusalCode.PROVIDER_DECLINED, "credential_header must be a string")
            if "\r" in material or "\n" in material:
                raise refuse(
                    RefusalCode.PROVIDER_DECLINED, "credential must be a single header value"
                )
            headers[header] = material

        return cls(
            url=url,
            max_bytes=max_bytes,
            extract=bool(payload.get("extract", True)),
            headers=headers,
            expected_format=expected_format,
            logical_source=logical_source,
        )

    def validate_format(self, exchange: _Exchange) -> None:
        media = exchange.content_type.partition(";")[0].strip().lower()
        actual = (
            "json"
            if media.endswith("json") or media.endswith("+json")
            else "csv"
            if media in {"text/csv", "application/csv"}
            else "html"
            if media in {"text/html", "application/xhtml+xml"}
            else "text"
            if media.startswith("text/")
            else "bytes"
        )
        if self.expected_format == "auto":
            self.expected_format = actual
        elif self.expected_format != "bytes" and self.expected_format != actual:
            raise ValueError(
                f"expected {self.expected_format}, received {media or 'unknown content type'}"
            )
        if self.expected_format == "json":

            def invalid_constant(value: str) -> None:
                raise ValueError(f"non-canonical JSON number: {value}")

            json.loads(exchange.wire_body, parse_constant=invalid_constant)
        elif self.expected_format == "csv":
            text = exchange.wire_body.decode("utf-8-sig")
            rows = csv.reader(io.StringIO(text), strict=True)
            width = None
            for row in rows:
                if width is None:
                    width = len(row)
                elif len(row) != width:
                    raise ValueError("CSV rows have inconsistent field counts")
