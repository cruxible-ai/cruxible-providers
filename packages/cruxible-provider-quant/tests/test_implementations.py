"""What the engines actually compute, checked against known answers.

Every assertion here is on a real engine run over a small deterministic fixture.
Nothing is mocked, because a mocked statistical engine tests the mock. The
tolerances are the ones documented in the package README, and the split between
"asserted exactly" and "asserted to a tolerance" is deliberate: an integer index,
a chosen model family, a comparison vector, and a reject/retain decision are the
answer, while the last bits of a float sum are an artifact of the BLAS.
"""

from __future__ import annotations

import math
from typing import Any

import pytest

from .conftest import run_in_process
from .fixtures import FIXTURES

# Documented in README.md. Exact figures rather than a blanket default, because a
# single loose tolerance across seven engines would be hiding the one that needs
# it behind six that do not.
RTOL_FORECAST = 1e-6
RTOL_STATISTIC = 1e-9
RTOL_RESIDUAL = 1e-9
RTOL_LINKAGE = 1e-9
RTOL_REDUCTION = 1e-12
RTOL_CALIBRATION = 1e-12


def _ok(fixture_id: str, **mutations: Any) -> dict[str, Any]:
    fixture = next(f for f in FIXTURES if f.fixture_id == fixture_id)
    payload = {**fixture.payload, **mutations}
    result = run_in_process(fixture.interface_id, payload)
    assert result.status == "ok", result.refusal or result.error
    assert result.output is not None
    return result.output


# --------------------------------------------------------------------------
# calc.reduce
# --------------------------------------------------------------------------


def test_a_grouped_reduction_matches_the_arithmetic_it_claims() -> None:
    fixture = next(f for f in FIXTURES if f.fixture_id == "quant-reduce-small")
    rows = fixture.payload["rows"]
    output = _ok("quant-reduce-small")

    assert output["reduction_kind"] == "grouped_aggregate"
    assert output["input_row_count"] == len(rows)
    assert [row["region"] for row in output["rows"]] == ["east", "north", "south"]

    for group in output["rows"]:
        members = [row for row in rows if row["region"] == group["region"]]
        assert group["total_amount"] == pytest.approx(
            sum(row["amount"] for row in members), rel=RTOL_REDUCTION
        )
        assert group["mean_count"] == pytest.approx(
            sum(row["count"] for row in members) / len(members), rel=RTOL_REDUCTION
        )


def test_group_order_is_sorted_not_hash_ordered() -> None:
    """Exact, because group order is part of the answer a consumer diffs."""

    output = _ok("quant-reduce-small")
    regions = [row["region"] for row in output["rows"]]
    assert regions == sorted(regions)


def test_a_windowed_reduction_orders_by_the_named_column() -> None:
    rows = [{"a": float(index), "k": index % 3} for index in reversed(range(10))]
    result = run_in_process(
        "calc.reduce",
        {
            "rows": rows,
            "window": {
                "column": "a",
                "order_by": "a",
                "function": "sum",
                "size": 3,
                "alias": "rolling",
            },
        },
    )
    assert result.status == "ok"
    assert result.output is not None
    ordered = result.output["rows"]
    assert [row["a"] for row in ordered] == [float(index) for index in range(10)]
    # The first two positions have no full window, so the rolling value is null:
    # a partial window reported as if it were full would be a quiet lie.
    assert ordered[0]["rolling"] is None
    assert ordered[1]["rolling"] is None
    assert ordered[2]["rolling"] == pytest.approx(0.0 + 1.0 + 2.0, rel=RTOL_REDUCTION)
    assert ordered[9]["rolling"] == pytest.approx(7.0 + 8.0 + 9.0, rel=RTOL_REDUCTION)


def test_the_engine_block_reports_the_observed_thread_pool() -> None:
    """Measured, not claimed: the pin is requested and the result is reported."""

    output = _ok("quant-reduce-medium")
    assert output["engine"]["name"] == "polars"
    assert isinstance(output["engine"]["thread_pool_size"], int)
    assert output["engine"]["thread_pool_size"] >= 1


