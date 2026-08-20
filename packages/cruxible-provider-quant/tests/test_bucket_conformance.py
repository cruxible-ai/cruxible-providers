"""One passing fixture per claimed bucket selector, and nothing claimed without one.

The rule the RP-0 contract states is that a claimed bucket without a passing
per-bucket conformance fixture refuses at registration. The registry enforces
the *presence* of a fixture id; only a test can enforce that the fixture exists
and passes. Both directions are asserted, because a fixture table that has drifted
ahead of the manifest is the same failure as one that has drifted behind it.

Buckets are measured, never claimed: each fixture's bucket is derived here by the
registered classifier from the actual payload, and then checked against the
selector — not read from the manifest, which is where the temptation would be.
"""

from __future__ import annotations

import pytest
from cruxible_provider_quant.interfaces import INTERFACE_IDS
from cruxible_provider_runtime.buckets import BucketSelector
from cruxible_provider_runtime.errors import RefusalCode, RefusalError
from cruxible_provider_runtime.manifest import ProviderManifest
from cruxible_provider_runtime.registry import StubRegistry, load_bucket_vocabulary

from .conftest import VOCAB_DIR, run_in_process
from .fixtures import FIXTURES, Fixture, fixtures_for

IDS = [fixture.fixture_id for fixture in FIXTURES]


def test_the_manifest_and_the_fixture_table_agree_exactly(manifest: ProviderManifest) -> None:
    declared: dict[str, tuple[str, str]] = {}
    for implementation in manifest.implementations:
        for selector in implementation.declared_input_buckets:
            declared[implementation.bucket_conformance[selector]] = (
                implementation.interface_id,
                selector,
            )
    offered = {fixture.fixture_id: (fixture.interface_id, fixture.selector) for fixture in FIXTURES}
    assert offered == declared


def test_every_interface_claims_at_least_one_bucket(manifest: ProviderManifest) -> None:
    assert {impl.interface_id for impl in manifest.implementations} == set(INTERFACE_IDS)
    for implementation in manifest.implementations:
        assert implementation.declared_input_buckets
        assert fixtures_for(implementation.interface_id)


@pytest.mark.parametrize("fixture", FIXTURES, ids=IDS)
def test_the_fixture_classifies_into_a_bucket_its_selector_matches(fixture: Fixture) -> None:
    vocabulary = load_bucket_vocabulary(VOCAB_DIR / f"{fixture.interface_id}.yaml")
    registry = StubRegistry()
    from .conftest import seed_interfaces

    seed_interfaces(registry)
    derived = registry.classify(fixture.interface_id, fixture.payload)
    assert derived == fixture.expected_bucket
    assert BucketSelector.parse(fixture.selector, vocabulary).matches(derived)


@pytest.mark.parametrize("fixture", FIXTURES, ids=IDS)
def test_the_fixture_is_admitted_and_answered(fixture: Fixture) -> None:
    """The half a registry cannot check: the claimed bucket actually works."""

    result = run_in_process(fixture.interface_id, fixture.payload)
    assert result.status == "ok", result.refusal or result.error
    assert result.output is not None
    assert result.output


def test_a_wildcard_face_covers_its_whole_dimension() -> None:
    """``reduction_kind=*`` has to mean all three kinds, not just the fixtured one.

    A wildcard is the cheap way to claim a face of a large cube, and the cheap
    way to over-claim. The fixture for this selector exercises the grouped kind;
    this exercises the other two, so the wildcard is discharged rather than
    assumed.
    """

    rows = [{"a": float(index), "b": index % 4} for index in range(120)]
    windowed = run_in_process(
        "calc.reduce",
        {
            "rows": rows,
            "window": {"column": "a", "order_by": "a", "function": "sum", "size": 3},
        },
    )
    assert windowed.status == "ok", windowed.refusal or windowed.error
    assert windowed.output is not None
    assert windowed.output["reduction_kind"] == "windowed"

    scalar = run_in_process(
        "calc.reduce",
        {"rows": rows, "aggregations": [{"column": "a", "function": "sum"}]},
    )
    assert scalar.status == "ok", scalar.refusal or scalar.error
    assert scalar.output is not None
    assert scalar.output["reduction_kind"] == "scalar_aggregate"


def test_an_input_outside_every_claim_refuses_before_the_engine_runs() -> None:
    """``ts.anomaly`` does not claim categorically-encoded values, and says so.

    The refusal happens at admission, so the engine is never reached — which is
    what makes an unclaimed bucket a governance answer rather than a runtime
    failure. That the classifier's own declaration of the domain is what triggers
    it matters: the domain is measured from the payload, not asserted by the
    manifest.
    """

    fixture = next(f for f in FIXTURES if f.fixture_id == "quant-anomaly-counts")
    payload = {**fixture.payload, "value_kind": "categorical_encoded"}
    with pytest.raises(RefusalError) as exc:
        run_in_process("ts.anomaly", payload)
    assert exc.value.code is RefusalCode.UNCLAIMED_BUCKET
    assert exc.value.refusal.detail["bucket"].split(";")[3] == "domain_class=categorical_encoded"


def test_a_trained_linkage_input_refuses_as_unclaimed() -> None:
    """The bucket law doing the job it exists for.

    This implementation is told its m/u probabilities. A caller who has confirmed
    matches is asking for a trained linker, and gets a refusal naming the bucket
    rather than an untrained answer that silently ignored the labels — which is
    exactly the seam a narrow-ML implementation of this slot will fill.
    """

    fixture = next(f for f in FIXTURES if f.fixture_id == "quant-linkage-strong-blocking")
    payload = {
        **fixture.payload,
        "known_matches": [{"left_id": "r0000", "right_id": "r0001"}],
    }
    with pytest.raises(RefusalError) as exc:
        run_in_process("match.record", payload)
    assert exc.value.code is RefusalCode.UNCLAIMED_BUCKET
    assert "label_availability=partial" in exc.value.refusal.detail["bucket"]


def test_an_unclassifiable_input_refuses() -> None:
    with pytest.raises(RefusalError) as exc:
        run_in_process("ts.forecast", {"series": [], "horizon": 3})
    assert exc.value.code is RefusalCode.UNCLASSIFIED_INPUT
