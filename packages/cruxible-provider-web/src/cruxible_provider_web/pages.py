"""``web.fetch.pages`` -- one request too long for one URL, split into pages (D-R34).

A Procedure passes the values it read from state as a typed ``query``
parameter. A watched set grows, and the URL its values build eventually exceeds
what an origin accepts. ``web.fetch`` declines that URL; this operation takes
the same request, names the parameter to split (``page_param``), and sends that
parameter's values across **as few requests as fit**, every other parameter
repeated on each page.

**The split.** Values are taken in the order given and packed greedily: a page
takes the next value while its built URL stays within ``max_url_chars``
(default 8192), and a value that does not fit starts the next page. For a
contiguous split in order this is the fewest pages, and it is a function of the
request alone: the same request always builds the same pages. Every value lands
on exactly one page. A value that does not fit on a page by itself declines, as
does a split needing more than :data:`MAX_PAGES` pages, and a value named twice
in the paged parameter (two pages could then be one request).

**The result** is ``captures: list<capture<*>>`` (D-R37 a): one Capture per page,
each the body that page's origin sent, verbatim, in page order. Each member's
coordinate is ``web.fetch``'s HTTP coordinate for that page's exchange: ``url``
is the page's built URL and ``request_digest`` covers it, and ``page_index``
(0-based) and ``page_count`` say which page of how many it is.

**A page that fails** -- refused (a redirect the client will not follow, a body
over ``max_bytes``) or failed (a status outside 2xx, a body not in the declared
format, a transport error) -- is left out of ``captures`` and listed in
``failed_pages`` with its index, URL, request digest and why. The result is then
incomplete, and says so in a typed field rather than by a short list: core
reads ``failed_pages`` and marks the extract incomplete, so absence never
retires on it (D-R37 a4). When no page succeeds there is nothing to commit: a
single page's outcome is returned as ``web.fetch`` would return it, and several
failed pages are one failed result naming each.

Egress is ``web.fetch``'s: every page goes through the instrumented client, so
every request -- redirect hops included -- reaches the run's recorder.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import httpx
from cruxible_provider_runtime.acquisition import external_capture_result
from cruxible_provider_runtime.errors import Declined, decline
from cruxible_provider_runtime.provider_api import ProviderResult, ProviderRunContext

from .coordinate import COORDINATE_TYPE, SELECTOR_TYPE, content_digest, http_coordinate
from .fetch import (
    MAX_URL_LENGTH,
    FetchRequest,
    encoded_name,
    encoded_value,
    exchange,
    recording_events,
    with_query,
)
from .http import ClientFactory, HttpResponse, default_client_factory
from .operations import FETCH_PAGES_OPERATION, PROVIDER_DECLINED

__all__ = ["MAX_PAGES", "Page", "WebFetchPages", "split_pages"]

MAX_PAGES = 64
"""The most pages one call sends: the result Contract's ``max_items``."""

Page = tuple[str, list[str]]
"""One page: its built URL and the slice of the paged parameter's values it carries."""


def split_pages(
    url: str,
    query: list[tuple[str, list[str], str]],
    page_param: str,
    *,
    max_url_chars: int,
) -> list[Page]:
    """``page_param``'s values split, in order, across as few URLs as fit ``max_url_chars``.

    Every other parameter is repeated on each page, in its place. The packing is
    greedy over the values in order, which gives the fewest pages any in-order
    contiguous split can. A page's length grows by an exact amount per added
    value, so each candidate is measured without rebuilding the URL; each built
    page is still checked against the bound.
    """

    positions = [index for index, (name, _, _) in enumerate(query) if name == page_param]
    if not positions:
        raise decline(
            PROVIDER_DECLINED,
            f"page_param {page_param!r} names no parameter of the query",
        )
    (position,) = positions
    name, values, join = query[position]
    if len(set(values)) != len(values):
        raise decline(
            PROVIDER_DECLINED,
            f"a value of the paged parameter {name!r} is named twice; each value is fetched once",
        )

    def build(chunk: list[str]) -> str:
        pages_query = list(query)
        pages_query[position] = (name, chunk, join)
        return with_query(url, pages_query)

    def alone(value: str) -> int:
        length = len(build([value]))
        if length > max_url_chars:
            raise decline(
                PROVIDER_DECLINED,
                f"one value of {name!r} does not fit a request URL of {max_url_chars} "
                "characters on its own",
            )
        return length

    # Adding a value to a page adds exactly its separator and its encoding:
    # ``,v`` to a comma-joined parameter, ``&name=v`` to a repeated one.
    separator = 1 if join == "comma" else len(encoded_name(name)) + 2
    slices: list[tuple[int, int]] = []
    start, length = 0, alone(values[0])
    for index in range(1, len(values)):
        grown = length + separator + len(encoded_value(values[index], join))
        if grown <= max_url_chars:
            length = grown
            continue
        slices.append((start, index))
        if len(slices) >= MAX_PAGES:
            raise decline(
                PROVIDER_DECLINED,
                f"the values of {name!r} need more than {MAX_PAGES} requests of "
                f"{max_url_chars} characters",
            )
        start, length = index, alone(values[index])
    slices.append((start, len(values)))

    pages: list[Page] = []
    for first, last in slices:
        chunk = values[first:last]
        built = build(chunk)
        if len(built) > max_url_chars:  # pragma: no cover - the measure above is exact
            raise AssertionError(f"page URL measured within the bound built {len(built)} long")
        pages.append((built, chunk))
    return pages