# --------------------------------------------------------------------------
# ts.anomaly
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fixture_id", "planted"),
    [
        ("quant-anomaly-counts", 31),
        ("quant-anomaly-rates", 17),
        ("quant-anomaly-unbounded", 23),
    ],
)
def test_stl_mad_finds_the_planted_spike_and_nothing_else(fixture_id: str, planted: int) -> None:
    """Flagged indices are asserted exactly. They are integers, and they are the answer."""

    output = _ok(fixture_id)
    assert output["method"] == "stl_mad"
    assert output["flagged_indices"] == [planted]
    assert output["scale_estimate"]["kind"] == "median_absolute_deviation"
    assert output["scale_estimate"]["value"] > 0.0
    assert output["threshold"] == {"kind": "modified_z", "value": 3.5}

    points = output["points"]
    assert len(points) == len(
        next(f for f in FIXTURES if f.fixture_id == fixture_id).payload["series"]
    )
    assert points[planted]["flagged"] is True
    assert abs(points[planted]["modified_z"]) > 3.5
    assert all(math.isfinite(point["modified_z"]) for point in points)


def test_the_modified_z_is_the_statistic_it_says_it_is() -> None:
    """0.6745 * (residual - median) / MAD, recomputed from the reported parts."""

    output = _ok("quant-anomaly-unbounded")
    scale = output["scale_estimate"]
    for point in output["points"][:20]:
        expected = (
            scale["mad_to_sigma"] * (point["residual"] - scale["median_residual"]) / scale["value"]
        )
        assert point["modified_z"] == pytest.approx(expected, rel=RTOL_RESIDUAL)


def test_the_threshold_is_declared_and_honoured() -> None:
    loose = _ok("quant-anomaly-counts", threshold_modified_z=50.0)
    assert loose["threshold"]["value"] == 50.0
    assert loose["flagged_indices"] == []

    tight = _ok("quant-anomaly-counts", threshold_modified_z=1.0)
    assert len(tight["flagged_indices"]) > 1


def test_changepoint_detection_returns_the_declared_number_of_boundaries() -> None:
    output = _ok("quant-anomaly-bounded")
    assert output["method"] == "changepoint"
    assert len(output["changepoints"]) == 2
    assert output["changepoints"] == sorted(output["changepoints"])
    assert len(output["segments"]) == 3
    assert output["segments"][0]["start"] == 0
    assert output["segments"][-1]["end"] == 60
    for previous, following in zip(output["segments"], output["segments"][1:], strict=False):
        assert previous["end"] == following["start"]


def test_a_planted_level_shift_is_found_at_its_boundary() -> None:
    """Exact: a changepoint index is the answer, not an estimate of one."""

    values = [10.0] * 40 + [30.0] * 40
    series = [
        {"timestamp": 1_704_067_200.0 + index * 86_400.0, "value": value}
        for index, value in enumerate(values)
    ]
    result = run_in_process(
        "ts.anomaly",
        {"series": series, "season_length": 7, "method": "changepoint", "changepoint_count": 1},
    )
    assert result.status == "ok", result.refusal or result.error
    assert result.output is not None
    assert result.output["changepoints"] == [40]
    assert result.output["segments"][0]["mean"] == pytest.approx(10.0, rel=RTOL_RESIDUAL)
    assert result.output["segments"][1]["mean"] == pytest.approx(30.0, rel=RTOL_RESIDUAL)


# --------------------------------------------------------------------------
# ts.forecast
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fixture_id",
    [
        "quant-forecast-short-counts",
        "quant-forecast-short-continuous",
        "quant-forecast-medium-counts",
        "quant-forecast-medium-continuous",
    ],
)
def test_a_forecast_has_an_explicit_horizon_and_nested_intervals(fixture_id: str) -> None:
    """The structural properties are exact. They are what a consumer relies on."""

    fixture = next(f for f in FIXTURES if f.fixture_id == fixture_id)
    output = _ok(fixture_id)
    horizon = fixture.payload["horizon"]

    assert output["horizon"] == horizon
    assert output["model"] == fixture.payload["model"]
    assert output["season_length"] == fixture.payload["season_length"]
    assert len(output["point_forecast"]) == horizon
    assert all(math.isfinite(value) for value in output["point_forecast"])

    levels = [interval["level"] for interval in output["prediction_intervals"]]
    assert levels == sorted(fixture.payload["interval_levels"])
    for interval in output["prediction_intervals"]:
        assert len(interval["lower"]) == len(interval["upper"]) == horizon
        for step in range(horizon):
            assert interval["lower"][step] <= output["point_forecast"][step]
            assert output["point_forecast"][step] <= interval["upper"][step]

    # A wider level must contain a narrower one at every step. This is the
    # property that makes two intervals worth reporting instead of one number.
    if len(output["prediction_intervals"]) > 1:
        narrow, wide = output["prediction_intervals"][0], output["prediction_intervals"][1]
        assert narrow["level"] < wide["level"]
        for step in range(horizon):
            assert wide["lower"][step] <= narrow["lower"][step]
            assert narrow["upper"][step] <= wide["upper"][step]


