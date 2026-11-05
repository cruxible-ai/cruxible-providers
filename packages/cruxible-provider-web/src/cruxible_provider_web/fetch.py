"""``web.fetch`` — retrieve one web resource; its Capture is the body the origin sent.

The Capture's bytes are the response body, verbatim. Nothing is wrapped around
them and nothing is derived from them here: how the body was obtained (the URL
asked for, the URL the redirect chain settled on, the status, the media type,
the request digest, the freshness headers, whether a network or a packaged
recording answered) is the typed coordinate (:mod:`.coordinate`). A reading of
the document — rendered, extracted, converted — is a different operation,
``web.render``, whose output is a derived Capture citing this one.

The adapter never mints a Capture. It returns Capture wire and the executor
carries it to the CaptureKind; core commits the bytes and never looks inside
them except through a declared ``extract``.

**Egress.** This implementation cannot enumerate its endpoints: the resource is
named by the caller, which is the whole point of the operation. Its manifest
therefore declares the experimental ``dynamic:target-from-run-input`` form, and
what governs is the recording — every request the client issues reaches the
run's egress recorder through an httpx event hook, redirect hops included.

One reading note about that recording. A request to the reserved
``fixture.invalid`` host is served from a recording shipped in this distribution
rather than from a socket; it is still recorded, because the recorder's subject
is the request the adapter issued. Nobody can misread the receipt: ``.invalid``
resolves nowhere, and the coordinate's ``source`` says ``packaged-recording`` in
so many words.
"""

from __future__ import annotations

import csv
import io
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

from cruxible_provider_runtime.acquisition import external_capture_result
from cruxible_provider_runtime.errors import Declined, decline
from cruxible_provider_runtime.provider_api import ProviderResult, ProviderRunContext

from .coordinate import COORDINATE_TYPE, SELECTOR_TYPE, http_coordinate
from .http import ClientFactory, HttpResponse, ResponseTooLarge, default_client_factory
from .operations import CREDENTIAL_UNAVAILABLE, FETCH_OPERATION, PROVIDER_DECLINED

__all__ = [
    "DEFAULT_MAX_BYTES",
    "MAX_QUERY_PARAMETERS",
    "MAX_QUERY_VALUES",
    "MAX_RESPONSE_BYTES",
    "MAX_URL_LENGTH",
    "WebFetch",
    "page_weight_class",
    "with_query",
]

DEFAULT_MAX_BYTES = 262_144
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
LIGHT_CEILING_BYTES = 262_144
MEDIUM_CEILING_BYTES = 2 * 1024 * 1024
MAX_QUERY_PARAMETERS = 64
MAX_QUERY_VALUES = 1000
MAX_URL_LENGTH = 8192
"""The longest request URL this adapter builds; many origins refuse longer ones."""
_QUERY_SAFE = "-._~!$'()*,;:@/?"
"""RFC 3986 query characters kept literal; ``&``, ``=``, ``+`` and ``#`` are encoded."""


def with_query(url: str, query: list[tuple[str, list[str], str]]) -> str:
    """``url`` with ``query``'s ``(name, values, join)`` parameters appended, in order."""

    pairs: list[str] = []
    for name, values, join in query:
        key = quote(name, safe=_QUERY_SAFE)
        if join == "comma":
            item_safe = _QUERY_SAFE.replace(",", "")
            pairs.append(key + "=" + ",".join(quote(value, safe=item_safe) for value in values))
        else:
            pairs.extend(f"{key}={quote(value, safe=_QUERY_SAFE)}" for value in values)
    if not pairs:
        return url
    parts = urlsplit(url)
    query_string = "&".join([parts.query, *pairs] if parts.query else pairs)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query_string, parts.fragment))


def page_weight_class(byte_count: int) -> str:
    """The weight class a byte count falls in, reported on an oversized response."""

    if byte_count <= LIGHT_CEILING_BYTES:
        return "light"
    if byte_count <= MEDIUM_CEILING_BYTES:
        return "medium"
    return "heavy"


