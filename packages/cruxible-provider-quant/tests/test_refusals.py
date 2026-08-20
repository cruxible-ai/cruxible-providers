"""Every rule this plane declines under has a raise site and a named test.

Same discipline as the runtime taxonomy's standing check, over the subset of it
that a quantitative implementation may reach for: a rule nobody raises is a rule
that has drifted away from the code, and the fail-closed paths are exactly the
ones nobody takes by accident.

The last test in this file is the enforcement. It is static — a code counts as
exercised when a test above names it — which is weaker than tracing raises at
runtime and stronger than nothing, and it fails in the useful direction: adding
a code to the plane's subset without a test breaks the build.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
from cruxible_provider_quant.refusals import QUANT_DECLINES, decline
from cruxible_provider_runtime.errors import RefusalCode

from .conftest import run_in_process
from .fixtures import FIXTURES


def _declined(interface_id: str, payload: dict[str, Any]) -> RefusalCode:
    """The rule a run declined under, read off the refusal's own code.

    It used to be read out of ``detail['reason']``, because the plane carried a
    second enum and had to smuggle it through a generic ``provider_declined``.
    The taxonomy carries these codes now, so the code is the reason.
    """

    result = run_in_process(interface_id, payload)
    assert result.status == "refused", result.output
    assert result.refusal is not None
    assert result.refusal.code in QUANT_DECLINES
    return result.refusal.code


def _fixture(fixture_id: str) -> dict[str, Any]:
    return dict(next(f for f in FIXTURES if f.fixture_id == fixture_id).payload)


# --------------------------------------------------------------------------
# series and sample shape
# --------------------------------------------------------------------------


def test_a_series_too_short_for_its_declared_period_refuses() -> None:
    """The raise site is inside a *claimed* bucket, which is the point.

    A 40-point series is ``series_length=short`` and admitted; STL at period 24
    needs 48 observations. Admission cannot know that — the seasonal period is a
    modelling parameter, not a bucket dimension — so the implementation is the
    only thing positioned to refuse, and it does so by name.
    """

    payload = _fixture("quant-anomaly-counts")
    payload["series"] = payload["series"][:40]
    payload["season_length"] = 24
    assert _declined("ts.anomaly", payload) is RefusalCode.INSUFFICIENT_SERIES_LENGTH


def test_a_forecast_series_too_short_for_its_period_refuses() -> None:
    payload = _fixture("quant-forecast-short-counts")
    payload["series"] = payload["series"][:30]
    payload["season_length"] = 24
    assert _declined("ts.forecast", payload) is RefusalCode.INSUFFICIENT_SERIES_LENGTH


def test_a_non_finite_observation_refuses() -> None:
    payload = _fixture("quant-anomaly-unbounded")
    series = [dict(record) for record in payload["series"]]
    series[5]["value"] = float("inf")
    payload["series"] = series
    assert _declined("ts.anomaly", payload) is RefusalCode.NON_FINITE_INPUT


def test_a_constant_residual_scale_refuses_rather_than_dividing_by_zero() -> None:
    """A perfectly flat series has no dispersion to score a residual against."""

    payload = _fixture("quant-anomaly-counts")
    payload["series"] = [
        {"timestamp": record["timestamp"], "value": 7.0} for record in payload["series"]
    ]
    assert _declined("ts.anomaly", payload) is RefusalCode.DEGENERATE_SCALE


def test_a_paired_test_with_unequal_groups_refuses() -> None:
    payload = _fixture("quant-stat-location-paired")
    payload["samples"] = {
        "after": payload["samples"]["after"],
        "before": payload["samples"]["before"][:30],
    }
    assert _declined("stat.test", payload) is RefusalCode.MISMATCHED_LENGTHS


# --------------------------------------------------------------------------
# declared method
# --------------------------------------------------------------------------


def test_an_unknown_anomaly_method_refuses() -> None:
    payload = _fixture("quant-anomaly-counts")
    payload["method"] = "isolation_forest"
    assert _declined("ts.anomaly", payload) is RefusalCode.UNKNOWN_METHOD


def test_an_unknown_forecast_model_refuses() -> None:
    payload = _fixture("quant-forecast-short-counts")
    payload["model"] = "prophet"
    assert _declined("ts.forecast", payload) is RefusalCode.UNKNOWN_METHOD


def test_an_unknown_rank_mode_refuses() -> None:
    payload = _fixture("quant-rank-small")
    payload["mode"] = "learned_to_rank"
    assert _declined("score.rank", payload) is RefusalCode.UNKNOWN_METHOD


def test_an_unknown_test_name_is_never_substituted() -> None:
    """The whole no-auto-selection position, in one assertion."""

    payload = _fixture("quant-stat-location-independent")
    payload["test"] = "the_obviously_right_one"
    assert _declined("stat.test", payload) is RefusalCode.UNKNOWN_TEST_NAME


def test_a_declared_family_that_does_not_match_the_test_refuses() -> None:
    payload = _fixture("quant-stat-location-independent")
    payload["test_family"] = "variance"
    assert _declined("stat.test", payload) is RefusalCode.DECLARED_FAMILY_MISMATCH


def test_an_unsupported_aggregation_refuses() -> None:
    payload = _fixture("quant-reduce-small")
    payload["aggregations"] = [{"column": "amount", "function": "kurtosis"}]
    assert _declined("calc.reduce", payload) is RefusalCode.UNSUPPORTED_AGGREGATION


def test_an_unsupported_window_function_refuses() -> None:
    payload = _fixture("quant-reduce-small")
    payload.pop("group_by")
    payload.pop("aggregations")
    payload["window"] = {
        "column": "amount",
        "order_by": "amount",
        "function": "median",
        "size": 3,
    }
    assert _declined("calc.reduce", payload) is RefusalCode.UNSUPPORTED_AGGREGATION


# --------------------------------------------------------------------------
# referenced data
# --------------------------------------------------------------------------


def test_an_aggregation_on_a_missing_column_refuses() -> None:
    payload = _fixture("quant-reduce-small")
    payload["aggregations"] = [{"column": "not_a_column", "function": "sum"}]
    assert _declined("calc.reduce", payload) is RefusalCode.UNKNOWN_COLUMN


def test_a_weight_naming_a_signal_no_item_carries_refuses() -> None:
    payload = _fixture("quant-rank-small")
    payload["weights"] = {**payload["weights"], "reachability": 1.0}
    assert _declined("score.rank", payload) is RefusalCode.UNKNOWN_COLUMN


def test_a_comparison_on_a_field_the_records_lack_refuses() -> None:
    payload = _fixture("quant-linkage-strong-blocking")
    payload["comparisons"] = [{"field": "postcode", "m_probability": 0.9, "u_probability": 0.01}]
    assert _declined("match.record", payload) is RefusalCode.UNKNOWN_COLUMN


# --------------------------------------------------------------------------
# the pinned-model boundary
# --------------------------------------------------------------------------


@pytest.fixture()
def pinned_model(tmp_path: Path) -> tuple[Path, str]:
    """A tiny logistic regression, fitted with a fixed seed and hashed."""

    import pickle

    import numpy as np
    from sklearn.linear_model import LogisticRegression

    features = np.array(
        [[float(i % 11), float((i * 3) % 7), float((i * 5) % 29)] for i in range(60)],
        dtype=np.float64,
    )
    labels = np.array([int(row[0] + row[1] > 9) for row in features])
    model = LogisticRegression(random_state=0, max_iter=500).fit(features, labels)
    path = tmp_path / "model.pkl"
    blob = pickle.dumps(model)
    path.write_bytes(blob)
    return path, "sha256:" + hashlib.sha256(blob).hexdigest()


def test_an_unpinned_model_reference_refuses(pinned_model: tuple[Path, str]) -> None:
    """No pin, no load. The trust decision has to have been made by someone."""

    path, _ = pinned_model
    payload = _fixture("quant-rank-small")
    payload["mode"] = "pinned_model"
    payload.pop("weights")
    payload["model_ref"] = {
        "kind": "pickled_sklearn",
        "path": str(path),
        "feature_order": ["severity", "exposure", "age_days"],
        "score_kind": "decision_function",
    }
    assert _declined("score.rank", payload) is RefusalCode.MALFORMED_MODEL_REF


def test_a_model_whose_bytes_do_not_match_its_pin_is_an_integrity_refusal(
    pinned_model: tuple[Path, str],
) -> None:
    """Not a decline. The file exists, was read, and is not what was approved."""

    path, pin = pinned_model
    path.write_bytes(path.read_bytes() + b"\x00")
    payload = _fixture("quant-rank-small")
    payload["mode"] = "pinned_model"
    payload.pop("weights")
    payload["model_ref"] = {
        "kind": "pickled_sklearn",
        "path": str(path),
        "sha256": pin,
        "feature_order": ["severity", "exposure", "age_days"],
        "score_kind": "decision_function",
    }
    result = run_in_process("score.rank", payload)
    assert result.status == "refused", result.output
    assert result.refusal is not None
    # An integrity event, not a capability decline. It must be countable apart
    # from the shape failures above, so it wears the taxonomy's own name.
    assert result.refusal.code is RefusalCode.ARTIFACT_HASH_MISMATCH
    assert result.refusal.detail["pinned"] == pin
    assert result.refusal.detail["observed"] != pin
    assert result.refusal.code not in QUANT_DECLINES


def test_a_missing_model_file_declines_rather_than_reporting_tampering(
    pinned_model: tuple[Path, str],
) -> None:
    """The other side of the split: absent is not altered.

    A file that was never supplied is a request this implementation cannot
    serve. Reporting it as a hash mismatch would put a tampering signal on a
    track record every time somebody mistyped a path.
    """

    path, pin = pinned_model
    payload = _fixture("quant-rank-small")
    payload["mode"] = "pinned_model"
    payload.pop("weights")
    payload["model_ref"] = {
        "kind": "pickled_sklearn",
        "path": str(path.parent / "absent.pkl"),
        "sha256": pin,
        "feature_order": ["severity", "exposure", "age_days"],
        "score_kind": "decision_function",
    }
    assert _declined("score.rank", payload) is RefusalCode.MALFORMED_MODEL_REF


def test_a_correctly_pinned_model_scores(pinned_model: tuple[Path, str]) -> None:
    """The success half, so the refusals above are not passing vacuously."""

    path, pin = pinned_model
    payload = _fixture("quant-rank-small")
    payload["mode"] = "pinned_model"
    payload.pop("weights")
    payload["model_ref"] = {
        "kind": "pickled_sklearn",
        "path": str(path),
        "sha256": pin,
        "feature_order": ["severity", "exposure", "age_days"],
        "score_kind": "positive_class_probability",
    }
    result = run_in_process("score.rank", payload)
    assert result.status == "ok", result.refusal
    assert result.output is not None
    assert result.output["score_kind"] == "positive_class_probability"
    assert result.output["engine"]["model_sha256"] == pin
    assert all(0.0 <= entry["score"] <= 1.0 for entry in result.output["ranking"])


# --------------------------------------------------------------------------
# linkage parameters
# --------------------------------------------------------------------------


def test_linkage_without_a_declared_prior_refuses() -> None:
    """splink would supply a default prior and warn. A warning is not a decision."""

    payload = _fixture("quant-linkage-strong-blocking")
    payload.pop("prior_match_probability")
    assert _declined("match.record", payload) is RefusalCode.UNDECLARED_MATCH_PARAMETERS


def test_linkage_without_declared_m_and_u_refuses() -> None:
    payload = _fixture("quant-linkage-strong-blocking")
    payload["comparisons"] = [{"field": "surname", "m_probability": 0.9}]
    assert _declined("match.record", payload) is RefusalCode.UNDECLARED_MATCH_PARAMETERS


# --------------------------------------------------------------------------
# parameter ranges
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("interface_id", "mutation"),
    [
        pytest.param("ts.anomaly", {"threshold_modified_z": -1.0}, id="negative-threshold"),
        pytest.param("ts.anomaly", {"season_length": 1}, id="degenerate-period"),
    ],
)
def test_an_out_of_range_anomaly_parameter_refuses(
    interface_id: str, mutation: dict[str, Any]
) -> None:
    payload = {**_fixture("quant-anomaly-counts"), **mutation}
    assert _declined(interface_id, payload) is RefusalCode.INVALID_PARAMETER


def test_an_alpha_outside_the_unit_interval_refuses() -> None:
    payload = _fixture("quant-stat-location-independent")
    payload["alpha"] = 5.0
    assert _declined("stat.test", payload) is RefusalCode.INVALID_PARAMETER


def test_a_probability_outside_the_unit_interval_refuses() -> None:
    payload = _fixture("quant-calibrate-balanced")
    records = [dict(record) for record in payload["predictions"]]
    records[3]["prediction"] = 1.4
    payload["predictions"] = records
    assert _declined("calc.calibrate", payload) is RefusalCode.INVALID_PARAMETER


def test_a_non_finite_prediction_refuses() -> None:
    payload = _fixture("quant-calibrate-balanced")
    records = [dict(record) for record in payload["predictions"]]
    records[2]["prediction"] = float("nan")
    payload["predictions"] = records
    assert _declined("calc.calibrate", payload) is RefusalCode.NON_FINITE_INPUT


def test_a_forecast_horizon_outside_the_cap_refuses() -> None:
    payload = _fixture("quant-forecast-medium-continuous")
    payload["horizon"] = 100_000
    assert _declined("ts.forecast", payload) is RefusalCode.INVALID_PARAMETER


def test_a_non_finite_signal_refuses() -> None:
    payload = _fixture("quant-rank-small")
    items = [dict(item) for item in payload["items"]]
    items[0] = {**items[0], "signals": {**items[0]["signals"], "severity": float("inf")}}
    payload["items"] = items
    assert _declined("score.rank", payload) is RefusalCode.NON_FINITE_INPUT


def test_a_non_finite_sample_observation_refuses() -> None:
    payload = _fixture("quant-stat-location-independent")
    samples = {key: list(values) for key, values in payload["samples"].items()}
    samples["a"][0] = float("nan")
    payload["samples"] = samples
    assert _declined("stat.test", payload) is RefusalCode.NON_FINITE_INPUT


def test_two_constant_samples_decline_rather_than_reporting_a_nan_conclusion() -> None:
    """The input is impeccable; the question is the one with no answer.

    Every observation is finite, both groups are the right length, and the test
    name and family agree. SciPy still returns a NaN statistic and a NaN p-value,
    because a t test over two zero-variance samples is undefined -- and reported
    as ``status=ok`` that reaches the evidence path as ``reject_null=False``, a
    statistical conclusion nobody drew.
    """

    payload = _fixture("quant-stat-location-independent")
    payload["samples"] = {"a": [4.0] * 12, "b": [4.0] * 12}
    assert _declined("stat.test", payload) is RefusalCode.NON_FINITE_RESULT


def test_an_unanswerable_question_declines_rather_than_erroring() -> None:
    """A decline, not an error: the implementation did exactly what it was asked."""

    payload = _fixture("quant-stat-location-independent")
    payload["samples"] = {"a": [1.0] * 8, "b": [1.0] * 8}
    result = run_in_process("stat.test", payload)
    assert result.status == "refused"
    assert result.error is None


def test_bin_edges_that_leave_part_of_the_probability_domain_uncovered_refuse() -> None:
    """A 0.1 must never be reported as though it had been seen inside [0.2, 0.8].

    The binning clips anything outside the declared edges into the nearest bin,
    so uncovered edges do not drop the prediction -- they relabel it, which is
    worse.
    """

    payload = _fixture("quant-calibrate-balanced")
    payload["bin_edges"] = [0.2, 0.5, 0.8]
    assert _declined("calc.calibrate", payload) is RefusalCode.INVALID_PARAMETER


def test_non_finite_bin_edges_refuse() -> None:
    payload = _fixture("quant-calibrate-balanced")
    payload["bin_edges"] = [0.0, 0.5, float("inf")]
    assert _declined("calc.calibrate", payload) is RefusalCode.NON_FINITE_INPUT


# --------------------------------------------------------------------------
# the standing check
# --------------------------------------------------------------------------


def test_every_rule_this_plane_declines_under_is_asserted_by_a_named_test() -> None:
    source = Path(__file__).read_text(encoding="utf-8")
    body = source.split("# the standing check")[0]
    unexercised = sorted(
        code.name for code in QUANT_DECLINES if f"RefusalCode.{code.name}" not in body
    )
    assert not unexercised, (
        "these decline rules are not asserted by any test above: "
        f"{unexercised}. Either exercise the path or remove the rule."
    )


def test_the_plane_cannot_decline_under_a_rule_that_is_not_its_own() -> None:
    """Most of the taxonomy is the executor's, and a provider may not speak for it.

    A provider reporting ``cache_integrity`` would be making a judgement it is
    not positioned to make, and a track record would count it against the wrong
    thing.
    """

    with pytest.raises(ValueError, match="is not a rule this plane declines under"):
        decline(RefusalCode.CACHE_INTEGRITY, "not this plane's to report")