def test_a_seasonal_series_forecasts_near_its_own_seasonal_level() -> None:
    """A tolerance test, and a loose one on purpose.

    The point is not that the baseline is accurate to six figures; it is that it
    is fitting the series in front of it. A forecast that came back at the series
    mean regardless of phase would pass a "finite and ordered" check and fail
    this one.
    """

    output = _ok("quant-forecast-medium-continuous")
    values = [
        record["value"]
        for record in next(
            f for f in FIXTURES if f.fixture_id == "quant-forecast-medium-continuous"
        ).payload["series"]
    ]
    level = sum(values) / len(values)
    amplitude = (max(values) - min(values)) / 2.0
    for step, predicted in enumerate(output["point_forecast"]):
        expected = values[len(values) - 7 + step % 7]
        assert abs(predicted - expected) < amplitude, (step, predicted, expected, level)


def test_the_selected_specification_is_reported() -> None:
    """A receipt has to record what ran, not only what was asked for."""

    arima = _ok("quant-forecast-short-continuous")
    assert arima["engine"]["name"] == "statsforecast.AutoARIMA"
    assert arima["model_selected"]

    ets = _ok("quant-forecast-short-counts")
    assert ets["engine"]["name"] == "statsforecast.AutoETS"
    assert ets["model_selected"]


def test_the_interval_levels_are_the_declared_ones() -> None:
    output = _ok("quant-forecast-medium-continuous", interval_levels=[50, 80, 99])
    assert [interval["level"] for interval in output["prediction_intervals"]] == [50, 80, 99]


# --------------------------------------------------------------------------
# stat.test
# --------------------------------------------------------------------------


def test_the_declared_test_is_the_test_that_ran() -> None:
    for fixture in FIXTURES:
        if fixture.interface_id != "stat.test":
            continue
        output = _ok(fixture.fixture_id)
        assert output["test"] == fixture.payload["test"]
        assert output["test_family"] == fixture.payload["test_family"]
        assert output["alpha"] == fixture.payload["alpha"]
        assert math.isfinite(output["statistic"])
        assert 0.0 <= output["p_value"] <= 1.0
        assert output["reject_null"] is (output["p_value"] < output["alpha"])


def test_a_violated_assumption_is_reported_and_does_not_change_the_test() -> None:
    """The no-auto-selection position, on the path where it is tempting to break.

    The samples are heavily skewed, so the normality check fails. A "helpful"
    implementation would switch to a rank test. This one runs the declared t-test
    and says the ground under it is soft.
    """

    skewed = [float(index) ** 3 for index in range(40)]
    flat = [1.0 + 0.001 * index for index in range(40)]
    result = run_in_process(
        "stat.test",
        {
            "test": "welch_t",
            "test_family": "location",
            "design": "independent",
            "alpha": 0.05,
            "declared_assumptions": ["normality", "equal_variance"],
            "samples": {"a": skewed, "b": flat},
        },
    )
    assert result.status == "ok"
    assert result.output is not None
    assert result.output["test"] == "welch_t"
    assert result.output["assumptions_satisfied"] is False
    by_name = {entry["name"]: entry for entry in result.output["assumptions"]}
    assert by_name["normality"]["checked"] is True
    assert by_name["normality"]["check"] == "shapiro_wilk"
    assert by_name["normality"]["holds"] is False
    assert by_name["equal_variance"]["check"] == "levene"


def test_an_undeclared_assumption_is_not_invented() -> None:
    """The report covers what was declared. Silence is not a passing check."""

    output = _ok("quant-stat-proportion")
    names = {entry["name"] for entry in output["assumptions"]}
    assert names == {"independent_observations"}
    assert output["assumptions"][0]["checked"] is False
    assert output["assumptions"][0]["holds"] is None
    assert output["assumptions_satisfied"] is None


def test_welch_and_student_disagree_on_the_same_samples() -> None:
    """Two declared tests, two answers, neither substituted for the other."""

    base = dict(next(f for f in FIXTURES if f.fixture_id == "quant-stat-variance").payload)
    common = {
        "test_family": "location",
        "design": "independent",
        "alpha": 0.05,
        "declared_assumptions": [],
        "samples": base["samples"],
    }
    welch = run_in_process("stat.test", {**common, "test": "welch_t"})
    student = run_in_process("stat.test", {**common, "test": "student_t"})
    assert welch.output is not None and student.output is not None
    assert welch.output["degrees_of_freedom"] != student.output["degrees_of_freedom"]
    assert welch.output["statistic"] == pytest.approx(
        student.output["statistic"], rel=RTOL_STATISTIC
    )


