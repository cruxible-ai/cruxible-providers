"""The acquisition envelope keeps the observation separate from extraction."""

import base64
import hashlib
import json

import pytest
from cruxible_provider_runtime.errors import RefusalCode
from cruxible_provider_web.fetch import WebFetch
from cruxible_provider_web.interfaces import MAX_RESPONSE_BYTES, classify_web_fetch

from .test_web_secrets import _CapturingClient, _context


@pytest.mark.parametrize(
    "media,body,expected",
    [
        ("application/json", b'{"severity": "critical"}', "json"),
        ("text/csv", b"id,severity\n1,critical\n", "csv"),
    ],
)
def test_structured_observations_preserve_exact_origin_bytes(media, body, expected):
    client = _CapturingClient(body, media)
    fetch = WebFetch(client_factory=lambda recorder, **kwargs: client)
    result = fetch(
        _context(
            "web.fetch",
            input={
                "url": "https://example.test/resource",
                "expected_format": expected,
            },
        )
    )
    assert result.status == "ok", result
    envelope = result.output
    assert envelope["tag"] == "playbill-provider-result-to-external-capture-v1"
    assert envelope["replayability"] == "attested_only"
    assert envelope["coordinate_type"] == "http-response-v1"
    material = base64.b64decode(envelope["content_base64"], validate=True)
    assert len(material) == envelope["byte_length"]
    assert "sha256:" + hashlib.sha256(material).hexdigest() == envelope["bytes_digest"]
    observation = json.loads(material)
    assert base64.b64decode(observation["retrieved"]["body_base64"]) == body
    assert observation["derived"]["engine"] != "trafilatura"


@pytest.mark.parametrize(
    "fields",
    [
        {"url": "https://example.test/resource", "expected_format": "bytes"},
        {"url": "https://example.test/report.pdf"},
        {"url": "https://example.test/photo.JPG"},
        {"url": "https://example.test/dump.tar"},
        {"url": "https://example.test/sheet.xlsx", "expected_format": "auto"},
    ],
)
def test_binary_resources_are_declined_before_contact(fields):
    client = _CapturingClient(b"\x00\xffraw", "application/octet-stream")
    result = WebFetch(client_factory=lambda recorder, **kwargs: client)(
        _context("web.fetch", input=fields)
    )
    assert result.status == "refused"
    assert result.refusal.code is RefusalCode.PROVIDER_DECLINED
    assert result.refusal.detail["reason"] == "binary_resource"
    assert client.urls == []


def test_html_heavier_than_two_mebibytes_is_declined_and_structured_is_not():
    client = _CapturingClient(b'{"ok":true}', "application/json")
    fetch = WebFetch(client_factory=lambda recorder, **kwargs: client)
    heavy = 2 * 1024 * 1024 + 1
    for url in ("https://example.test/page", "https://example.test/page.html"):
        refused = fetch(_context("web.fetch", input={"url": url, "max_bytes": heavy}))
        assert refused.status == "refused"
        assert refused.refusal.code is RefusalCode.PROVIDER_DECLINED
        assert refused.refusal.detail == {
            "reason": "heavy_page",
            "url": url,
            "max_bytes": heavy,
            "ceiling_bytes": 2 * 1024 * 1024,
        }
    assert client.urls == []
    medium = fetch(
        _context(
            "web.fetch",
            input={"url": "https://example.test/page", "max_bytes": 2 * 1024 * 1024},
        )
    )
    assert medium.status != "refused"
    structured = fetch(
        _context(
            "web.fetch",
            input={"url": "https://example.test/api/v1/data", "max_bytes": MAX_RESPONSE_BYTES},
        )
    )
    assert structured.status == "ok", structured


@pytest.mark.parametrize(
    "media,body,expected",
    [
        ("text/html", b"<html>login</html>", "json"),
        ("application/json", b"{broken}", "json"),
        ("application/json", b'{"x": NaN}', "json"),
        ("text/csv", b"a,b\n1,2,3\n", "csv"),
    ],
)
def test_bad_or_wrong_format_cannot_become_an_observation(media, body, expected):
    client = _CapturingClient(body, media)
    result = WebFetch(client_factory=lambda recorder, **kwargs: client)(
        _context("web.fetch", input={"url": "https://example.test", "expected_format": expected})
    )
    assert result.status == "error"
    assert result.output is None


def test_heavy_api_is_classified_but_unbounded_request_is_not():
    assert classify_web_fetch(
        {
            "url": "https://example.test/data",
            "expected_format": "json",
            "max_bytes": 8 * 1024 * 1024,
        }
    ) == {
        "source_kind": "api_json",
        "access": "public",
        "page_weight": "heavy",
    }
    assert (
        classify_web_fetch({"url": "https://example.test", "max_bytes": MAX_RESPONSE_BYTES + 1})
        is None
    )


def test_header_credentials_are_typed_and_validated():
    client = _CapturingClient(b'{"ok":true}', "application/json")
    fetch = WebFetch(client_factory=lambda recorder, **kwargs: client)
    payload = {
        "url": "https://example.test",
        "expected_format": "json",
        "credential_ref": "api",
        "credential_header": "x-api-key",
    }
    result = fetch(_context("web.fetch", input=payload, secrets={"api": "secret-value"}))
    assert result.status == "ok"
    assert client.headers[0]["x-api-key"] == "secret-value"
    assert "secret-value" not in repr(result)
    for changes, secret in [
        ({"render": True}, "secret-value"),
        ({}, "bad\r\nvalue"),
        ({"credential_header": "bad\nheader"}, "secret-value"),
    ]:
        refused = fetch(
            _context("web.fetch", input={**payload, **changes}, secrets={"api": secret})
        )
        assert refused.status == "refused"
    assert len(client.urls) == 1
