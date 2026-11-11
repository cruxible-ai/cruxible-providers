"""``web.render``: a derive -- pure computation over a Capture this run fetched.

A derive declares ``effect: pure`` (D-R37 b5). Core hands it each ``capture<*>``
input as Capture wire (the exact stored bytes, their coordinate), and it must
compute only over that: no network, no clock, byte-identical output for
identical input, so that a re-run makes the same Capture and its Claims
re-observe. These tests hold the forms (``text``, ``json``), the coordinate of
the derived Capture, and the declines; the purity conformance test at the
repository root runs every pure operation twice with the network blocked.
"""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime
from typing import Any

import pytest
from cruxible_provider_runtime.acquisition import external_capture_result
from cruxible_provider_runtime.egress import EgressRecorder, no_network
from cruxible_provider_runtime.protocol import Budgets
from cruxible_provider_runtime.provider_api import ProviderRunContext
from cruxible_provider_web.engines import Extraction, ExtractionOutput
from cruxible_provider_web.operations import PROVIDER_DECLINED
from cruxible_provider_web.render import WebRender, document_json

BUDGETS = Budgets(wall_clock_seconds=30.0, output_bytes=1_000_000)

REQUESTED = "https://first.example/report"
SETTLED = "https://second.example/reports/latest"
OBSERVED = "2026-11-04T08:30:00Z"
PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>  Gauge
  report </title><base href="https://second.example/reports/"></head>
<body><nav><a href="/">Home</a> <a href="#main">Skip</a></nav>
<main id="main"><h1>Gauge report</h1>
<p>Newlyn reported a mean sea level of 3.214 metres over the last complete tidal
cycle. See the <a href="method.html#calibration">method</a>, the
<a href="https://data.example/gauges?id=7">readings</a> and the
<a href="method.html">method</a> again.</p>
<p><a href="mailto:ops@example.org">Mail</a> <a href="javascript:void(0)">Run</a></p>
</main></body></html>
"""


def _coordinate(**changes: Any) -> dict[str, Any]:
    return {
        "url": REQUESTED,
        "final_url": SETTLED,
        "status": 200,
        "content_type": "text/html; charset=utf-8",
        "request_digest": "sha256:" + hashlib.sha256(REQUESTED.encode()).hexdigest(),
        "source": "network",
        **changes,
    }


def _source(document: bytes = PAGE.encode(), **coordinate: Any) -> dict[str, Any]:
    """The fetched Capture as Core hands it to a derive: its Capture wire."""

    return external_capture_result(
        document,
        source_identity="web.response",
        coordinate_type="http-response-v1",
        coordinate=_coordinate(**coordinate),
        observed_at=datetime.fromisoformat(OBSERVED),
        selector_type="whole-response-v1",
    )


def _context(input_: dict[str, Any], *, egress: EgressRecorder | None = None) -> ProviderRunContext:
    return ProviderRunContext(
        run_id="run-derive",
        operation="web.render",
        operation_signature_digest="sha256:" + "aa" * 32,
        build_digest="sha256:" + "cc" * 32,
        implementation_digest="sha256:" + "bb" * 32,
        input=input_,
        coordinates={},
        budgets=BUDGETS,
        declared_endpoints=(),
        capture_contract=None,
        secrets={},
        egress=egress or EgressRecorder(),
    )


class _Extractor:
    """A fixed extractor, so the derive's own wiring is what is under test."""

    name = "fixed-extractor"

    def __init__(self, text: str = "Gauge report\n") -> None:
        self.text = text
        self.read: list[tuple[str, str, bool]] = []

    def extract(
        self,
        html: str,
        *,
        url: str,
        output: ExtractionOutput = "markdown",
        require_content: bool = True,
    ) -> Extraction:
        self.read.append((html, output, require_content))
        return Extraction(engine=self.name, kind=output, text=self.text)


def test_a_text_derive_extracts_the_cited_bytes_and_contacts_nothing() -> None:
    extractor = _Extractor("# Gauge report\n")
    recorder = EgressRecorder()
    result = WebRender(extractor=extractor)(
        _context({"form": "text", "source": _source()}, egress=recorder)
    )
    assert result.status == "ok", result
    assert extractor.read == [(PAGE, "markdown", True)]
    assert base64.b64decode(result.output["content_base64"]) == b"# Gauge report\n"
    coordinate = result.output["coordinate"]
    assert coordinate["renderer"] == "fixed-extractor"
    assert coordinate["content_type"] == "text/markdown; charset=utf-8"
    assert recorder.observed() == []


