"""A web.fetch Capture is the body the origin sent; its metadata is the coordinate."""

import base64
import hashlib

import pytest
from cruxible_provider_web.fetch import WebFetch

from .test_web_secrets import _CapturingClient, _context


@pytest.mark.parametrize(
    "media,body,expected",
    [
        ("application/json", b'{"severity": "critical"}', "json"),
        ("text/csv", b"id,severity\n1,critical\n", "csv"),
        ("application/octet-stream", b"\x00\xffraw", "bytes"),
        ("application/gzip", b"\x1f\x8b\x08\x00compressed", "bytes"),
    ],
)
def test_structured_and_binary_observations_are_the_exact_origin_bytes(media, body, expected):
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
    assert "source_effective_time" not in envelope
    material = base64.b64decode(envelope["content_base64"], validate=True)
    # Verbatim: no wrapper, no derivation, whatever the format.
    assert material == body
    assert len(material) == envelope["byte_length"]
    assert "sha256:" + hashlib.sha256(material).hexdigest() == envelope["bytes_digest"]
    assert envelope["coordinate"] == {
        "url": "https://example.test/resource",
        "final_url": "https://example.test/resource",
        "status": 200,
        "content_type": media,
        "request_digest": "sha256:" + hashlib.sha256(b"https://example.test/resource").hexdigest(),
        "source": "network",
    }


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


def test_header_credentials_are_typed_and_travel_only_as_a_header():
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
        ({}, "bad\r\nvalue"),
        ({"credential_header": "bad\nheader"}, "secret-value"),
    ]:
        refused = fetch(
            _context("web.fetch", input={**payload, **changes}, secrets={"api": secret})
        )
        assert refused.status == "refused"
    assert len(client.urls) == 1
