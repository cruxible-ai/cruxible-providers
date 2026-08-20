"""Per-bucket conformance: every claimed selector, exercised and passing.

A claimed bucket with no passing fixture refuses at registration, which makes
this file the thing that keeps the manifest's claims honest rather than
aspirational. Each fixture states three things and each is checked here:

* the fixture's input **measures** into the bucket the fixture names — derived
  by the registered classifier, never read from the fixture;
* that bucket is inside the selector the manifest claims;
* running the adapter over the recorded exchange produces what the fixture says.

The runs here are in-process, against the same default engines and the same
recorded transport the child process uses. The process, protocol, and budget
paths are covered separately in the full-loop suite; what is under test here is
the adapter's behaviour per bucket.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
from cruxible_provider_runtime.buckets import BucketSelector
from cruxible_provider_runtime.egress import EgressRecorder
from cruxible_provider_runtime.manifest import ProviderManifest
from cruxible_provider_runtime.protocol import Budgets
from cruxible_provider_runtime.provider_api import ProviderResult, ProviderRunContext
from cruxible_provider_runtime.registry import StubRegistry
from cruxible_provider_web.fetch import WebFetch
from cruxible_provider_web.interfaces import (
    FETCH_INTERFACE_ID,
    SEARCH_INTERFACE_ID,
    registrations,
)
from cruxible_provider_web.recordings import BucketFixture, load_fixtures, load_recordings
from cruxible_provider_web.search import SearxngSearch

BUDGETS = Budgets(wall_clock_seconds=60.0, output_bytes=4_000_000)
FIXTURES = sorted(load_fixtures().values(), key=lambda fixture: fixture.id)


def _registry() -> StubRegistry:
    stub = StubRegistry()
    for registration in registrations():
        stub.register_interface(registration)
    return stub


def _run(fixture: BucketFixture, manifest: ProviderManifest) -> ProviderResult:
    implementation = manifest.implementation(fixture.interface_id)
    provider: Any = WebFetch() if fixture.interface_id == FETCH_INTERFACE_ID else SearxngSearch()
    context = ProviderRunContext(
        run_id=f"fixture-{fixture.id}",
        interface_id=fixture.interface_id,
        interface_digest=implementation.interface_digest,
        implementation_digest="sha256:" + "11" * 32,
        input_bucket=fixture.bucket_id,
        input=fixture.input,
        coordinates=fixture.coordinates,
        budgets=BUDGETS,
        declared_endpoints=implementation.declared_endpoints,
        capture_contract=implementation.capture_contract_families[0],
        secrets=fixture.secrets,
        egress=EgressRecorder(),
    )
    return provider(context)


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda fixture: fixture.id)
def test_every_fixture_references_a_recording(fixture: BucketFixture) -> None:
    assert fixture.recording_id in load_recordings()


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda fixture: fixture.id)
def test_the_fixture_input_measures_into_the_bucket_it_names(fixture: BucketFixture) -> None:
    """Derived by the registered classifier, not read off the fixture."""

    registry = _registry()
    assert registry.classify(fixture.interface_id, fixture.input) == fixture.bucket_id


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda fixture: fixture.id)
def test_the_named_bucket_is_inside_the_claimed_selector(fixture: BucketFixture) -> None:
    registration = _registry().interface(fixture.interface_id)
    selector = BucketSelector.parse(fixture.bucket_selector, registration.bucket_vocabulary)
    assert selector.matches(fixture.bucket_id)


def test_every_claimed_selector_has_a_fixture(manifest: ProviderManifest) -> None:
    """The registration-time rule, checked against the files that must back it."""

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
    retrieved: Mapping[str, Any] = result.output["retrieved"]
    derived: Mapping[str, Any] = result.output["derived"]

    if "status_code" in expect:
        assert retrieved["status_code"] == expect["status_code"]
    assert retrieved["source"] == expect["source"]
    if "renderer" in expect:
        assert retrieved["renderer"] == expect["renderer"]
    if "derived_kind" in expect:
        assert derived["kind"] == expect["derived_kind"]
    if "derived_engine" in expect:
        assert derived["engine"] == expect["derived_engine"]
    for needle in expect.get("derived_contains", []):
        assert needle in derived["text"], f"{needle!r} missing from the extracted text"
    for needle in expect.get("derived_excludes", []):
        assert needle not in derived["text"], f"{needle!r} survived extraction"
    for key, value in expect.get("metadata", {}).items():
        assert derived["metadata"][key] == value
    if "result_count" in expect:
        assert retrieved["result_count"] == expect["result_count"]
    if "returned" in expect:
        assert len(derived["results"]) == expect["returned"]
    if "dropped_by_recency" in expect:
        assert derived["dropped_by_recency"] == expect["dropped_by_recency"]
    if "time_range" in expect:
        assert retrieved["parameters"]["time_range"] == expect["time_range"]


def test_the_fixtures_cover_both_interfaces() -> None:
    """A plane package's fixtures must not quietly cover only the easy interface."""

    covered = {fixture.interface_id for fixture in FIXTURES}
    assert covered == {FETCH_INTERFACE_ID, SEARCH_INTERFACE_ID}
