"""``web.fetch.pages``: one parameter's values split across as few requests as fit (D-R34).

The split is held as a function of the request alone -- deterministic, in
order, every value exactly once, and no more pages than an in-order split
needs -- and the result as D-R37 a's list form: one verbatim Capture per page,
each with its own coordinate (the page's built URL and request digest, its index
and the page count), a failed page listed rather than silently dropped.
"""

from __future__ import annotations

import base64
import hashlib
import json
import random
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest
from cruxible_provider_runtime.egress import EgressRecorder
from cruxible_provider_runtime.errors import Declined
from cruxible_provider_runtime.operations import ContractRef
from cruxible_provider_runtime.provider_api import ProviderResult
from cruxible_provider_runtime.registration import load_registration
from cruxible_provider_runtime.testing import assert_payload_verbatim, captured_members
from cruxible_provider_web import PACKAGE_ROOT
from cruxible_provider_web.fetch import (
    MAX_URL_LENGTH,
    WebFetch,
    encoded_name,
    encoded_value,
    with_query,
)
from cruxible_provider_web.http import RecordingClient
from cruxible_provider_web.operations import PROVIDER_DECLINED
from cruxible_provider_web.pages import MAX_PAGES, WebFetchPages, split_pages

from .test_web_contracts import _conforms, _operation
from .test_web_secrets import _context

EPSS = "https://api.first.org/data/v1/epss"
#: 600 CVE ids: within the 1000-value bound, and over one 8192-character URL
#: once comma-joined.
WATCHED = [f"CVE-{2000 + index // 100}-{10000 + index * 37:05d}" for index in range(600)]

OUT = (
    load_registration(PACKAGE_ROOT)
    .contract(ContractRef.parse("web.fetch.pages.result@1"))
    .model_dump(mode="json")
)


def _origin_body(url: str) -> bytes:
    """What the origin double sends for ``url``: a body that names the request."""

    cves = dict(parse_qsl(urlsplit(url).query)).get("cve", "")
    return json.dumps({"url": url, "cves": cves.split(",") if cves else []}).encode()


