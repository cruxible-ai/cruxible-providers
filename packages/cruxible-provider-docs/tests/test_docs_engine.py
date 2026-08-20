"""The opt-in real-engine lane for the document plane.

``pytest -m engine``. Never part of the default run and never part of the default
CI lane: everything here needs a per-engine extra installed and, on first use, a
model download.

This lane is what makes the recordings honest. The default lane replays a
recorded engine response for a document shipped in the distribution; these tests
run the **real** engine over those same bytes and assert it reproduces the
recording. A recording that has drifted away from what the engine does fails
here, and nowhere else.

Every test skips with a reason when its engine is absent, so
``pytest -m engine --collect-only`` collects cleanly on a machine with no
engines at all.
"""

from __future__ import annotations

import re

import pytest
from cruxible_provider_docs.engines import DoclingMarkdownEngine, PaddleOcrEngine
from cruxible_provider_docs.recordings import load_document, load_recordings

pytestmark = pytest.mark.engine

MARKDOWN_RECORDINGS = sorted(
    recording.id
    for recording in load_recordings().values()
    if recording.interface_id == "doc.to_markdown"
)
OCR_RECORDINGS = sorted(
    recording.id
    for recording in load_recordings().values()
    if recording.interface_id == "ocr.extract"
)


def _normalise(text: str) -> str:
    """Compare on words, not on whitespace or heading punctuation.

    A conversion engine is entitled to choose its own heading level and its own
    line wrapping; what a recording claims is the *content*. Comparing raw
    strings would turn a formatting choice into a failure and train everyone to
    regenerate recordings without reading them.
    """

    return " ".join(re.sub(r"[#*_`|>-]+", " ", text).lower().split())


@pytest.fixture()
def docling_available() -> None:
    pytest.importorskip("docling", reason="the docling extra is not installed")


@pytest.fixture()
def paddleocr_available() -> None:
    pytest.importorskip("paddleocr", reason="the paddleocr extra is not installed")


@pytest.mark.usefixtures("docling_available")
@pytest.mark.parametrize("recording_id", MARKDOWN_RECORDINGS)
def test_the_conversion_recording_still_describes_what_docling_does(recording_id: str) -> None:
    recording, data = load_document(recording_id)
    result = DoclingMarkdownEngine().convert(
        data, filename=recording.document.filename, media_type=recording.document.media_type
    )

    assert result.engine == "docling"
    assert result.page_count == recording.output["page_count"]
    produced = _normalise(result.markdown)
    for sentence in _sentences(recording.output["markdown"]):
        assert _normalise(sentence) in produced, (
            f"the recording for {recording_id!r} claims text the engine no longer produces: "
            f"{sentence!r}"
        )


@pytest.mark.usefixtures("paddleocr_available")
@pytest.mark.parametrize("recording_id", OCR_RECORDINGS)
def test_the_reading_recording_still_describes_what_paddleocr_does(recording_id: str) -> None:
    recording, data = load_document(recording_id)
    result = PaddleOcrEngine().read(data, filename=recording.document.filename, language="en")

    assert result.engine == "paddleocr"
    produced = _normalise(result.text)
    for line in recording.output["pages"][0]["text"].splitlines():
        assert _normalise(line) in produced, (
            f"the recording for {recording_id!r} claims a line the engine no longer reads: {line!r}"
        )


def _sentences(markdown: str) -> list[str]:
    return [line.strip() for line in markdown.splitlines() if line.strip()]