def test_a_json_derive_is_the_canonical_reading_of_the_page() -> None:
    extractor = _Extractor("Gauge report\nNewlyn reported 3.214 metres.")
    result = WebRender(extractor=extractor)(_context({"form": "json", "source": _source()}))
    assert result.status == "ok", result
    assert extractor.read == [(PAGE, "text", False)]
    stored = base64.b64decode(result.output["content_base64"])
    document = json.loads(stored)
    assert document == {
        "url": REQUESTED,
        "final_url": SETTLED,
        "title": "Gauge report",
        "text": "Gauge report\nNewlyn reported 3.214 metres.",
        # Resolved against <base href>, fragment dropped, first occurrence kept,
        # in document order; only http(s) targets.
        "links": [
            "https://second.example/",
            "https://second.example/reports/",
            "https://second.example/reports/method.html",
            "https://data.example/gauges?id=7",
        ],
    }
    # Canonical: sorted keys, no insignificant whitespace, UTF-8.
    assert stored == json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    assert result.output["coordinate"]["content_type"] == "application/json"


def test_a_page_without_a_title_or_main_content_still_has_a_reading() -> None:
    stored = document_json(
        "<html><body><a href='/x'>x</a></body></html>",
        url=REQUESTED,
        final_url=SETTLED,
        text="",
    )
    assert json.loads(stored) == {
        "url": REQUESTED,
        "final_url": SETTLED,
        "text": "",
        "links": ["https://second.example/x"],
    }


def test_the_derived_capture_cites_the_source_and_keeps_its_observation_time() -> None:
    source = _source(source="packaged-recording")
    result = WebRender(extractor=_Extractor())(_context({"form": "text", "source": source}))
    assert result.status == "ok", result
    output = result.output
    coordinate = output["coordinate"]
    assert coordinate["derived_from"] == "sha256:" + hashlib.sha256(PAGE.encode()).hexdigest()
    assert coordinate["derived_from"] == source["bytes_digest"]
    # The cited document's own coordinate is carried over, not re-derived.
    assert (coordinate["url"], coordinate["final_url"], coordinate["status"]) == (
        REQUESTED,
        SETTLED,
        200,
    )
    assert coordinate["source"] == "packaged-recording"
    assert output["source_identity"] == "web.rendered"
    # Never the clock: identical input gives an identical Capture (c6).
    assert output["observed_at"] == OBSERVED
    assert "headers" not in coordinate


def test_identical_input_gives_byte_identical_output_with_the_network_blocked() -> None:
    payload = {"form": "json", "source": _source(), "logical_source": "tide.page"}
    with no_network():
        first = WebRender(extractor=_Extractor())(_context(payload))
        second = WebRender(extractor=_Extractor())(_context(payload))
    assert first.status == second.status == "ok"
    assert first.output == second.output


def _altered(**changes: Any) -> dict[str, Any]:
    return {**_source(), **changes}


@pytest.mark.parametrize(
    "input_",
    [
        {"form": "dom", "source": _source()},
        {"form": "text"},
        {"form": "text", "source": "sha256:" + "a" * 64},
        {"form": "text", "source": _altered(content_base64="not base64!")},
        {"form": "text", "source": _altered(bytes_digest="sha256:" + "0" * 64)},
        {"form": "text", "source": _altered(observed_at=None)},
        {"form": "text", "source": _altered(coordinate_type="noop-recording-v1")},
        {"form": "text", "source": _altered(coordinate={"url": REQUESTED})},
        {"form": "text", "source": _source(b"")},
        {"form": "text", "source": _source(b'{"a": 1}', content_type="application/json")},
        {"form": "json", "source": _source(b"\xff\xfe<html>")},
        {"form": "text", "source": _source(), "logical_source": "Not A Name"},
        {"form": "text", "source": _source(), "max_bytes": 0},
    ],
    ids=[
        "dom-is-not-a-derive",
        "no-source",
        "a-digest-not-wire",
        "bad-base64",
        "digest-does-not-reproduce",
        "no-observation-time",
        "not-http",
        "bad-coordinate",
        "empty",
        "text-of-json",
        "not-utf8",
        "logical-source",
        "max-bytes",
    ],
)
def test_a_derive_declines_what_it_cannot_cite_or_read(input_: dict[str, Any]) -> None:
    result = WebRender(extractor=_Extractor())(_context(input_))
    assert result.status == "refused"
    assert result.refusal is not None and result.refusal.code == PROVIDER_DECLINED


def test_a_derived_document_heavier_than_max_bytes_is_declined() -> None:
    result = WebRender(extractor=_Extractor("x" * 64))(
        _context({"form": "text", "source": _source(), "max_bytes": 16})
    )
    assert result.status == "refused" and result.refusal is not None
    assert result.refusal.detail == {
        "url": SETTLED,
        "cap_bytes": 16,
        "observed_weight": "light",
    }