def _factory(
    *, failing: frozenset[str] = frozenset(), unreachable: frozenset[str] = frozenset()
) -> Any:
    """A production RecordingClient over a transport answering each page by its URL.

    A page whose URL carries a value in ``failing`` answers 503; one carrying a
    value in ``unreachable`` raises a connect error.
    """

    def handle(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if any(value in url for value in unreachable):
            raise httpx.ConnectError("connection refused", request=request)
        status = 503 if any(value in url for value in failing) else 200
        return httpx.Response(
            status_code=status,
            headers={"content-type": "application/json"},
            content=_origin_body(url),
            request=request,
        )

    def factory(recorder: Any, *, url: str, timeout_seconds: float) -> RecordingClient:
        del url
        return RecordingClient(
            recorder, timeout_seconds=timeout_seconds, transport=httpx.MockTransport(handle)
        )

    return factory


def _query(values: list[str], *, join: str = "comma") -> list[dict[str, Any]]:
    return [
        {"name": "scope", "values": ["time-series"]},
        {"name": "cve", "values": values, "join": join},
        {"name": "envelope", "values": ["true"]},
    ]


def _call(payload: dict[str, Any], **factory: Any) -> tuple[ProviderResult, EgressRecorder]:
    recorder = EgressRecorder()
    request = {"url": EPSS, "page_param": "cve", "query": _query(WATCHED), **payload}
    result = WebFetchPages(client_factory=_factory(**factory))(
        _context("web.fetch.pages", input=request, egress=recorder)
    )
    return result, recorder


def _cves(url: str) -> list[str]:
    return [
        value
        for name, joined in parse_qsl(urlsplit(url).query)
        if name == "cve"
        for value in joined.split(",")
    ]


def _parsed(query: list[dict[str, Any]]) -> list[tuple[str, list[str], str]]:
    return [(item["name"], item["values"], item.get("join", "repeat")) for item in query]


# -- the split -------------------------------------------------------------


def test_a_600_value_comma_list_is_split_into_the_fewest_pages_that_fit() -> None:
    pages = split_pages(EPSS, _parsed(_query(WATCHED)), "cve", max_url_chars=MAX_URL_LENGTH)
    assert len(with_query(EPSS, _parsed(_query(WATCHED)))) > MAX_URL_LENGTH
    # 600 ids of 14-15 characters, comma-joined: about 9,000 characters of
    # values, so two pages, and never one.
    assert len(pages) == 2
    for url, values in pages:
        assert len(url) <= MAX_URL_LENGTH
        # The other parameters repeat on every page, in their place.
        assert url.startswith(f"{EPSS}?scope=time-series&cve=")
        assert url.endswith("&envelope=true")
        assert _cves(url) == values
    # Greedy: a page is closed only when the next value would not fit.
    _first_url, first_values = pages[0]
    next_value = pages[1][1][0]
    grown = with_query(EPSS, _parsed(_query([*first_values, next_value])))
    assert len(grown) > MAX_URL_LENGTH


@pytest.mark.parametrize("join", ["comma", "repeat"])
@pytest.mark.parametrize("max_url_chars", [160, 400, 2048, MAX_URL_LENGTH])
def test_every_value_appears_exactly_once_and_in_order(join: str, max_url_chars: int) -> None:
    values = WATCHED[:120]
    pages = split_pages(
        EPSS, _parsed(_query(values, join=join)), "cve", max_url_chars=max_url_chars
    )
    flattened = [value for _url, chunk in pages for value in chunk]
    assert flattened == values
    for url, chunk in pages:
        assert len(url) <= max_url_chars
        assert _cves(url) == chunk
        assert url == with_query(EPSS, _parsed(_query(chunk, join=join)))


def test_the_split_is_deterministic() -> None:
    query = _parsed(_query(WATCHED))
    first = split_pages(EPSS, query, "cve", max_url_chars=1000)
    again = split_pages(EPSS, [(n, list(v), j) for n, v, j in query], "cve", max_url_chars=1000)
    assert first == again
    reordered = _parsed(_query(list(reversed(WATCHED))))
    assert split_pages(EPSS, reordered, "cve", max_url_chars=1000) != first


def _fewest_pages(url: str, query: list[tuple[str, list[str], str]], limit: int) -> int:
    """The fewest pages any in-order contiguous split needs, by exhaustive search."""

    position = next(index for index, item in enumerate(query) if item[0] == "cve")
    name, values, join = query[position]

    def length(chunk: list[str]) -> int:
        built = list(query)
        built[position] = (name, chunk, join)
        return len(with_query(url, built))

    best = [0] + [len(values) + 1] * len(values)
    for end in range(1, len(values) + 1):
        for start in range(end):
            if length(values[start:end]) <= limit:
                best[end] = min(best[end], best[start] + 1)
    return best[len(values)]


@pytest.mark.parametrize("seed", range(12))
def test_no_in_order_split_needs_fewer_pages(seed: int) -> None:
    generator = random.Random(seed)
    values = [
        "".join(generator.choice("abc,&= é") for _ in range(generator.randint(1, 30)))
        for _ in range(generator.randint(1, 40))
    ]
    values = list(dict.fromkeys(values))
    join = generator.choice(["comma", "repeat"])
    query = _parsed(_query(values, join=join))
    limit = generator.randint(150, 600)
    try:
        pages = split_pages(EPSS, query, "cve", max_url_chars=limit)
    except Declined:
        # Only a value too long for a page on its own refuses.
        assert _fewest_pages(EPSS, query, limit) > len(values)
        return
    assert len(pages) == _fewest_pages(EPSS, query, limit)
    assert [value for _url, chunk in pages for value in chunk] == values


def test_the_measured_length_is_the_built_length() -> None:
    """The per-value growth the split adds up is exactly what the URL grows by."""

    for join in ("comma", "repeat"):
        values = ["a,b", "c d", "é&=", "plain"]
        base = len(with_query(EPSS, _parsed(_query(values[:1], join=join))))
        separator = 1 if join == "comma" else len(encoded_name("cve")) + 2
        grown = base + sum(separator + len(encoded_value(value, join)) for value in values[1:])
        assert grown == len(with_query(EPSS, _parsed(_query(values, join=join))))


# -- the result ------------------------------------------------------------


def test_each_page_is_its_own_verbatim_capture_with_its_own_coordinate() -> None:
    result, recorder = _call({"max_url_chars": 2048})
    assert result.status == "ok", result
    assert "failed_pages" not in result.output
    assert result.incomplete_captures == []
    members = captured_members(result.output, OUT)
    assert_payload_verbatim(members)
    count = len(members)
    assert count == len(split_pages(EPSS, _parsed(_query(WATCHED)), "cve", max_url_chars=2048))
    sent: list[str] = []
    for index, member in enumerate(members):
        coordinate = member.coordinate
        url = coordinate["url"]
        # The stored bytes are exactly what the origin sent for this page.
        assert member.stored == _origin_body(url)
        assert coordinate["final_url"] == url
        assert coordinate["request_digest"] == "sha256:" + hashlib.sha256(url.encode()).hexdigest()
        assert coordinate["page_index"] == index
        assert coordinate["page_count"] == count
        assert coordinate["status"] == 200 and coordinate["source"] == "network"
        sent += _cves(url)
    assert sent == WATCHED
    assert len({member.coordinate["request_digest"] for member in members}) == count
    assert recorder.observed() == ["https://api.first.org"]
    assert result.metrics["page_count"] == count


def test_two_calls_with_the_same_request_send_the_same_pages() -> None:
    first, _ = _call({"max_url_chars": 2048})
    again, _ = _call({"max_url_chars": 2048})
    coordinates = [
        [member.coordinate for member in captured_members(result.output, OUT)]
        for result in (first, again)
    ]
    assert coordinates[0] == coordinates[1]
    assert [item["content_base64"] for item in first.output["captures"]] == [
        item["content_base64"] for item in again.output["captures"]
    ]


def test_a_failed_page_is_left_out_and_listed() -> None:
    pages = split_pages(EPSS, _parsed(_query(WATCHED)), "cve", max_url_chars=2048)
    failing = pages[1][1][0]
    result, _ = _call({"max_url_chars": 2048}, failing=frozenset({failing}))
    assert result.status == "ok", result
    members = captured_members(result.output, OUT)
    assert [member.coordinate["page_index"] for member in members] == [
        index for index in range(len(pages)) if index != 1
    ]
    (failure,) = result.output["failed_pages"]
    assert failure == {
        "page_index": 1,
        "page_count": len(pages),
        "url": pages[1][0],
        "request_digest": "sha256:" + hashlib.sha256(pages[1][0].encode()).hexdigest(),
        "outcome": "failed",
        "code": "HttpStatus",
        "message": "origin answered 503",
        "status_code": 503,
    }
    assert result.metrics["failed_page_count"] == 1
    assert result.incomplete_captures == ["captures"]


def test_an_unreachable_page_is_a_failed_page_not_a_failed_call() -> None:
    pages = split_pages(EPSS, _parsed(_query(WATCHED)), "cve", max_url_chars=2048)
    result, _ = _call({"max_url_chars": 2048}, unreachable=frozenset({pages[0][1][0]}))
    assert result.status == "ok", result
    (failure,) = result.output["failed_pages"]
    assert failure["page_index"] == 0 and failure["code"] == "ConnectError"
    assert len(result.output["captures"]) == len(pages) - 1
    assert result.incomplete_captures == ["captures"]


def test_a_refused_page_is_listed_with_its_code() -> None:
    """A page over max_bytes is refused by the client, and only that page."""

    pages = split_pages(EPSS, _parsed(_query(WATCHED)), "cve", max_url_chars=2048)
    longest = max(range(len(pages)), key=lambda index: len(_origin_body(pages[index][0])))
    cap = len(_origin_body(pages[longest][0])) - 1
    result, _ = _call({"max_url_chars": 2048, "max_bytes": cap})
    assert result.status == "ok", result
    refused = [item for item in result.output["failed_pages"] if item["page_index"] == longest]
    assert refused and refused[0]["outcome"] == "refused"
    assert refused[0]["code"] == PROVIDER_DECLINED


def test_when_every_page_fails_there_is_no_capture() -> None:
    pages = split_pages(EPSS, _parsed(_query(WATCHED)), "cve", max_url_chars=2048)
    result, _ = _call({"max_url_chars": 2048}, failing=frozenset({"CVE-"}))
    assert result.status == "error" and result.error is not None
    assert result.error.kind == "AllPagesFailed"
    assert len(result.error.detail["failed_pages"]) == len(pages)
    # One page is one web.fetch: its outcome is returned as web.fetch's would be.
    single, _ = _call({"query": _query(WATCHED[:3])}, failing=frozenset({"CVE-"}))
    fetched = WebFetch(client_factory=_factory(failing=frozenset({"CVE-"})))(
        _context("web.fetch", input={"url": EPSS, "query": _query(WATCHED[:3])})
    )
    assert single.status == fetched.status == "error"
    assert single.error == fetched.error


@pytest.mark.parametrize("join", ["comma", "repeat"])
def test_an_empty_value_list_asks_nothing_and_returns_no_captures(join: str) -> None:
    """Nothing asked, nothing fetched: a complete, successful result with no Captures."""

    assert split_pages(EPSS, _parsed(_query([], join=join)), "cve", max_url_chars=2048) == []
    request = {"url": EPSS, "page_param": "cve", "query": _query([], join=join)}
    _conforms(request, _operation("web.fetch.pages").in_.spelling)
    opened: list[str] = []

    def factory(recorder: Any, *, url: str, timeout_seconds: float) -> Any:
        opened.append(url)
        raise AssertionError("an empty value list sends nothing")

    recorder = EgressRecorder()
    result = WebFetchPages(client_factory=factory)(
        _context("web.fetch.pages", input=request, egress=recorder)
    )
    assert result.status == "ok", result
    assert result.refusal is None and result.error is None
    assert result.output == {"captures": []}
    _conforms(result.output, _operation("web.fetch.pages").out.spelling)
    # Complete: no member is missing, so nothing is named incomplete.
    assert result.incomplete_captures == []
    assert result.metrics == {"byte_count": 0.0, "page_count": 0.0, "failed_page_count": 0.0}
    assert result.events == []
    # No client was opened and no request reached the recorder.
    assert opened == []
    assert recorder.observed() == []


def test_the_result_is_bounded_by_its_contract() -> None:
    assert OUT["fields"]["captures"]["type"]["max_items"] == MAX_PAGES
    assert OUT["fields"]["failed_pages"]["type"]["max_items"] == MAX_PAGES


def test_a_member_s_bytes_carry_no_wrapper() -> None:
    result, _ = _call({"max_url_chars": 2048})
    for member in result.output["captures"]:
        stored = base64.b64decode(member["content_base64"])
        assert set(json.loads(stored)) == {"url", "cves"}


# -- the declines ----------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"page_param": None},
        {"page_param": ""},
        {"page_param": "station"},
        {"max_url_chars": 0},
        {"max_url_chars": MAX_URL_LENGTH + 1},
        {"max_url_chars": True},
        {"max_url_chars": 60},
        {"query": _query(["CVE-1", "CVE-2", "CVE-1"])},
        {"url": f"{EPSS}?cve=CVE-old"},
        # Only the paged parameter may be empty; any other still needs a value.
        {"query": [{"name": "scope", "values": []}, *_query(WATCHED)[1:]]},
        {"query": _query([]), "page_param": "scope"},
        # Exactly one value fits a page, and there is one value too many.
        {
            "query": _query([f"CVE-{index:04d}" for index in range(MAX_PAGES + 1)]),
            "max_url_chars": len(with_query(EPSS, _parsed(_query(["CVE-0000"])))),
        },
    ],
    ids=[
        "no-page-param",
        "empty-page-param",
        "unknown-page-param",
        "max-url-zero",
        "max-url-over",
        "max-url-bool",
        "value-alone-too-long",
        "value-twice",
        "name-in-url",
        "empty-other-param",
        "empty-unpaged-param",
        "too-many-pages",
    ],
)
def test_a_split_the_adapter_cannot_make_is_declined(payload: dict[str, Any]) -> None:
    if payload.get("page_param", "cve") is None:
        payload = {}
        request: dict[str, Any] = {"url": EPSS, "query": _query(WATCHED)}
    else:
        request = {"url": EPSS, "page_param": "cve", "query": _query(WATCHED), **payload}
    seen: list[str] = []

    def factory(recorder: Any, *, url: str, timeout_seconds: float) -> Any:
        seen.append(url)
        raise AssertionError("a declined split sends nothing")

    result = WebFetchPages(client_factory=factory)(_context("web.fetch.pages", input=request))
    assert result.status == "refused" and result.refusal is not None
    assert result.refusal.code == PROVIDER_DECLINED
    assert seen == []


def test_web_fetch_points_an_over_long_url_at_web_fetch_pages() -> None:
    result = WebFetch(client_factory=_factory())(
        _context("web.fetch", input={"url": EPSS, "query": _query(WATCHED)})
    )
    assert result.status == "refused" and result.refusal is not None
    assert result.refusal.code == PROVIDER_DECLINED
    assert "web.fetch.pages" in result.refusal.message


def test_the_registration_serves_pages_as_a_fetch() -> None:
    operation = load_registration(PACKAGE_ROOT).operation("web.fetch.pages")
    assert operation.effect == "external_read"
    assert operation.entrypoint == "cruxible_provider_web.pages:WebFetchPages"