def test_a_known_test_statistic_matches_its_closed_form() -> None:
    """Pinned against arithmetic anyone can redo, not against a recorded run."""

    a = [1.0, 2.0, 3.0, 4.0, 5.0]
    b = [2.0, 4.0, 5.0, 4.0, 6.0]
    result = run_in_process(
        "stat.test",
        {
            "test": "paired_t",
            "test_family": "location",
            "design": "paired",
            "alpha": 0.05,
            "declared_assumptions": [],
            "samples": {"a": a, "b": b},
        },
    )
    assert result.output is not None
    # differences: [-1, -2, -2, 0, -1]; mean -1.2, sample sd sqrt(0.7);
    # t = -1.2 / (sqrt(0.7) / sqrt(5)) = -3.2071349...
    assert result.output["effect"]["value"] == pytest.approx(-1.2, rel=RTOL_STATISTIC)
    assert result.output["degrees_of_freedom"] == pytest.approx(4.0)
    assert result.output["statistic"] == pytest.approx(-3.2071349, rel=1e-6)
    assert result.output["statistic_kind"] == "t"
    assert result.output["reject_null"] is True


def test_pearson_r_on_a_near_linear_pair_is_close_to_one() -> None:
    output = _ok("quant-stat-association")
    assert output["statistic"] > 0.99
    assert output["effect"] == {"kind": "pearson_r", "value": output["statistic"]}
    assert output["p_value"] < 1e-30


# --------------------------------------------------------------------------
# score.rank
# --------------------------------------------------------------------------


def test_the_weighted_score_is_the_declared_linear_combination() -> None:
    fixture = next(f for f in FIXTURES if f.fixture_id == "quant-rank-small")
    output = _ok("quant-rank-small")
    weights = fixture.payload["weights"]
    signals = {item["id"]: item["signals"] for item in fixture.payload["items"]}

    assert output["score_kind"] == "weighted_sum"
    assert output["signals_used"] == sorted(weights)
    for entry in output["ranking"]:
        expected = sum(weights[name] * signals[entry["id"]][name] for name in weights)
        assert entry["score"] == pytest.approx(expected, rel=RTOL_REDUCTION)


def test_the_ranking_is_ordered_and_the_tie_break_is_declared() -> None:
    output = _ok("quant-rank-small")
    scores = [entry["score"] for entry in output["ranking"]]
    assert scores == sorted(scores, reverse=True)
    assert [entry["rank"] for entry in output["ranking"]] == list(range(1, len(scores) + 1))
    assert output["tie_break"] == "id_ascending"


def test_tied_items_are_named_and_ordered_by_the_declared_rule() -> None:
    """The heavy-ties bucket exists so this case has somewhere to be checked."""

    output = _ok("quant-rank-medium")
    tied = [entry for entry in output["ranking"] if entry["tied_with"]]
    assert tied, "the medium fixture is built to produce ties"
    group = [entry for entry in output["ranking"] if entry["score"] == tied[0]["score"]]
    assert [entry["id"] for entry in group] == sorted(entry["id"] for entry in group)
    assert set(tied[0]["tied_with"]) == {e["id"] for e in group} - {tied[0]["id"]}


# --------------------------------------------------------------------------
# match.record
# --------------------------------------------------------------------------


def test_planted_duplicates_score_above_the_rest() -> None:
    output = _ok("quant-linkage-strong-blocking")
    assert output["link_type"] == "dedupe_only"
    assert output["engine"]["trained"] is False
    assert output["engine"]["parameters"] == "declared"

    pairs = output["pairs"]
    assert pairs, "the fixture plants duplicates above the threshold"
    assert pairs == sorted(pairs, key=lambda pair: (pair["left_id"], pair["right_id"]))

    planted = {(f"r{index - 1:04d}", f"r{index:04d}") for index in range(60) if index % 10 == 1}
    found = {(pair["left_id"], pair["right_id"]) for pair in pairs}
    assert planted <= found

    for pair in pairs:
        if (pair["left_id"], pair["right_id"]) in planted:
            assert pair["comparison_vector"] == {"first_name": 1, "surname": 1, "city": 1}
            # Well above the 0.5 threshold, and deliberately not "essentially 1":
            # with a declared prior of 0.01 an all-fields agreement is strong
            # evidence, not certainty, and the number says so.
            assert pair["match_probability"] > 0.95