class WebFetch:
    """Retrieve a resource the run input names."""

    operations = (FETCH_OPERATION,)

    def __init__(self, *, client_factory: ClientFactory | None = None) -> None:
        # The default is the production spelling. The seam exists so a test can
        # hold the transport still, not so a test can replace the thing under
        # test: the conformance suite drives this same default.
        self._client_factory = client_factory or default_client_factory

    def __call__(self, context: ProviderRunContext) -> ProviderResult:
        try:
            request = _Request.parse(context)
        except Declined as exc:
            return ProviderResult(status="refused", refusal=exc.refusal)

        timeout_seconds = max(1.0, context.budgets.wall_clock_seconds * 0.8)
        try:
            client = self._client_factory(
                context.egress, url=request.url, timeout_seconds=timeout_seconds
            )
            with client:
                response = client.get(
                    request.url, headers=request.headers, cap_bytes=request.max_bytes
                )
        except Declined as exc:
            return ProviderResult(status="refused", refusal=exc.refusal)
        except ResponseTooLarge as exc:
            return ProviderResult.refused(
                PROVIDER_DECLINED,
                "the origin sent more than the max_bytes this run declared",
                url=request.url,
                cap_bytes=exc.cap_bytes,
                observed_weight=page_weight_class(exc.read_bytes),
            )

        if not 200 <= response.status_code < 300:
            # A status outside 2xx is a failed attempt at an answer, not a
            # declined one: the operation's product is a retrieved resource, and
            # this run did not get one.
            return ProviderResult.failed(
                "HttpStatus",
                f"origin answered {response.status_code}",
                url=request.url,
                final_url=response.final_url,
                status_code=response.status_code,
            )
        try:
            request.validate_format(response)
        except (ValueError, UnicodeError, csv.Error) as exc:
            return ProviderResult.failed("ResponseFormat", str(exc))

        events: list[dict[str, Any]] = []
        if response.from_recording is not None:
            events.append(
                {
                    "kind": "packaged_recording",
                    "url": request.url,
                    "note": "served from a recording shipped in this distribution; no origin "
                    "was contacted",
                }
            )
        return ProviderResult.ok(
            external_capture_result(
                response.body,
                source_identity=request.logical_source,
                coordinate_type=COORDINATE_TYPE,
                selector_type=SELECTOR_TYPE,
                coordinate=http_coordinate(
                    url=request.url,
                    final_url=response.final_url,
                    status=response.status_code,
                    content_type=response.content_type,
                    response_headers=response.headers,
                    from_recording=response.from_recording is not None,
                ),
                observed_at=datetime.now(UTC),
            ),
            metrics={"byte_count": float(len(response.body))},
            events=events,
        )


def _query(value: object) -> list[tuple[str, list[str], str]]:
    """The validated ``query``: one parameter per name, each with at least one value."""

    if not isinstance(value, list) or len(value) > MAX_QUERY_PARAMETERS:
        raise decline(
            PROVIDER_DECLINED, f"query must be a list of at most {MAX_QUERY_PARAMETERS} parameters"
        )
    parameters: list[tuple[str, list[str], str]] = []
    names: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping) or set(item) - {"name", "values", "join"}:
            raise decline(PROVIDER_DECLINED, "a query parameter is {name, values, join?}")
        name, values, join = item.get("name"), item.get("values"), item.get("join", "repeat")
        if not isinstance(name, str) or not name:
            raise decline(PROVIDER_DECLINED, "a query parameter needs a name")
        if name in names:
            raise decline(PROVIDER_DECLINED, f"query parameter {name!r} is named twice")
        names.add(name)
        if (
            not isinstance(values, list)
            or not 0 < len(values) <= MAX_QUERY_VALUES
            or not all(isinstance(entry, str) for entry in values)
        ):
            # No values is refused rather than sent as nothing: an empty filter
            # usually means "everything", which is not what an empty list asked.
            raise decline(
                PROVIDER_DECLINED,
                f"query parameter {name!r} needs between 1 and {MAX_QUERY_VALUES} string values",
            )
        if join not in {"comma", "repeat"}:
            raise decline(PROVIDER_DECLINED, "a query parameter's join is comma or repeat")
        parameters.append((name, list(values), str(join)))
    return parameters


