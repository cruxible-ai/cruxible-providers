"""Per-bucket conformance: every claimed selector, exercised and passing.

Each fixture states three things and each is checked: that its input **measures**
into the bucket it names, derived by the registered classifier; that the bucket
is inside the selector the manifest claims; and that running the adapter over the
packaged document produces what the fixture says.

The runs are in-process against the same default engines the child process uses,
which for this plane means the engine-free converter and the replay of a recorded
engine response. What validates the recordings themselves is the engine-marked
lane, not this file.
"""

from __future__ import annotations

from typing import Any

import pytest
from cruxible_provider_docs.interfaces import (
    MARKDOWN_INTERFACE_ID,
    OCR_INTERFACE_ID,
    registrations,
)
from cruxible_provider_docs.ocr import PaddleOcrExtract
from cruxible_provider_docs.recordings import BucketFixture, load_fixtures, load_recordings
from cruxible_provider_docs.to_markdown import DoclingToMarkdown
from cruxible_provider_runtime.buckets import BucketSelector
from cruxible_provider_runtime.egress import EgressRecorder
from cruxible_provider_runtime.manifest import ProviderManifest
from cruxible_provider_runtime.protocol import Budgets
from cruxible_provider_runtime.provider_api import ProviderResult, ProviderRunContext
from cruxible_provider_runtime.registry import StubRegistry

BUDGETS = Budgets(wall_clock_seconds=60.0, output_bytes=4_000_000)
FIXTURES = sorted(load_fixtures().values(), key=lambda fixture: fixture.id)


def _registry() -> StubRegistry:
    stub = StubRegistry()
    for registration in registrations():
        stub.register_interface(registration)
    return stub


def _run(fixture: BucketFixture, manifest: ProviderManifest) -> ProviderResult:
    implementation = manifest.implementation(fixture.interface_id)
    provider: Any = (
        DoclingToMarkdown() if fixture.interface_id == MARKDOWN_INTERFACE_ID else PaddleOcrExtract()
    )
    context = ProviderRunContext(
        run_id=f"fixture-{fixture.id}",
        interface_id=fixture.interface_id,
        interface_digest=implementation.interface_digest,
        implementation_digest="sha256:" + "11" * 32,
        input_bucket=fixture.bucket_id,
        input=fixture.input,
        coordinates={},
        budgets=BUDGETS,
        declared_endpoints=implementation.declared_endpoints,
        capture_contract=implementation.capture_contract_families[0],
        secrets={},
        egress=EgressRecorder(),
    )
    return provider(context)


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda fixture: fixture.id)
def test_a_fixture_that_names_a_recording_names_one_that_exists(
    fixture: BucketFixture,
) -> None:
    if fixture.recording_id is None:
        # The engine-free bucket, which has no engine response to record.
        assert fixture.input["source"]["kind"] == "inline"
        return
    assert fixture.recording_id in load_recordings()


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda fixture: fixture.id)
def test_the_fixture_input_measures_into_the_bucket_it_names(fixture: BucketFixture) -> None:
    """Derived by the registered classifier, not read off the fixture."""

    assert _registry().classify(fixture.interface_id, fixture.input) == fixture.bucket_id


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda fixture: fixture.id)
def test_the_named_bucket_is_inside_the_claimed_selector(fixture: BucketFixture) -> None:
    registration = _registry().interface(fixture.interface_id)
    selector = BucketSelector.parse(fixture.bucket_selector, registration.bucket_vocabulary)
    assert selector.matches(fixture.bucket_id)


def test_every_claimed_selector_has_a_fixture(manifest: ProviderManifest) -> None:
    fixtures = load_fixtures()
    for implementation in manifest.implementations:
        for selector, fixture_id in implementation.bucket_conformance.items():
            assert fixture_id in fixtures, f"{selector!r} claims fixture {fixture_id!r}"
            assert fixtures[fixture_id].bucket_selector == selector
            assert fixtures[fixture_id].interface_id == implementation.interface_id
        assert set(implementation.bucket_conformance) == set(implementation.declared_input_buckets)


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda fixture: fixture.id)
def test_the_fixture_passes(fixture: BucketFixture, manifest: ProviderManifest) -> None:
    result = _run(fixture, manifest)
    expect = fixture.expect
    assert result.status == expect["status"], result.refusal or result.error
    assert result.output is not None
    document = result.output["document"]
    derived = result.output["derived"]

    assert document["origin"] == expect["origin"]
    assert document["sha256"].startswith("sha256:")
    assert derived["engine"] == expect["engine"]
    assert derived["kind"] == expect["derived_kind"]
    for needle in expect.get("derived_contains", []):
        assert needle in derived["text"], f"{needle!r} missing from the conversion"
    for needle in expect.get("derived_excludes", []):
        assert needle not in derived["text"], f"{needle!r} survived the conversion"
    if "page_count" in expect:
        assert derived["page_count"] == expect["page_count"]
    if "line_count" in expect:
        assert sum(page["line_count"] for page in derived["pages"]) == expect["line_count"]


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda fixture: fixture.id)
def test_a_replayed_fixture_never_claims_an_engine_ran(
    fixture: BucketFixture, manifest: ProviderManifest
) -> None:
    """The label is the whole difference between replay and fabrication."""

    result = _run(fixture, manifest)
    assert result.output is not None
    engine = result.output["derived"]["engine"]
    if fixture.recording_id is None:
        assert engine == "plain-text"
        assert not result.events
    else:
        assert engine == f"recorded:{fixture.recording_id}"
        assert engine not in {"docling", "paddleocr"}
        assert any(event["kind"] == "recorded_engine_response" for event in result.events)


def test_a_packaged_document_that_changed_under_its_recording_refuses() -> None:
    """The recording describes specific bytes, and says so with a digest."""

    from cruxible_provider_docs.recordings import PackagedDocument

    document = PackagedDocument(
        filename="tide-gauge-report.pdf",
        media_type="application/pdf",
        path="tide-gauge-report.pdf",
        sha256="sha256:" + "ee" * 32,
    )
    with pytest.raises(ValueError, match="no longer there"):
        document.read()


def test_the_fixtures_cover_both_interfaces() -> None:
    assert {fixture.interface_id for fixture in FIXTURES} == {
        MARKDOWN_INTERFACE_ID,
        OCR_INTERFACE_ID,
    }