def test_the_match_weight_is_the_fellegi_sunter_sum_it_claims() -> None:
    """Recomputed from the declared m/u and the reported comparison vector."""

    output = _ok("quant-linkage-strong-blocking")
    fixture = next(f for f in FIXTURES if f.fixture_id == "quant-linkage-strong-blocking")
    parameters = {c["field"]: c for c in fixture.payload["comparisons"]}
    prior = fixture.payload["prior_match_probability"]

    for pair in output["pairs"]:
        weight = math.log2(prior / (1.0 - prior))
        for field, gamma in pair["comparison_vector"].items():
            m = parameters[field]["m_probability"]
            u = parameters[field]["u_probability"]
            weight += math.log2((m / u) if gamma == 1 else ((1 - m) / (1 - u)))
        assert pair["match_weight"] == pytest.approx(weight, rel=RTOL_LINKAGE)
        assert pair["match_probability"] == pytest.approx(
            2.0**weight / (1.0 + 2.0**weight), rel=RTOL_LINKAGE
        )


def test_the_output_proposes_and_never_decides() -> None:
    """No clusters, no merges, no surviving-record choice. Review is required."""

    output = _ok("quant-linkage-weak-blocking")
    assert output["review_required"] is True
    forbidden = {"clusters", "cluster_id", "merged", "merge", "canonical_record", "survivor"}
    assert not forbidden & set(output)
    for pair in output["pairs"]:
        assert not forbidden & set(pair)
        assert "decision" not in pair


# --------------------------------------------------------------------------
# calc.calibrate
# --------------------------------------------------------------------------


def test_the_brier_score_is_the_mean_squared_error_it_claims() -> None:
    fixture = next(f for f in FIXTURES if f.fixture_id == "quant-calibrate-balanced")
    records = fixture.payload["predictions"]
    output = _ok("quant-calibrate-balanced")

    expected = sum((r["prediction"] - r["outcome"]) ** 2 for r in records) / len(records)
    assert output["brier_score"] == pytest.approx(expected, rel=RTOL_CALIBRATION)
    assert output["sample_size"] == len(records)
    assert output["base_rate"] == pytest.approx(
        sum(r["outcome"] for r in records) / len(records), rel=RTOL_CALIBRATION
    )


def test_the_murphy_decomposition_reconstructs_the_brier_score() -> None:
    """reliability - resolution + uncertainty == Brier, which is the whole point.

    Three numbers that do not add back up to the score they decompose are three
    numbers nobody should read.
    """

    for fixture_id in ("quant-calibrate-balanced", "quant-calibrate-imbalanced"):
        output = _ok(fixture_id)
        parts = output["brier_decomposition"]
        reconstructed = parts["reliability"] - parts["resolution"] + parts["uncertainty"]
        assert reconstructed == pytest.approx(output["brier_score"], abs=0.02), fixture_id
        assert parts["uncertainty"] == pytest.approx(
            output["base_rate"] * (1 - output["base_rate"]), rel=RTOL_CALIBRATION
        )


def test_the_reliability_bins_cover_every_prediction_exactly_once() -> None:
    fixture = next(f for f in FIXTURES if f.fixture_id == "quant-calibrate-imbalanced")
    output = _ok("quant-calibrate-imbalanced")
    assert output["bin_edges"] == fixture.payload["bin_edges"]
    assert len(output["reliability_bins"]) == len(fixture.payload["bin_edges"]) - 1
    assert sum(entry["count"] for entry in output["reliability_bins"]) == output["sample_size"]
    for entry in output["reliability_bins"]:
        if entry["count"] == 0:
            assert entry["mean_prediction"] is None
            assert entry["observed_frequency"] is None
        else:
            assert entry["lower"] <= entry["mean_prediction"] <= entry["upper"]
            assert 0.0 <= entry["observed_frequency"] <= 1.0


def test_a_perfectly_calibrated_set_reads_as_calibrated() -> None:
    """The end-to-end sanity check: a reading that cannot detect calibration is useless."""

    records = []
    for bin_index in range(10):
        probability = bin_index / 10.0 + 0.05
        positives = round(20 * probability)
        for position in range(20):
            index = bin_index * 20 + position
            records.append(
                {
                    "prediction": probability,
                    "outcome": float(position < positives),
                    "made_at": float(index),
                    "settled_at": float(index) + 1.0,
                }
            )
    result = run_in_process("calc.calibrate", {"predictions": records, "bin_count": 10})
    assert result.status == "ok", result.refusal
    assert result.output is not None
    assert result.output["expected_calibration_error"] < 0.06
    assert result.output["brier_decomposition"]["reliability"] < 0.01