class _Request:
    """The validated run input."""

    __slots__ = ("expected_format", "headers", "logical_source", "max_bytes", "url")

    def __init__(
        self,
        *,
        url: str,
        max_bytes: int,
        headers: Mapping[str, str],
        expected_format: str,
        logical_source: str,
    ) -> None:
        self.url = url
        self.max_bytes = max_bytes
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
            raise decline(
                PROVIDER_DECLINED,
                "logical_source must be a logical name, not a locator",
            )
        url = payload.get("url")
        if not isinstance(url, str) or urlsplit(url).scheme not in {"http", "https"}:
            # The detail names the url only when there is one: an optional
            # detail field is an absent key, never null.
            raise decline(
                PROVIDER_DECLINED,
                "web.fetch needs an http or https url",
                **({"url": url} if isinstance(url, str) else {}),
            )
        if "query" in payload:
            url = with_query(url, _query(payload["query"]))
            if len(url) > MAX_URL_LENGTH:
                raise decline(
                    PROVIDER_DECLINED,
                    f"the request URL the query builds is longer than {MAX_URL_LENGTH} "
                    "characters; split the values across fetches",
                )
        max_bytes = payload.get("max_bytes", DEFAULT_MAX_BYTES)
        if (
            not isinstance(max_bytes, int)
            or isinstance(max_bytes, bool)
            or not 0 < max_bytes <= MAX_RESPONSE_BYTES
        ):
            raise decline(PROVIDER_DECLINED, "max_bytes must be between 1 and 33554432")
        if "paced" in payload and not isinstance(payload["paced"], bool):
            raise decline(PROVIDER_DECLINED, "paced must be a bool")
        expected_format = payload.get("expected_format", "auto")
        if expected_format not in {"auto", "html", "json", "csv", "text", "bytes"}:
            raise decline(PROVIDER_DECLINED, "unsupported expected_format")
        headers: dict[str, str] = {}
        credential_ref = payload.get("credential_ref")
        if credential_ref is not None:
            if not isinstance(credential_ref, str):
                raise decline(PROVIDER_DECLINED, "credential_ref must be a string")
            material = context.secrets.get(credential_ref)
            if material is None:
                raise decline(
                    CREDENTIAL_UNAVAILABLE,
                    f"the run names credential {credential_ref!r} and it was not delivered",
                    ref=credential_ref,
                )
            header = payload.get("credential_header", "authorization")
            if not isinstance(header, str) or not re.fullmatch(
                r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", header
            ):
                raise decline(PROVIDER_DECLINED, "credential_header must be a string")
            if "\r" in material or "\n" in material:
                raise decline(PROVIDER_DECLINED, "credential must be a single header value")
            headers[header] = material

        return cls(
            url=url,
            max_bytes=max_bytes,
            headers=headers,
            expected_format=expected_format,
            logical_source=logical_source,
        )

    def validate_format(self, response: HttpResponse) -> None:
        """Refuse a body that is not the format the run declared.

        The provider checks its own payload; core never parses Capture bytes at
        admission, so a wrong-format body is this adapter's failure to report.
        """

        media = response.content_type.partition(";")[0].strip().lower()
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

            json.loads(response.body, parse_constant=invalid_constant)
        elif self.expected_format == "csv":
            text = response.body.decode("utf-8-sig")
            rows = csv.reader(io.StringIO(text), strict=True)
            width = None
            for row in rows:
                if width is None:
                    width = len(row)
                elif len(row) != width:
                    raise ValueError("CSV rows have inconsistent field counts")
