"""``web.fetch``'s typed ``query``: values from state, encoded deterministically (D-R34).

A Procedure passes values it read from state as typed parameters rather than
spelling them into a URL. The encoding is a function of the parameters alone, so
the same parameters always build the same request: the coordinate records the
built URL and its request digest covers it.
"""

from __future__ import annotations

import hashlib
from typing import Any

import pytest
from cruxible_provider_web.fetch import MAX_URL_LENGTH, WebFetch, with_query
from cruxible_provider_web.operations import PROVIDER_DECLINED

from .test_web_secrets import _CapturingClient, _context

BASE = "https://api.example/data/v1/epss"


def _fetch(payload: dict[str, Any]) -> tuple[Any, _CapturingClient]:
    client = _CapturingClient('{"data": []}', "application/json")
    provider = WebFetch(client_factory=lambda recorder, *, url, timeout_seconds: client)  # type: ignore[arg-type,return-value]
    return provider(_context("web.fetch", input=payload)), client


@pytest.mark.parametrize(
    ("url", "query", "built"),
    [
        (BASE, [("cve", ["CVE-1", "CVE-2"], "comma")], f"{BASE}?cve=CVE-1,CVE-2"),
        (BASE, [("cve", ["CVE-1", "CVE-2"], "repeat")], f"{BASE}?cve=CVE-1&cve=CVE-2"),
        (
            f"{BASE}?limit=10",
            [("cve", ["CVE-1"], "repeat"), ("scope", ["time-series"], "repeat")],
            f"{BASE}?limit=10&cve=CVE-1&scope=time-series",
        ),
        (f"{BASE}#top", [("q", ["a b"], "repeat")], f"{BASE}?q=a%20b#top"),
        # The separators a query is read by are always encoded; so is a comma
        # inside a value that is comma-joined, and nothing else is.
        (BASE, [("q", ["a&b=c+d#e"], "repeat")], f"{BASE}?q=a%26b%3Dc%2Bd%23e"),
        (BASE, [("q", ["a,b", "c"], "comma")], f"{BASE}?q=a%2Cb,c"),
        (BASE, [("q", ["a,b"], "repeat")], f"{BASE}?q=a,b"),
        (BASE, [("päth/x", ["é:@!$'()*;/?"], "repeat")], f"{BASE}?p%C3%A4th/x=%C3%A9:@!$'()*;/?"),
    ],
    ids=[
        "comma",
        "repeat",
        "existing-query",
        "fragment",
        "separators",
        "comma-in-value",
        "comma-literal",
        "unicode-and-kept",
    ],
)
def test_the_query_is_encoded_onto_the_url_in_order(
    url: str, query: list[tuple[str, list[str], str]], built: str
) -> None:
    assert with_query(url, query) == built


def test_the_built_url_is_the_request_the_coordinate_records() -> None:
    result, client = _fetch(
        {
            "url": BASE,
            "query": [
                {"name": "cve", "values": ["CVE-2021-44228", "CVE-2023-4863"], "join": "comma"},
                {"name": "limit", "values": ["100"]},
            ],
        }
    )
    assert result.status == "ok", result
    built = f"{BASE}?cve=CVE-2021-44228,CVE-2023-4863&limit=100"
    assert client.urls == [built]
    coordinate = result.output["coordinate"]
    assert coordinate["url"] == built
    assert coordinate["request_digest"] == "sha256:" + hashlib.sha256(built.encode()).hexdigest()


def test_the_same_parameters_build_the_same_request() -> None:
    query = [{"name": "cve", "values": ["CVE-1", "CVE-2"], "join": "comma"}]
    first, _ = _fetch({"url": BASE, "query": query})
    second, _ = _fetch({"url": BASE, "query": [dict(item) for item in query]})
    assert first.output["coordinate"] == second.output["coordinate"]
    other, _ = _fetch({"url": BASE, "query": [{**query[0], "values": ["CVE-2", "CVE-1"]}]})
    assert (
        other.output["coordinate"]["request_digest"] != first.output["coordinate"]["request_digest"]
    )


@pytest.mark.parametrize(
    "query",
    [
        {"cve": ["CVE-1"]},
        [{"name": "cve", "values": []}],
        [{"name": "cve", "values": "CVE-1"}],
        [{"name": "cve", "values": [1]}],
        [{"name": "", "values": ["x"]}],
        [{"name": "cve", "values": ["x"], "join": "pipe"}],
        [{"name": "cve", "values": ["x"]}, {"name": "cve", "values": ["y"]}],
        [{"name": "cve", "value": "x"}],
        [{"name": "cve", "values": ["x" * MAX_URL_LENGTH]}],
    ],
    ids=[
        "a-map",
        "no-values",
        "not-a-list",
        "not-strings",
        "no-name",
        "join",
        "named-twice",
        "undeclared-key",
        "too-long",
    ],
)
def test_a_query_the_adapter_cannot_build_is_declined(query: Any) -> None:
    result, client = _fetch({"url": BASE, "query": query})
    assert result.status == "refused" and result.refusal is not None
    assert result.refusal.code == PROVIDER_DECLINED
    assert client.urls == []
