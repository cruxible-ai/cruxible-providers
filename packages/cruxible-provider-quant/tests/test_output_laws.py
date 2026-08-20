"""The standing product laws, asserted over every fixture's real output.

Three of them, and each fails in the direction that matters: a new
implementation that quietly adds a confidence field, grades its own output, or
starts asking for a credential breaks the build rather than shipping.

1. **No generic confidence score.** Scanned recursively over every output, keys
   and values alike, because "confidence" arriving as a string in a
   ``score_kind`` would be the same law broken by a different route.
2. **Grade is the CaptureContract's.** No ``grade`` key anywhere, and every
   declared capture-contract family is a ``derived`` family. A forecast, an
   anomaly flag, a rank, and a linkage score are derived readings; a provider
   that graded itself would be self-certifying.
3. **No secrets, no endpoints.** Pure computation: nothing here needs a
   credential or a network, so nothing here may quietly acquire one.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

import pytest
from cruxible_provider_quant.interfaces import INTERFACE_IDS
from cruxible_provider_runtime.canonical import canonical_json
from cruxible_provider_runtime.manifest import ProviderManifest

from .conftest import run_in_process
from .fixtures import FIXTURES

IDS = [fixture.fixture_id for fixture in FIXTURES]
SOURCE_DIR = Path(__file__).resolve().parent.parent / "src" / "cruxible_provider_quant"

BANNED_KEYS = frozenset(
    {
        "confidence",
        "confidence_score",
        "certainty",
        "belief",
        "credence",
        "trust",
        "trust_score",
        "reliability_score",
        "grade",
    }
)
"""What a unified belief number gets called when someone adds one.

