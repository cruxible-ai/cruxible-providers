"""Input-bucket classifiers for the seven quantitative interfaces.

**These belong to core, not to this package.** A classifier is registered with
its interface, and the bucket recorded on a run is derived by *the interface's*
classifier so that two implementations of the same slot are measured the same
way — which is the entire point of buckets, since the comparison they exist for
is a classical baseline against a narrow-ML model on the same key. Core's
interface registry does not exist yet, so RP-0 shipped the vocabularies as data
and a stub registry, and the classifiers have to live somewhere until the
registration surface lands. They live here, and they must move.

What they may and may not read
------------------------------

A classifier reads the **actual input payload** and nothing else. It never reads
the manifest, and it never reads a bucket the caller asserts. Where a dimension
is a property of a declared modelling choice rather than of the data — the
seasonal periods for ``ts.anomaly``, the test family for ``stat.test`` — the
classifier reads that choice *out of the input*, which is still a measurement of
what was asked for, and never out of the manifest, which would be a claim.

A classifier returns ``None`` when the input carries nothing it can place. The
registry turns that into ``unclassified_input``. Nothing here guesses.

Every classifier is pure Python: it runs in the executor process at admission,
before any environment is materialised, so it must not import an engine.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from itertools import pairwise
from typing import Any

from .series import parse_series, spacing_seconds

__all__ = [
    "CLASSIFIERS",
    "classify_calc_calibrate",
    "classify_calc_reduce",
    "classify_match_record",
    "classify_score_rank",
    "classify_stat_test",
    "classify_ts_anomaly",
    "classify_ts_forecast",
]

_HOUR = 3600.0
_DAY = 86400.0
_WEEK = 7 * _DAY

# Above this, spacings are too uneven to call a cadence. Fitted, not derived:
# it admits the ragged month-length case (~3% variation) and rejects a series
# whose gaps differ by more than a small multiple.
_IRREGULAR_CV = 0.10


def _sequence(payload: Mapping[str, Any], key: str) -> Sequence[Any] | None:
    value = payload.get(key)
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return None
    return value


# --------------------------------------------------------------------------
# shared series dimensions
# --------------------------------------------------------------------------


def _frequency(timestamps: Sequence[float]) -> str:
    spacing = spacing_seconds(timestamps)
    if spacing is None:
        return "irregular"
    mean, coefficient_of_variation = spacing
    if coefficient_of_variation > _IRREGULAR_CV:
        return "irregular"
    if mean < _HOUR:
        return "sub_hourly"
    if mean < _DAY:
        return "hourly"
    if mean < _WEEK:
        return "daily"
    return "weekly_or_slower"


def _series_length(length: int) -> str:
    if length < 30:
        return "very_short"
    if length < 200:
        return "short"
    if length < 2000:
        return "medium"
    return "long"


def _series_count(payload: Mapping[str, Any]) -> str:
    # One ``series`` key is one series. The dimension exists because a batched
    # implementation will claim the wider faces of this cube later.
    count = 1 if payload.get("series") is not None else 0
    if count <= 1:
        return "single"
    if count <= 10:  # pragma: no cover - reachable only for a batched payload
        return "few"
    if count <= 1000:  # pragma: no cover - same
        return "many"
    return "massive"  # pragma: no cover - same


def _is_count_like(values: Sequence[float]) -> bool:
    return all(value >= 0 and float(value).is_integer() for value in values)


def _domain_class(
    payload: Mapping[str, Any], values: Sequence[float], *, intermittent: bool
) -> str:
    """Measure the value domain, in a documented precedence.

    1. an explicit ``value_kind`` of ``categorical_encoded``, because no
       measurement can tell an encoded state apart from a small integer, and
       treating one as the other is the mistake the class exists to name;
    2. mostly-zero counts, where the interface has an ``intermittent`` class;
    3. non-negative integers -> counts;
    4. everything inside [0, 1] -> rates;
    5. an explicit ``value_bounds`` pair -> continuous_bounded;
    6. otherwise continuous_unbounded.
    """

    if payload.get("value_kind") == "categorical_encoded":
        return "categorical_encoded"
    counts = _is_count_like(values)
    if intermittent and counts and values and sum(1 for v in values if v == 0) / len(values) > 0.5:
        return "intermittent"
    if counts:
        return "counts"
    if values and all(0.0 <= value <= 1.0 for value in values):
        return "rates"
    bounds = payload.get("value_bounds")
    if isinstance(bounds, Sequence) and not isinstance(bounds, str | bytes) and len(bounds) == 2:
        return "continuous_bounded"
    return "continuous_unbounded"


def _declared_periods(payload: Mapping[str, Any]) -> list[int] | None:
    raw = payload.get("season_length")
    if raw is None:
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return [raw]
    if isinstance(raw, Sequence) and not isinstance(raw, str | bytes):
        periods: list[int] = []
        for item in raw:
            if isinstance(item, bool) or not isinstance(item, int):
                return None
            periods.append(item)
        return periods
    return None


# --------------------------------------------------------------------------
# calc.reduce
# --------------------------------------------------------------------------


def classify_calc_reduce(payload: Mapping[str, Any]) -> Mapping[str, str] | None:
    rows = _sequence(payload, "rows")
    if rows is None or not rows or not all(isinstance(row, Mapping) for row in rows):
        return None
    columns: set[str] = set()
    for row in rows:
        assert isinstance(row, Mapping)
        columns.update(str(name) for name in row)
    if not columns:
        return None

    count = len(rows)
    if count <= 10_000:
        row_count = "small"
    elif count <= 1_000_000:
        row_count = "medium"
    elif count <= 100_000_000:  # pragma: no cover - not constructible in a test
        row_count = "large"
    else:  # pragma: no cover - same
        row_count = "very_large"

    width = len(columns)
    column_count = "narrow" if width <= 10 else "moderate" if width <= 100 else "wide"

    if isinstance(payload.get("window"), Mapping):
        reduction_kind = "windowed"
    elif _sequence(payload, "group_by"):
        reduction_kind = "grouped_aggregate"
    else:
        reduction_kind = "scalar_aggregate"

    # A relation that arrived as a JSON payload over a pipe is in memory by
    # construction. ``out_of_core`` is reachable only by an implementation that
    # takes a reference to storage instead of the rows, which this one does not.
    return {
        "row_count": row_count,
        "column_count": column_count,
        "reduction_kind": reduction_kind,
        "memory_fit": "in_memory",
    }


# --------------------------------------------------------------------------
# ts.anomaly
# --------------------------------------------------------------------------


def _gap_profile(missing: Sequence[int], length: int) -> str:
    if not missing:
        return "regular"
    runs: list[int] = []
    current = 1
    for previous, index in pairwise(missing):
        if index == previous + 1:
            current += 1
        else:
            runs.append(current)
            current = 1
    runs.append(current)
    if max(runs) == 1 and len(missing) / max(length, 1) < 0.1:
        return "sparse_gaps"
    return "heavy_gaps"


def classify_ts_anomaly(payload: Mapping[str, Any]) -> Mapping[str, str] | None:
    series = parse_series(payload)
    if series is None:
        return None
    periods = _declared_periods(payload)
    if periods is None:
        seasonality = "unknown"
    elif not periods or all(period <= 1 for period in periods):
        seasonality = "none"
    elif len(periods) == 1:
        seasonality = "single_period"
    else:
        seasonality = "multi_period"
    return {
        "frequency": _frequency(series.timestamps),
        "series_length": _series_length(series.length),
        "series_count": _series_count(payload),
        "domain_class": _domain_class(payload, series.values, intermittent=False),
        "seasonality": seasonality,
        "gap_profile": _gap_profile(series.missing_indices, len(series.timestamps)),
    }


# --------------------------------------------------------------------------
# ts.forecast
# --------------------------------------------------------------------------


def classify_ts_forecast(payload: Mapping[str, Any]) -> Mapping[str, str] | None:
    series = parse_series(payload)
    if series is None:
        return None
    horizon = payload.get("horizon")
    if isinstance(horizon, bool) or not isinstance(horizon, int) or horizon < 1:
        return None
    if horizon <= 3:
        horizon_class = "very_short"
    elif horizon <= 12:
        horizon_class = "short"
    elif horizon <= 48:
        horizon_class = "medium"
    else:
        horizon_class = "long"

    covariates = payload.get("covariates")
    if not isinstance(covariates, Mapping) or not covariates:
        exogenous = "none"
    elif covariates.get("known_over_horizon") is True:
        exogenous = "known_future"
    else:
        exogenous = "past_only"

    return {
        "frequency": _frequency(series.timestamps),
        "series_length": _series_length(series.length),
        "series_count": _series_count(payload),
        "domain_class": _domain_class(payload, series.values, intermittent=True),
        "horizon": horizon_class,
        "exogenous": exogenous,
    }


# --------------------------------------------------------------------------
# stat.test
# --------------------------------------------------------------------------

_TEST_FAMILIES = frozenset(
    {"location", "proportion", "variance", "association", "distributional", "count_model"}
)


def classify_stat_test(payload: Mapping[str, Any]) -> Mapping[str, str] | None:
    family = payload.get("test_family")
    if family not in _TEST_FAMILIES:
        return None
    samples = payload.get("samples")
    if not isinstance(samples, Mapping) or not samples:
        return None

    group_sizes: list[int] = []
    variables = 0
    for group in samples.values():
        raw = group if isinstance(group, Sequence) and not isinstance(group, str | bytes) else None
        if raw is None:
            return None
        if raw and all(
            isinstance(row, Sequence) and not isinstance(row, str | bytes) for row in raw
        ):
            variables = max(variables, max(len(row) for row in raw))
        else:
            variables = max(variables, 1)
        group_sizes.append(len(raw))
    if not group_sizes or min(group_sizes) == 0:
        return None

    smallest = min(group_sizes)
    if smallest < 30:
        sample_size = "tiny"
    elif smallest < 200:
        sample_size = "small"
    elif smallest < 5000:
        sample_size = "moderate"
    else:
        sample_size = "large"

    declared_design = payload.get("design")
    if declared_design in {"independent", "paired", "clustered", "time_ordered"}:
        design = str(declared_design)
    else:
        return None

    if variables <= 1:
        dimensionality = "univariate"
    elif variables <= 10:
        dimensionality = "low_multivariate"
    else:
        dimensionality = "high_multivariate"

    return {
        "test_family": str(family),
        "sample_size": sample_size,
        "design": design,
        "dimensionality": dimensionality,
    }


# --------------------------------------------------------------------------
# score.rank
# --------------------------------------------------------------------------


def classify_score_rank(payload: Mapping[str, Any]) -> Mapping[str, str] | None:
    items = _sequence(payload, "items")
    if items is None or not items or not all(isinstance(item, Mapping) for item in items):
        return None
    signals: set[str] = set()
    vectors: list[tuple[tuple[str, Any], ...]] = []
    labelled = 0
    for item in items:
        assert isinstance(item, Mapping)
        signal = item.get("signals")
        if not isinstance(signal, Mapping) or not signal:
            return None
        signals.update(str(name) for name in signal)
        vectors.append(tuple(sorted((str(k), v) for k, v in signal.items())))
        if item.get("label") is not None:
            labelled += 1

    count = len(items)
    candidate_count = "small" if count <= 100 else "medium" if count <= 10_000 else "large"
    width = len(signals)
    signal_count = "single" if width == 1 else "few" if width <= 10 else "many"

    share_labelled = labelled / count
    if share_labelled == 0.0:
        label_availability = "unlabeled"
    elif share_labelled >= 0.99:
        label_availability = "labeled"
    else:
        label_availability = "partial"

    # Tie density has to be read before scoring, so it is measured on the thing
    # the scorer will see: items sharing an identical signal vector must score
    # identically under any deterministic scorer.
    duplicated = count - len(set(vectors))
    share_tied = duplicated / count
    if share_tied < 0.05:
        tie_density = "sparse_ties"
    elif share_tied < 0.5:
        tie_density = "moderate_ties"
    else:
        tie_density = "heavy_ties"

    return {
        "candidate_count": candidate_count,
        "signal_count": signal_count,
        "label_availability": label_availability,
        "tie_density": tie_density,
    }


# --------------------------------------------------------------------------
# match.record
# --------------------------------------------------------------------------


def classify_match_record(payload: Mapping[str, Any]) -> Mapping[str, str] | None:
    records = _sequence(payload, "records")
    comparisons = _sequence(payload, "comparisons")
    if records is None or comparisons is None or not records or not comparisons:
        return None
    if not all(isinstance(record, Mapping) for record in records):
        return None
    fields: list[str] = []
    for comparison in comparisons:
        if not isinstance(comparison, Mapping) or not isinstance(comparison.get("field"), str):
            return None
        fields.append(str(comparison["field"]))

    blocking_fields = [
        str(field)
        for field in (_sequence(payload, "blocking_fields") or [])
        if isinstance(field, str)
    ]
    blocks: dict[tuple[Any, ...], int] = {}
    for record in records:
        assert isinstance(record, Mapping)
        key = tuple(record.get(field) for field in blocking_fields)
        blocks[key] = blocks.get(key, 0) + 1
    if not blocking_fields:
        blocking = "none"
        largest = len(records)
    else:
        largest = max(blocks.values())
        blocking = "strong" if largest <= 100 else "weak"

    pairs = sum(size * (size - 1) // 2 for size in blocks.values())
    if pairs <= 1_000_000:
        pair_space = "small"
    elif pairs <= 1_000_000_000:  # pragma: no cover - not constructible in a test
        pair_space = "medium"
    else:  # pragma: no cover - same
        pair_space = "large"

    width = len(set(fields))
    field_richness = "single_field" if width == 1 else "few_fields" if width <= 5 else "rich"

    known = _sequence(payload, "known_matches")
    if not known:
        label_availability = "unlabeled"
    elif len(known) >= pairs:
        label_availability = "labeled"
    else:
        label_availability = "partial"

    return {
        "pair_space": pair_space,
        "blocking": blocking,
        "field_richness": field_richness,
        "label_availability": label_availability,
    }


# --------------------------------------------------------------------------
# calc.calibrate
# --------------------------------------------------------------------------


def classify_calc_calibrate(payload: Mapping[str, Any]) -> Mapping[str, str] | None:
    raw = _sequence(payload, "predictions")
    if raw is None or not raw or not all(isinstance(record, Mapping) for record in raw):
        return None

    predictions: list[Any] = []
    outcomes: list[float] = []
    made: list[float] = []
    settled: list[float] = []
    for record in raw:
        assert isinstance(record, Mapping)
        predictions.append(record.get("prediction"))
        outcome = record.get("outcome")
        if isinstance(outcome, bool):
            outcome = int(outcome)
        if not isinstance(outcome, int | float):
            return None
        outcomes.append(float(outcome))
        for key, sink in (("made_at", made), ("settled_at", settled)):
            moment = record.get(key)
            if isinstance(moment, bool) or not isinstance(moment, int | float):
                return None
            sink.append(float(moment))

    if any(isinstance(prediction, Mapping) for prediction in predictions):
        if not all(
            isinstance(prediction, Mapping) and {"lower", "upper"} <= set(prediction)
            for prediction in predictions
        ):
            return None
        outcome_type = "interval"
    elif all(outcome in (0.0, 1.0) for outcome in outcomes):
        outcome_type = "binary"
    elif all(float(outcome).is_integer() for outcome in outcomes):
        outcome_type = "categorical"
    else:
        outcome_type = "continuous"

    size = len(raw)
    if size < 30:
        sample_size = "tiny"
    elif size < 200:
        sample_size = "small"
    elif size < 5000:
        sample_size = "moderate"
    else:
        sample_size = "large"

    window = max(settled) - min(made)
    lags = sorted(settled[i] - made[i] for i in range(size))
    median_lag = lags[size // 2]
    if window <= 0:
        resolution_lag = "immediate"
    elif median_lag > window / 2:
        resolution_lag = "long_horizon"
    elif max(lags) <= window / size:
        resolution_lag = "immediate"
    else:
        resolution_lag = "bounded"

    tally: dict[float, int] = {}
    for outcome in outcomes:
        tally[outcome] = tally.get(outcome, 0) + 1
    rarest = min(tally.values()) / size if len(tally) > 1 else 1.0
    if math.isclose(rarest, 1.0) or rarest >= 0.2:
        base_rate = "balanced"
    elif rarest >= 0.01:
        base_rate = "moderate_imbalance"
    else:
        base_rate = "rare_event"

    return {
        "outcome_type": outcome_type,
        "sample_size": sample_size,
        "resolution_lag": resolution_lag,
        "base_rate": base_rate,
    }


CLASSIFIERS = {
    "calc.calibrate": classify_calc_calibrate,
    "calc.reduce": classify_calc_reduce,
    "match.record": classify_match_record,
    "score.rank": classify_score_rank,
    "stat.test": classify_stat_test,
    "ts.anomaly": classify_ts_anomaly,
    "ts.forecast": classify_ts_forecast,
}
