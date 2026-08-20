"""The per-bucket conformance fixtures, one per declared selector.

A claimed bucket without a passing conformance fixture refuses at registration.
This module is the other half of that rule: every selector the manifest declares
appears here exactly once, with a payload that classifies into a bucket the
selector matches and that the implementation actually answers.
``test_bucket_conformance.py`` asserts both directions — no fixture without a
claim, no claim without a fixture — so the manifest and this file cannot drift
apart in either direction.

All synthetic data is generated from a **closed-form deterministic** pseudo-noise
(``0.1 * sin(i * 12.9898)``) rather than from a seeded random generator. A seeded
generator is reproducible only as long as nobody changes the generator; a closed
form is reproducible because it is the same arithmetic everywhere, and a reader
can see what the series is without running anything.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

__all__ = ["FIXTURES", "Fixture", "fixtures_for", "noise"]

EPOCH = 1_704_067_200.0
"""2024-01-01T00:00:00Z, as epoch seconds."""

DAY = 86_400.0


def noise(index: int, scale: float = 0.1) -> float:
    """Deterministic, closed-form pseudo-noise. Same value on every machine."""

    return scale * math.sin(index * 12.9898)


def _series(values: list[float], step: float = DAY) -> list[dict[str, Any]]:
    return [
        {"timestamp": EPOCH + position * step, "value": value}
        for position, value in enumerate(values)
    ]


def _seasonal(
    length: int, period: int, level: float, amplitude: float, jitter: float = 0.1
) -> list[float]:
    return [
        level + amplitude * math.sin(2 * math.pi * index / period) + noise(index, jitter)
        for index in range(length)
    ]


def _seasonal_counts(length: int, period: int, level: float, amplitude: float) -> list[float]:
    """Integer counts with enough jitter that rounding does not erase the residual.

    The jitter is not decoration. Rounding a smooth seasonal curve produces an
    exactly periodic integer pattern, STL fits it to within float noise, and the
    residual MAD collapses to almost nothing — at which point every point in the
    series scores as a large modified z and the reading is meaningless. Real
    counts have dispersion; a fixture that does not is testing an artefact.
    """

    return [
        float(max(0, round(value)))
        for value in _seasonal(length, period, level, amplitude, jitter=1.6)
    ]


def _spiked(values: list[float], at: int, by: float) -> list[float]:
    spiked = list(values)
    spiked[at] += by
    return spiked


@dataclass(frozen=True)
class Fixture:
    """One conformance fixture: the selector it discharges and its payload."""

    interface_id: str
    selector: str
    fixture_id: str
    payload: dict[str, Any]
    expected_bucket: str


# --------------------------------------------------------------------------
# calc.reduce
# --------------------------------------------------------------------------

_REDUCE_SMALL_ROWS = [
    {
        "region": ["north", "south", "east"][index % 3],
        "amount": float(index) + noise(index),
        "count": index % 7,
    }
    for index in range(120)
]

_REDUCE_MEDIUM_ROWS = [
    {"bucket": index % 5, "value": float(index % 97) + noise(index)} for index in range(10_001)
]

# --------------------------------------------------------------------------
# ts.anomaly
# --------------------------------------------------------------------------

_ANOMALY_COUNTS = _spiked(_seasonal_counts(60, 7, 20.0, 5.0), at=31, by=25.0)
_ANOMALY_RATES = _spiked([value / 100.0 for value in _seasonal(60, 7, 40.0, 8.0)], at=17, by=0.4)
_ANOMALY_BOUNDED = _spiked(_seasonal(60, 7, 55.5, 9.0), at=44, by=30.0)
_ANOMALY_UNBOUNDED = _spiked(_seasonal(60, 7, 1000.5, 40.0), at=23, by=250.0)

# --------------------------------------------------------------------------
# ts.forecast
# --------------------------------------------------------------------------

_FORECAST_SHORT_COUNTS = _seasonal_counts(60, 7, 40.0, 6.0)
_FORECAST_SHORT_CONTINUOUS = _seasonal(60, 7, 12.5, 2.0)
_FORECAST_MEDIUM_COUNTS = _seasonal_counts(250, 7, 40.0, 6.0)
_FORECAST_MEDIUM_CONTINUOUS = _seasonal(250, 7, 12.5, 2.0)

# --------------------------------------------------------------------------
# stat.test
# --------------------------------------------------------------------------

_GROUP_A = [10.0 + noise(index, 2.0) for index in range(40)]
_GROUP_B = [11.5 + noise(index + 100, 2.0) for index in range(40)]
_SUCCESSES_A = [float((index * 7) % 10 < 6) for index in range(60)]
_SUCCESSES_B = [float((index * 3) % 10 < 4) for index in range(60)]
_WIDE = [10.0 + noise(index, 6.0) for index in range(40)]
_PAIRED_X = [float(index) + noise(index, 1.5) for index in range(40)]
_PAIRED_Y = [2.0 * index + 3.0 + noise(index + 50, 1.5) for index in range(40)]

# --------------------------------------------------------------------------
# score.rank
# --------------------------------------------------------------------------


def _rank_items(count: int, *, distinct: int) -> list[dict[str, Any]]:
    """``distinct`` signal vectors spread over ``count`` items.

    Setting ``distinct`` below ``count`` is how the ``heavy_ties`` bucket becomes
    reachable: items sharing a signal vector must score identically under any
    deterministic scorer, so the ordering among them is decided by the declared
    tie-break rule rather than by the objective — which is the case worth having
    a fixture for.
    """

    return [
        {
            "id": f"item-{index:04d}",
            "signals": {
                "severity": float(index % distinct % 11),
                "exposure": float((index % distinct) * 3 % 7),
                "age_days": float((index % distinct) * 5 % 29),
            },
        }
        for index in range(count)
    ]


# --------------------------------------------------------------------------
# match.record
# --------------------------------------------------------------------------

_SURNAMES = ["ashwood", "brennan", "calloway", "delaney", "everleigh"]
_CITIES = ["york", "leeds", "hull", "derby", "exeter"]


def _linkage_records(count: int, *, one_block: bool) -> list[dict[str, Any]]:
    """Records with a planted duplicate every tenth pair.

    ``one_block`` puts every record in a single blocking key so that the largest
    block exceeds the strong-blocking threshold, which is what makes the
    ``blocking=weak`` bucket reachable without inventing a second dataset.
    """

    records: list[dict[str, Any]] = []
    for index in range(count):
        twin = index % 10 == 1
        source = index - 1 if twin else index
        records.append(
            {
                "unique_id": f"r{index:04d}",
                "first_name": f"name{source % 23:02d}",
                "surname": "shared" if one_block else _SURNAMES[source % len(_SURNAMES)],
                "city": _CITIES[source % len(_CITIES)],
            }
        )
    return records


_LINKAGE_COMPARISONS = [
    {"field": "first_name", "m_probability": 0.9, "u_probability": 0.05},
    {"field": "surname", "m_probability": 0.95, "u_probability": 0.02},
    {"field": "city", "m_probability": 0.8, "u_probability": 0.2},
]

# --------------------------------------------------------------------------
# calc.calibrate
# --------------------------------------------------------------------------


def _settled(predictions: list[float], outcomes: list[float]) -> list[dict[str, Any]]:
    return [
        {
            "prediction": prediction,
            "outcome": outcome,
            "made_at": EPOCH + index * DAY,
            "settled_at": EPOCH + index * DAY + DAY,
        }
        for index, (prediction, outcome) in enumerate(zip(predictions, outcomes, strict=True))
    ]


_CALIBRATE_BALANCED_P = [
    round(0.05 + 0.9 * ((index * 37) % 100) / 100.0, 4) for index in range(100)
]
_CALIBRATE_BALANCED_Y = [float(_CALIBRATE_BALANCED_P[index] > 0.5) for index in range(100)]
_CALIBRATE_IMBALANCED_P = [
    round(0.01 + 0.5 * ((index * 41) % 100) / 100.0, 4) for index in range(200)
]
_CALIBRATE_IMBALANCED_Y = [float(index % 20 == 0) for index in range(200)]


# --------------------------------------------------------------------------
# the table
# --------------------------------------------------------------------------

FIXTURES: tuple[Fixture, ...] = (
    Fixture(
        interface_id="calc.reduce",
        selector="row_count=small;column_count=*;reduction_kind=*;memory_fit=in_memory",
        fixture_id="quant-reduce-small",
        payload={
            "rows": _REDUCE_SMALL_ROWS,
            "group_by": ["region"],
            "aggregations": [
                {"column": "amount", "function": "sum", "alias": "total_amount"},
                {"column": "count", "function": "mean", "alias": "mean_count"},
            ],
        },
        expected_bucket=(
            "row_count=small;column_count=narrow;reduction_kind=grouped_aggregate;"
            "memory_fit=in_memory"
        ),
    ),
    Fixture(
        interface_id="calc.reduce",
        selector="row_count=medium;column_count=*;reduction_kind=*;memory_fit=in_memory",
        fixture_id="quant-reduce-medium",
        payload={
            "rows": _REDUCE_MEDIUM_ROWS,
            "aggregations": [
                {"column": "value", "function": "median", "alias": "median_value"},
                {"column": "value", "function": "max", "alias": "max_value"},
            ],
        },
        expected_bucket=(
            "row_count=medium;column_count=narrow;reduction_kind=scalar_aggregate;"
            "memory_fit=in_memory"
        ),
    ),
    Fixture(
        interface_id="ts.anomaly",
        selector=(
            "frequency=*;series_length=short;series_count=single;domain_class=counts;"
            "seasonality=*;gap_profile=regular"
        ),
        fixture_id="quant-anomaly-counts",
        payload={
            "series": _series(_ANOMALY_COUNTS),
            "season_length": 7,
            "method": "stl_mad",
        },
        expected_bucket=(
            "frequency=daily;series_length=short;series_count=single;domain_class=counts;"
            "seasonality=single_period;gap_profile=regular"
        ),
    ),
    Fixture(
        interface_id="ts.anomaly",
        selector=(
            "frequency=*;series_length=short;series_count=single;domain_class=rates;"
            "seasonality=*;gap_profile=regular"
        ),
        fixture_id="quant-anomaly-rates",
        payload={
            "series": _series(_ANOMALY_RATES),
            "season_length": 7,
            "method": "stl_mad",
        },
        expected_bucket=(
            "frequency=daily;series_length=short;series_count=single;domain_class=rates;"
            "seasonality=single_period;gap_profile=regular"
        ),
    ),
    Fixture(
        interface_id="ts.anomaly",
        selector=(
            "frequency=*;series_length=short;series_count=single;"
            "domain_class=continuous_bounded;seasonality=*;gap_profile=regular"
        ),
        fixture_id="quant-anomaly-bounded",
        payload={
            "series": _series(_ANOMALY_BOUNDED),
            "value_bounds": [0.0, 100.0],
            "season_length": 7,
            "method": "changepoint",
            "changepoint_count": 2,
        },
        expected_bucket=(
            "frequency=daily;series_length=short;series_count=single;"
            "domain_class=continuous_bounded;seasonality=single_period;gap_profile=regular"
        ),
    ),
    Fixture(
        interface_id="ts.anomaly",
        selector=(
            "frequency=*;series_length=short;series_count=single;"
            "domain_class=continuous_unbounded;seasonality=*;gap_profile=regular"
        ),
        fixture_id="quant-anomaly-unbounded",
        payload={
            "series": _series(_ANOMALY_UNBOUNDED),
            "season_length": 7,
            "method": "stl_mad",
            "threshold_modified_z": 3.5,
        },
        expected_bucket=(
            "frequency=daily;series_length=short;series_count=single;"
            "domain_class=continuous_unbounded;seasonality=single_period;gap_profile=regular"
        ),
    ),
    Fixture(
        interface_id="ts.forecast",
        selector=(
            "frequency=*;series_length=short;series_count=single;domain_class=counts;"
            "horizon=*;exogenous=none"
        ),
        fixture_id="quant-forecast-short-counts",
        payload={
            "series": _series(_FORECAST_SHORT_COUNTS),
            "season_length": 7,
            "horizon": 3,
            "model": "auto_ets",
            "interval_levels": [80, 95],
        },
        expected_bucket=(
            "frequency=daily;series_length=short;series_count=single;domain_class=counts;"
            "horizon=very_short;exogenous=none"
        ),
    ),
    Fixture(
        interface_id="ts.forecast",
        selector=(
            "frequency=*;series_length=short;series_count=single;"
            "domain_class=continuous_unbounded;horizon=*;exogenous=none"
        ),
        fixture_id="quant-forecast-short-continuous",
        payload={
            "series": _series(_FORECAST_SHORT_CONTINUOUS),
            "season_length": 7,
            "horizon": 6,
            "model": "auto_arima",
            "interval_levels": [80, 95],
        },
        expected_bucket=(
            "frequency=daily;series_length=short;series_count=single;"
            "domain_class=continuous_unbounded;horizon=short;exogenous=none"
        ),
    ),
    Fixture(
        interface_id="ts.forecast",
        selector=(
            "frequency=*;series_length=medium;series_count=single;domain_class=counts;"
            "horizon=*;exogenous=none"
        ),
        fixture_id="quant-forecast-medium-counts",
        payload={
            "series": _series(_FORECAST_MEDIUM_COUNTS),
            "season_length": 7,
            "horizon": 14,
            "model": "auto_ets",
            "interval_levels": [90],
        },
        expected_bucket=(
            "frequency=daily;series_length=medium;series_count=single;domain_class=counts;"
            "horizon=medium;exogenous=none"
        ),
    ),
    Fixture(
        interface_id="ts.forecast",
        selector=(
            "frequency=*;series_length=medium;series_count=single;"
            "domain_class=continuous_unbounded;horizon=*;exogenous=none"
        ),
        fixture_id="quant-forecast-medium-continuous",
        payload={
            "series": _series(_FORECAST_MEDIUM_CONTINUOUS),
            "season_length": 7,
            "horizon": 7,
            "model": "auto_ets",
            "interval_levels": [80, 95],
        },
        expected_bucket=(
            "frequency=daily;series_length=medium;series_count=single;"
            "domain_class=continuous_unbounded;horizon=short;exogenous=none"
        ),
    ),
    Fixture(
        interface_id="stat.test",
        selector="test_family=location;sample_size=*;design=independent;dimensionality=univariate",
        fixture_id="quant-stat-location-independent",
        payload={
            "test": "welch_t",
            "test_family": "location",
            "design": "independent",
            "alpha": 0.05,
            "alternative": "two-sided",
            "declared_assumptions": ["independent_observations", "normality"],
            "samples": {"a": _GROUP_A, "b": _GROUP_B},
        },
        expected_bucket=(
            "test_family=location;sample_size=small;design=independent;dimensionality=univariate"
        ),
    ),
    Fixture(
        interface_id="stat.test",
        selector="test_family=location;sample_size=*;design=paired;dimensionality=univariate",
        fixture_id="quant-stat-location-paired",
        payload={
            "test": "paired_t",
            "test_family": "location",
            "design": "paired",
            "alpha": 0.05,
            "declared_assumptions": ["normality"],
            "samples": {"after": _GROUP_B, "before": _GROUP_A},
        },
        expected_bucket=(
            "test_family=location;sample_size=small;design=paired;dimensionality=univariate"
        ),
    ),
    Fixture(
        interface_id="stat.test",
        selector=(
            "test_family=proportion;sample_size=*;design=independent;dimensionality=univariate"
        ),
        fixture_id="quant-stat-proportion",
        payload={
            "test": "proportions_z",
            "test_family": "proportion",
            "design": "independent",
            "alpha": 0.05,
            "declared_assumptions": ["independent_observations"],
            "samples": {"control": _SUCCESSES_B, "treatment": _SUCCESSES_A},
        },
        expected_bucket=(
            "test_family=proportion;sample_size=small;design=independent;dimensionality=univariate"
        ),
    ),
    Fixture(
        interface_id="stat.test",
        selector="test_family=variance;sample_size=*;design=independent;dimensionality=univariate",
        fixture_id="quant-stat-variance",
        payload={
            "test": "levene",
            "test_family": "variance",
            "design": "independent",
            "alpha": 0.05,
            "declared_assumptions": ["independent_observations"],
            "samples": {"narrow": _GROUP_A, "wide": _WIDE},
        },
        expected_bucket=(
            "test_family=variance;sample_size=small;design=independent;dimensionality=univariate"
        ),
    ),
    Fixture(
        interface_id="stat.test",
        selector="test_family=association;sample_size=*;design=paired;dimensionality=univariate",
        fixture_id="quant-stat-association",
        payload={
            "test": "pearson_r",
            "test_family": "association",
            "design": "paired",
            "alpha": 0.01,
            "declared_assumptions": ["normality"],
            "samples": {"x": _PAIRED_X, "y": _PAIRED_Y},
        },
        expected_bucket=(
            "test_family=association;sample_size=small;design=paired;dimensionality=univariate"
        ),
    ),
    Fixture(
        interface_id="stat.test",
        selector=(
            "test_family=distributional;sample_size=*;design=independent;dimensionality=univariate"
        ),
        fixture_id="quant-stat-distributional",
        payload={
            "test": "ks_2samp",
            "test_family": "distributional",
            "design": "independent",
            "alpha": 0.05,
            "declared_assumptions": ["independent_observations"],
            "samples": {"a": _GROUP_A, "b": _WIDE},
        },
        expected_bucket=(
            "test_family=distributional;sample_size=small;design=independent;"
            "dimensionality=univariate"
        ),
    ),
    Fixture(
        interface_id="score.rank",
        selector="candidate_count=small;signal_count=*;label_availability=*;tie_density=*",
        fixture_id="quant-rank-small",
        payload={
            "mode": "weighted",
            "objective": "exploitable-and-exposed first",
            "items": _rank_items(20, distinct=20),
            "weights": {"severity": 3.0, "exposure": 2.0, "age_days": -0.1},
        },
        expected_bucket=(
            "candidate_count=small;signal_count=few;label_availability=unlabeled;"
            "tie_density=sparse_ties"
        ),
    ),
    Fixture(
        interface_id="score.rank",
        selector="candidate_count=medium;signal_count=*;label_availability=*;tie_density=*",
        fixture_id="quant-rank-medium",
        payload={
            "mode": "weighted",
            "objective": "exploitable-and-exposed first",
            "items": _rank_items(400, distinct=20),
            "weights": {"severity": 3.0, "exposure": 2.0, "age_days": -0.1},
        },
        expected_bucket=(
            "candidate_count=medium;signal_count=few;label_availability=unlabeled;"
            "tie_density=heavy_ties"
        ),
    ),
    Fixture(
        interface_id="match.record",
        selector="pair_space=small;blocking=strong;field_richness=*;label_availability=unlabeled",
        fixture_id="quant-linkage-strong-blocking",
        payload={
            "records": _linkage_records(60, one_block=False),
            "comparisons": _LINKAGE_COMPARISONS,
            "blocking_fields": ["surname"],
            "prior_match_probability": 0.01,
            "threshold_match_probability": 0.5,
        },
        expected_bucket=(
            "pair_space=small;blocking=strong;field_richness=few_fields;"
            "label_availability=unlabeled"
        ),
    ),
    Fixture(
        interface_id="match.record",
        selector="pair_space=small;blocking=weak;field_richness=*;label_availability=unlabeled",
        fixture_id="quant-linkage-weak-blocking",
        payload={
            "records": _linkage_records(150, one_block=True),
            "comparisons": _LINKAGE_COMPARISONS,
            "blocking_fields": ["surname"],
            # A single block puts all 11175 candidate pairs through the model, so
            # the threshold is what keeps the response reviewable. The planted
            # duplicates clear it; agreement on surname and city alone does not.
            "prior_match_probability": 0.01,
            "threshold_match_probability": 0.9,
        },
        expected_bucket=(
            "pair_space=small;blocking=weak;field_richness=few_fields;label_availability=unlabeled"
        ),
    ),
    Fixture(
        interface_id="calc.calibrate",
        selector="outcome_type=binary;sample_size=*;resolution_lag=*;base_rate=balanced",
        fixture_id="quant-calibrate-balanced",
        payload={
            "predictions": _settled(_CALIBRATE_BALANCED_P, _CALIBRATE_BALANCED_Y),
            "bin_count": 10,
        },
        expected_bucket=(
            "outcome_type=binary;sample_size=small;resolution_lag=immediate;base_rate=balanced"
        ),
    ),
    Fixture(
        interface_id="calc.calibrate",
        selector=(
            "outcome_type=binary;sample_size=*;resolution_lag=*;base_rate=moderate_imbalance"
        ),
        fixture_id="quant-calibrate-imbalanced",
        payload={
            "predictions": _settled(_CALIBRATE_IMBALANCED_P, _CALIBRATE_IMBALANCED_Y),
            "bin_edges": [0.0, 0.05, 0.1, 0.25, 0.5, 1.0],
        },
        expected_bucket=(
            "outcome_type=binary;sample_size=moderate;resolution_lag=immediate;"
            "base_rate=moderate_imbalance"
        ),
    ),
)


def fixtures_for(interface_id: str) -> tuple[Fixture, ...]:
    return tuple(fixture for fixture in FIXTURES if fixture.interface_id == interface_id)
