"""Per-bucket conformance: every claimed selector, exercised and passing.

Each fixture states three things and each is checked: that its input **measures**
into the bucket it names, derived by the registered classifier; that the bucket
is inside the selector the manifest claims; and that running the adapter over
the exact input produces the body whose digest the fixture pins.

The fixtures are generated, not hand-written, and the last test here asserts that
regenerating them is a byte-identical no-op -- which is what makes tens of
kilobytes of committed base64 reviewable: the review is of the generator.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from cruxible_provider_runtime.buckets import BucketSelector
from cruxible_provider_runtime.canonical import canonical_json, sha256_hex
from cruxible_provider_runtime.egress import EgressRecorder
from cruxible_provider_runtime.manifest import ProviderManifest
from cruxible_provider_runtime.protocol import Budgets
from cruxible_provider_runtime.provider_api import ProviderResult, ProviderRunContext
from cruxible_provider_runtime.registry import StubRegistry
from cruxible_provider_workspace.file import WorkspaceFile
from cruxible_provider_workspace.fixtures import FIXTURES_DIR, BucketFixture, load_fixtures
from cruxible_provider_workspace.interface import registration

from . import fixture_generation
from .conftest import INTERFACE_ID

BUDGETS = Budgets(wall_clock_seconds=60.0, output_bytes=8_000_000)
FIXTURES = sorted(load_fixtures().values(), key=lambda fixture: fixture.id)


def _registry() -> StubRegistry:
    stub = StubRegistry()
    stub.register_interface(registration())
    return stub


def _run(fixture: BucketFixture, manifest: ProviderManifest) -> ProviderResult:
    implementation = manifest.implementation(fixture.interface_id)
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
    return WorkspaceFile()(context)


def test_there_is_one_fixture_per_claimed_bucket() -> None:
    assert len(FIXTURES) == 6


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda fixture: fixture.id)
def test_the_fixture_input_measures_into_the_bucket_it_names(fixture: BucketFixture) -> None:
    """Derived by the registered classifier, not read off the fixture."""

    assert _registry().classify(fixture.interface_id, fixture.input) == fixture.bucket_id


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda fixture: fixture.id)
def test_the_named_bucket_is_inside_the_claimed_selector(fixture: BucketFixture) -> None:
    registration_ = _registry().interface(fixture.interface_id)
    selector = BucketSelector.parse(fixture.bucket_selector, registration_.bucket_vocabulary)
    assert selector.matches(fixture.bucket_id)


def test_every_claimed_selector_has_a_fixture(manifest: ProviderManifest) -> None:
    fixtures = load_fixtures()
    implementation = manifest.implementation(INTERFACE_ID)
    for selector, fixture_id in implementation.bucket_conformance.items():
        assert fixture_id in fixtures, f"{selector!r} claims fixture {fixture_id!r}"
        assert fixtures[fixture_id].bucket_selector == selector
        assert fixtures[fixture_id].interface_id == implementation.interface_id
    assert set(implementation.bucket_conformance) == set(implementation.declared_input_buckets)
    assert set(implementation.bucket_conformance.values()) == set(fixtures)


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda fixture: fixture.id)
def test_the_fixture_input_is_internally_consistent(fixture: BucketFixture) -> None:
    """The declared length and digest describe the payload the fixture carries."""

    import base64

    data = base64.b64decode(fixture.input["bytes"], validate=True)
    assert len(data) == fixture.input["byte_length"]
    assert "sha256:" + hashlib.sha256(data).hexdigest() == fixture.input["bytes_digest"]
    assert fixture.expect["bytes_digest"] == fixture.input["bytes_digest"]


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda fixture: fixture.id)
def test_the_fixture_passes(fixture: BucketFixture, manifest: ProviderManifest) -> None:
    result = _run(fixture, manifest)
    expect = fixture.expect
    assert result.status == expect["status"], result.refusal or result.error
    assert result.output is not None
    assert result.output["input_bucket"] == fixture.bucket_id
    assert result.output["source"] == {
        "logical_source": fixture.input["logical_source"],
        "commitment_digest": fixture.input["commitment_digest"],
        "bytes_digest": expect["bytes_digest"],
        "byte_length": fixture.input["byte_length"],
    }
    content = result.output["content"]
    assert content["kind"] == expect["kind"]
    if expect["kind"] == "text":
        for key in ("bom", "newline", "trailing_newline", "line_count", "character_count"):
            assert content[key] == expect[key], key
        assert content["lines"][0] == expect["first_line"]
        assert content["lines"][-1] == expect["last_line"]
    else:
        assert content["bytes"] == fixture.input["bytes"]
        assert content["byte_length"] == fixture.input["byte_length"]
    assert not result.events
    assert sha256_hex(canonical_json(result.output)) == expect["output_digest"]


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda fixture: fixture.id)
def test_the_fixture_replays_to_the_same_body(
    fixture: BucketFixture, manifest: ProviderManifest
) -> None:
    """Pure: two runs over the same input produce byte-identical bodies."""

    first = _run(fixture, manifest)
    second = _run(fixture, manifest)
    assert canonical_json(first.output) == canonical_json(second.output)


@pytest.mark.parametrize("fixture_id", sorted(fixture_generation.PAYLOADS))
def test_regenerating_a_fixture_is_a_byte_identical_no_op(fixture_id: str) -> None:
    """The generator is the review surface for the committed blobs."""

    committed = (FIXTURES_DIR / f"{fixture_id}.json").read_bytes()
    assert fixture_generation.render(fixture_id) == committed


def test_the_generator_and_the_fixtures_directory_agree_on_the_set() -> None:
    assert {path.stem for path in Path(FIXTURES_DIR).glob("*.json")} == set(
        fixture_generation.PAYLOADS
    )
