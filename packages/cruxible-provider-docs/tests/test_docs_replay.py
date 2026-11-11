"""Every packaged recording replays, labelled, and the engine-free path converts.

The runs are in-process against the same default engines the child process uses,
which for this plane means the engine-free converter and the replay of a recorded
engine response. What validates the recordings themselves is the engine-marked
lane, not this file.
"""

from __future__ import annotations

import base64
from typing import Any

import pytest
from cruxible_provider_docs.ocr import OPERATION as OCR_OPERATION
from cruxible_provider_docs.ocr import PaddleOcrExtract
from cruxible_provider_docs.recordings import PackagedDocument, load_recordings
from cruxible_provider_docs.to_markdown import OPERATION as MARKDOWN_OPERATION
from cruxible_provider_docs.to_markdown import DoclingToMarkdown
from cruxible_provider_runtime.egress import EgressRecorder
from cruxible_provider_runtime.protocol import Budgets
from cruxible_provider_runtime.provider_api import ProviderResult, ProviderRunContext

BUDGETS = Budgets(wall_clock_seconds=60.0, output_bytes=4_000_000)

CSV = b"reach,nitrate_mg_l,phosphate_mg_l\nUpper,4.1,0.08\nMiddle,6.7,0.11\nLower,9.2,0.19\n"

#: One case per packaged recording, plus the engine-free inline conversion.
CASES: dict[str, dict[str, Any]] = {
    "plain-text": {
        "operation": MARKDOWN_OPERATION,
        "input": {
            "source": {
                "kind": "inline",
                "filename": "reach-readings.csv",
                "media_type": "text/csv",
                "content_base64": base64.b64encode(CSV).decode("ascii"),
            },
            "layout": "tabular",
        },
        "engine": "plain-text",
        "derived_kind": "markdown",
        "contains": ["| reach | nitrate_mg_l | phosphate_mg_l |", "| Lower | 9.2 | 0.19 |"],
        "origin": "inline",
    },
    "html-brief": {
        "operation": MARKDOWN_OPERATION,
        "input": {"page_count": 1},
        "derived_kind": "markdown",
        "contains": ["Datum revision brief", "1998"],
        "excludes": ["Sign up to volunteer"],
    },
    "pdf-single": {
        "operation": MARKDOWN_OPERATION,
        "input": {"page_count": 1},
        "derived_kind": "markdown",
        "contains": ["Newlyn tide gauge recalibration", "3.8 millimetres"],
        "page_count": 1,
    },
    "pdf-short": {
        "operation": MARKDOWN_OPERATION,
        "input": {"page_count": 3},
        "derived_kind": "markdown",
        "contains": ["Sampling programme", "lower reach"],
        "page_count": 3,
    },
    "scan-clean": {
        "operation": OCR_OPERATION,
        "input": {"page_count": 1},
        "derived_kind": "ocr_text",
        "contains": ["NEWLYN TIDE GAUGE", "3.214"],
        "line_count": 3,
    },
    "scan-degraded": {
        "operation": OCR_OPERATION,
        "input": {"page_count": 1, "scan_quality": "degraded"},
        "derived_kind": "ocr_text",
        "contains": ["NEWLYN TIDE GAUGE"],
        "line_count": 3,
    },
}


def _input(case_id: str) -> dict[str, Any]:
    case = CASES[case_id]
    if case_id not in load_recordings():
        return dict(case["input"])
    document = load_recordings()[case_id].document
    source = {
        "kind": "packaged_fixture",
        "id": case_id,
        "filename": document.filename,
        "media_type": document.media_type,
    }
    return {"source": source, **case["input"]}


def _run(case_id: str) -> ProviderResult:
    operation = CASES[case_id]["operation"]
    provider: Any = DoclingToMarkdown() if operation == MARKDOWN_OPERATION else PaddleOcrExtract()
    context = ProviderRunContext(
        run_id=f"replay-{case_id}",
        operation=operation,
        operation_signature_digest="sha256:" + "22" * 32,
        build_digest="sha256:" + "33" * 32,
        implementation_digest="sha256:" + "11" * 32,
        input=_input(case_id),
        coordinates={},
        budgets=BUDGETS,
        declared_endpoints=(),
        capture_contract=None,
        secrets={},
        egress=EgressRecorder(),
    )
    return provider(context)


def test_every_packaged_recording_has_a_case() -> None:
    assert set(load_recordings()) <= set(CASES)
    for case_id, recording in load_recordings().items():
        assert recording.operation == CASES[case_id]["operation"]


@pytest.mark.parametrize("case_id", sorted(CASES))
def test_the_case_converts(case_id: str) -> None:
    case = CASES[case_id]
    result = _run(case_id)
    assert result.status == "ok", result.refusal or result.error
    assert result.output is not None
    document = result.output["document"]
    derived = result.output["derived"]
    assert set(result.output) == {"document", "derived"}
    assert document["origin"] == case.get("origin", "packaged-recording")
    assert document["sha256"].startswith("sha256:")
    assert derived["kind"] == case["derived_kind"]
    for needle in case["contains"]:
        assert needle in derived["text"], f"{needle!r} missing from the conversion"
    for needle in case.get("excludes", []):
        assert needle not in derived["text"], f"{needle!r} survived the conversion"
    if "page_count" in case:
        assert derived["page_count"] == case["page_count"]
    if "line_count" in case:
        assert sum(page["line_count"] for page in derived["pages"]) == case["line_count"]


@pytest.mark.parametrize("case_id", sorted(CASES))
def test_a_replay_never_claims_an_engine_ran(case_id: str) -> None:
    """The label is the whole difference between replay and fabrication."""

    result = _run(case_id)
    assert result.output is not None
    engine = result.output["derived"]["engine"]
    if case_id not in load_recordings():
        assert engine == "plain-text"
        assert not result.events
    else:
        assert engine == f"recorded:{case_id}"
        assert engine not in {"docling", "paddleocr"}
        assert any(event["kind"] == "recorded_engine_response" for event in result.events)


def test_a_recording_replays_only_for_its_own_operation() -> None:
    """An OCR reading is not a Markdown conversion, whatever the input says."""

    result = DoclingToMarkdown()(
        ProviderRunContext(
            run_id="replay-crossed",
            operation=MARKDOWN_OPERATION,
            operation_signature_digest="sha256:" + "22" * 32,
            build_digest="sha256:" + "33" * 32,
            implementation_digest="sha256:" + "11" * 32,
            input=_input("scan-clean"),
            coordinates={},
            budgets=BUDGETS,
            declared_endpoints=(),
            capture_contract=None,
            secrets={},
            egress=EgressRecorder(),
        )
    )
    assert result.status == "refused"
    assert result.refusal is not None
    assert result.refusal.code == "provider_declined"
    assert result.refusal.detail["recorded_for"] == OCR_OPERATION


def test_a_packaged_document_that_changed_under_its_recording_refuses() -> None:
    """The recording describes specific bytes, and says so with a digest."""

    document = PackagedDocument(
        filename="tide-gauge-report.pdf",
        media_type="application/pdf",
        path="tide-gauge-report.pdf",
        sha256="sha256:" + "ee" * 32,
    )
    with pytest.raises(ValueError, match="no longer there"):
        document.read()