``reliability`` is deliberately absent: it is a named component of the Brier
decomposition with a definition, which is the opposite of the thing being
banned. ``reliability_score`` is present, because that is what the banned thing
would be called if it arrived wearing the same coat.
"""


def _keys(document: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(document, dict):
        for key, value in document.items():
            found.add(str(key))
            found |= _keys(value)
    elif isinstance(document, list):
        for item in document:
            found |= _keys(item)
    return found


@pytest.mark.parametrize("fixture", FIXTURES, ids=IDS)
def test_no_output_carries_a_generic_confidence_or_a_grade(fixture: object) -> None:
    result = run_in_process(
        fixture.interface_id,  # type: ignore[attr-defined]
        fixture.payload,  # type: ignore[attr-defined]
    )
    assert result.status == "ok"
    offending = _keys(result.output) & BANNED_KEYS
    assert not offending, f"{fixture.fixture_id} emits {sorted(offending)}"  # type: ignore[attr-defined]
    rendered = canonical_json(result.output).decode("utf-8").lower()
    # Word boundaries, not substrings. ``uncertainty`` is a named component of
    # the Brier decomposition with a definition, and a check that could not tell
    # it apart from ``certainty`` would be banning arithmetic rather than a
    # habit.
    for word in BANNED_KEYS:
        assert not re.search(rf"\b{word}\b", rendered), (
            f"{fixture.fixture_id} mentions {word!r} in its output"  # type: ignore[attr-defined]
        )


def test_no_source_declares_a_banned_field() -> None:
    """Parsed, not grepped.

    The prohibition has to be explainable in the docstrings that explain it, so
    scanning for the *word* would forbid the documentation along with the thing
    documented. The syntax tree does not have that problem: what is checked is
    the shape a field actually takes — a string key in a dict literal, an
    attribute name, a keyword argument, a variable — and prose is invisible to
    it.
    """

    for path in sorted(SOURCE_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        named: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                named |= {
                    key.value.lower()
                    for key in node.keys
                    if isinstance(key, ast.Constant) and isinstance(key.value, str)
                }
            elif isinstance(node, ast.Attribute):
                named.add(node.attr.lower())
            elif isinstance(node, ast.keyword) and node.arg:
                named.add(node.arg.lower())
            elif isinstance(node, ast.Name):
                named.add(node.id.lower())
        offending = named & BANNED_KEYS
        assert not offending, f"{path.name} declares {sorted(offending)}"


def test_every_declared_capture_contract_family_is_a_derived_family(
    manifest: ProviderManifest,
) -> None:
    for implementation in manifest.implementations:
        assert implementation.capture_contract_families
        for family in implementation.capture_contract_families:
            assert family.startswith(f"{implementation.interface_id}.capture."), family
            assert family.endswith(".derived.v1"), (
                f"{family} is not a derived family; a forecast, an anomaly flag, a "
                "rank, and a linkage score are never observed"
            )


def test_every_implementation_declares_zero_endpoints(manifest: ProviderManifest) -> None:
    for implementation in manifest.implementations:
        assert implementation.declared_endpoints == ()


def test_every_implementation_declares_itself_deterministic_and_side_effect_free(
    manifest: ProviderManifest,
) -> None:
    """Honest flags: nothing here reaches the network or writes anything."""

    assert {impl.interface_id for impl in manifest.implementations} == set(INTERFACE_IDS)
    for implementation in manifest.implementations:
        assert implementation.deterministic is True
        assert implementation.side_effects is False


def test_no_implementation_reads_a_credential() -> None:
    """Statically: ``context.secrets`` appears in no source file."""

    for path in sorted(SOURCE_DIR.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        assert "context.secrets" not in source, path.name
        assert ".secrets[" not in source, path.name


@pytest.mark.parametrize("fixture", FIXTURES, ids=IDS)
def test_a_delivered_credential_changes_nothing(fixture: object) -> None:
    """Behaviourally, so a future implementation cannot read one another way."""

    interface_id = fixture.interface_id  # type: ignore[attr-defined]
    payload = fixture.payload  # type: ignore[attr-defined]
    without = run_in_process(interface_id, payload)
    with_secret = run_in_process(
        interface_id, payload, secrets={"quant.unused_credential": "c0ffee-do-not-use"}
    )
    assert canonical_json(without.output) == canonical_json(with_secret.output)
    assert "c0ffee-do-not-use" not in canonical_json(with_secret.output).decode("utf-8")


def test_the_linkage_output_never_carries_a_merge_decision() -> None:
    """Linkage proposes; identity decisions are governed elsewhere."""

    for fixture in FIXTURES:
        if fixture.interface_id != "match.record":
            continue
        result = run_in_process(fixture.interface_id, fixture.payload)
        assert result.output is not None
        assert result.output["review_required"] is True
        rendered = canonical_json(result.output).decode("utf-8")
        for word in ("cluster", "merge", "survivor", "canonical_record"):
            assert word not in rendered, f"{fixture.fixture_id} mentions {word!r}"


@pytest.mark.parametrize("fixture", FIXTURES, ids=IDS)
def test_every_score_travels_with_the_scale_it_lives_on(fixture: object) -> None:
    """The positive form of the confidence ban.

    Banning a word is easy to route around. What actually prevents a confidence
    score is that every number an implementation reports is required to name what
    it is: a scale for a rank, a scale estimate for a residual, a level for an
    interval, declared parameters for a match weight.
    """

    interface_id = fixture.interface_id  # type: ignore[attr-defined]
    result = run_in_process(interface_id, fixture.payload)  # type: ignore[attr-defined]
    output = result.output
    assert output is not None
    if interface_id == "score.rank":
        assert output["score_kind"] in {
            "weighted_sum",
            "decision_function",
            "positive_class_probability",
        }
        assert output["objective"]
    elif interface_id == "ts.anomaly":
        assert output["scale_estimate"]["kind"] in {"median_absolute_deviation", "none"}
        assert output["threshold"]["kind"] in {"modified_z", "declared_changepoint_count"}
    elif interface_id == "ts.forecast":
        assert output["prediction_intervals"]
        assert all(entry["level"] for entry in output["prediction_intervals"])
    elif interface_id == "stat.test":
        assert output["statistic_kind"]
        assert output["alpha"]
    elif interface_id == "match.record":
        assert output["engine"]["parameters"] == "declared"
        assert output["prior_match_probability"]
    elif interface_id == "calc.calibrate":
        assert output["bin_edges"]
        assert output["outcome_type"] == "binary"
    else:
        assert interface_id == "calc.reduce"
        assert output["reduction_kind"]