def _paging(payload: Mapping[str, Any]) -> tuple[str, int]:
    page_param = payload.get("page_param")
    if not isinstance(page_param, str) or not page_param:
        raise decline(PROVIDER_DECLINED, "web.fetch.pages needs page_param, a query parameter name")
    max_url_chars = payload.get("max_url_chars", MAX_URL_LENGTH)
    if (
        not isinstance(max_url_chars, int)
        or isinstance(max_url_chars, bool)
        or not 0 < max_url_chars <= MAX_URL_LENGTH
    ):
        raise decline(PROVIDER_DECLINED, f"max_url_chars must be between 1 and {MAX_URL_LENGTH}")
    return page_param, max_url_chars


def _failure(index: int, count: int, url: str, outcome: ProviderResult) -> dict[str, Any]:
    """A ``web.fetch.pages.failure@1`` record: which page, and why it has no Capture."""

    record: dict[str, Any] = {
        "page_index": index,
        "page_count": count,
        "url": url,
        "request_digest": content_digest(url.encode("utf-8")),
    }
    if outcome.refusal is not None:
        record.update(outcome="refused", code=outcome.refusal.code, message=outcome.refusal.message)
    else:
        assert outcome.error is not None
        record.update(outcome="failed", code=outcome.error.kind, message=outcome.error.message)
        status_code = outcome.error.detail.get("status_code")
        if isinstance(status_code, int):
            record["status_code"] = status_code
    return record


class WebFetchPages:
    """Fetch one request's pages, one Capture per page."""

    operations = (FETCH_PAGES_OPERATION,)

    def __init__(self, *, client_factory: ClientFactory | None = None) -> None:
        # The production client, as for web.fetch; the seam holds a transport still.
        self._client_factory = client_factory or default_client_factory

    def __call__(self, context: ProviderRunContext) -> ProviderResult:
        try:
            request = FetchRequest.parse(context, paged=True)
            page_param, max_url_chars = _paging(context.input)
            pages = split_pages(request.url, request.query, page_param, max_url_chars=max_url_chars)
        except Declined as exc:
            return ProviderResult(status="refused", refusal=exc.refusal)

        count = len(pages)
        # One budget for the whole call, shared by its pages.
        timeout_seconds = max(1.0, context.budgets.wall_clock_seconds * 0.8 / count)
        captures: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        outcomes: list[ProviderResult] = []
        events: list[dict[str, Any]] = []
        byte_count = 0
        for index, (url, _values) in enumerate(pages):
            exchanged = self._exchange(context, request, url, timeout_seconds)
            if isinstance(exchanged, ProviderResult):
                failed.append(_failure(index, count, url, exchanged))
                outcomes.append(exchanged)
                continue
            byte_count += len(exchanged.body)
            events += recording_events(exchanged, url)
            captures.append(_member(request, url, exchanged, index=index, count=count))

        if not captures:
            if count == 1:
                # One page is one web.fetch: its refusal or failure is the answer.
                return outcomes[0]
            return ProviderResult.failed(
                "AllPagesFailed",
                f"none of the {count} pages was retrieved",
                failed_pages=failed,
            )
        output: dict[str, Any] = {"captures": captures}
        if failed:
            output["failed_pages"] = failed
        return ProviderResult.ok(
            output,
            metrics={
                "byte_count": float(byte_count),
                "page_count": float(count),
                "failed_page_count": float(len(failed)),
            },
            events=events,
        )

    def _exchange(
        self, context: ProviderRunContext, request: FetchRequest, url: str, timeout: float
    ) -> HttpResponse | ProviderResult:
        try:
            return exchange(self._client_factory, context, request, url, timeout_seconds=timeout)
        except httpx.HTTPError as exc:
            # One unreachable page is that page's failure, not the call's: the
            # pages already retrieved are still this run's evidence.
            return ProviderResult.failed(type(exc).__name__, str(exc) or type(exc).__name__)


def _member(
    request: FetchRequest, url: str, response: HttpResponse, *, index: int, count: int
) -> dict[str, Any]:
    """One page's Capture wire: its body verbatim, its exchange as the coordinate."""

    return external_capture_result(
        response.body,
        source_identity=request.logical_source,
        coordinate_type=COORDINATE_TYPE,
        selector_type=SELECTOR_TYPE,
        coordinate=http_coordinate(
            url=url,
            final_url=response.final_url,
            status=response.status_code,
            content_type=response.content_type,
            response_headers=response.headers,
            from_recording=response.from_recording is not None,
            page_index=index,
            page_count=count,
        ),
        observed_at=datetime.now(UTC),
    )
