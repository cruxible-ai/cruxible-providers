"""``ts.anomaly`` — STL-residual scoring and changepoint detection.

Two declared methods, never auto-selected:

``stl_mad``
    Decompose the series with STL (statsmodels, robust fitting) and score each
    residual against the **median absolute deviation** of the residuals. The
    reported per-point quantity is the modified z-score,
    ``0.6745 * residual / MAD`` — a named statistic against a named scale
    estimate, both of which travel in the output. It is not a confidence and it
    is not comparable across series with different scale estimates, which is
    exactly why the scale estimate is reported beside it.

``changepoint``
    Binary segmentation over the series (ruptures, ``l2`` cost) for an
    explicitly declared number of changepoints. The count is declared because
    penalty-selected changepoint counts are a modelling choice, and a slot that
    made that choice silently would hide it from the track record.

A flag is a reading. It enters a Capture under the declared CaptureContract with
a contract-governed grade, and it is never world-state truth on its own; a run
that flags nothing and a run that flags everything are both successful runs of
this interface.

Determinism: STL is a deterministic LOESS fit and binary segmentation with a
declared breakpoint count is a deterministic search, so both are reproducible
run to run. Across platforms the residuals can move in the last bits, so the
suite asserts flagged *indices* and changepoint *positions* exactly — they are
integers, and they are the answer — and residual magnitudes to a relative
tolerance of 1e-9.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

from cruxible_provider_runtime.errors import RefusalCode
from cruxible_provider_runtime.provider_api import ProviderResult, ProviderRunContext

from .outputs import ok_if_finite
from .refusals import decline
from .series import Series, parse_series

__all__ = ["DEFAULT_MODIFIED_Z_THRESHOLD", "METHODS", "Anomaly"]

METHODS: tuple[str, ...] = ("stl_mad", "changepoint")

DEFAULT_MODIFIED_Z_THRESHOLD = 3.5
"""The conventional modified-z cutoff. Declared in the output, never implied."""

_MAD_TO_SIGMA = 0.6745
"""Scales the MAD to a standard-deviation-equivalent for a normal sample."""

_DEGENERATE_MAD_RELATIVE = 1e-12
"""Below this fraction of the series magnitude, a MAD is decomposition noise."""


class Anomaly:
    """Flag anomalous points or intervals in one time series."""

    interface_id = "ts.anomaly"

    def __call__(self, context: ProviderRunContext) -> ProviderResult:
        payload = context.input
        series = parse_series(payload)
        if series is None:
            return decline(
                RefusalCode.INVALID_PARAMETER,
                "series must be a non-empty, strictly time-ordered array of "
                "{timestamp, value} records",
            )
        if any(not math.isfinite(value) for value in series.values):
            return decline(RefusalCode.NON_FINITE_INPUT, "every series value must be finite")

        method = payload.get("method")
        if method == "stl_mad":
            return self._stl_mad(payload, series)
        if method == "changepoint":
            return self._changepoint(payload, series)
        return decline(
            RefusalCode.UNKNOWN_METHOD,
            f"method {method!r} is not one this implementation performs",
            supported=list(METHODS),
        )

    # -- stl_mad -----------------------------------------------------------

    def _stl_mad(self, payload: Any, series: Series) -> ProviderResult:
        import numpy as np
        import statsmodels
        from statsmodels.tsa.seasonal import STL

        period = payload.get("season_length")
        if isinstance(period, Sequence) and not isinstance(period, str | bytes):
            if len(period) != 1:
                return decline(
                    RefusalCode.INVALID_PARAMETER,
                    "STL decomposes one period; a multi-period series needs an "
                    "implementation that says so",
                    season_length=list(period),
                )
            period = period[0]
        if isinstance(period, bool) or not isinstance(period, int) or period < 2:
            return decline(
                RefusalCode.INVALID_PARAMETER,
                "season_length must be an integer of at least 2 for STL",
                season_length=payload.get("season_length"),
            )
        if series.length < 2 * period:
            return decline(
                RefusalCode.INSUFFICIENT_SERIES_LENGTH,
                f"STL at period {period} needs at least {2 * period} observations, "
                f"and the series carries {series.length}",
                observations=series.length,
                required=2 * period,
                season_length=period,
            )

        threshold = payload.get("threshold_modified_z", DEFAULT_MODIFIED_Z_THRESHOLD)
        if isinstance(threshold, bool) or not isinstance(threshold, int | float) or threshold <= 0:
            return decline(
                RefusalCode.INVALID_PARAMETER,
                "threshold_modified_z must be a positive number",
                threshold_modified_z=threshold,
            )
        threshold = float(threshold)

        values = np.asarray(series.values, dtype=np.float64)
        fitted = STL(values, period=period, robust=True).fit()
        residual = np.asarray(fitted.resid, dtype=np.float64)

        median_residual = float(np.median(residual))
        mad = float(np.median(np.abs(residual - median_residual)))
        # Not ``mad == 0.0``. A flat series decomposes to residuals on the order
        # of 1e-15 rather than to exact zeros, and dividing by that produces
        # modified z-scores in the 1e15 range which would flag every point in a
        # series that has no variation at all. The floor is relative to the
        # series magnitude, because "no dispersion" is a statement about scale.
        floor = _DEGENERATE_MAD_RELATIVE * max(1.0, float(np.max(np.abs(values))))
        if mad <= floor:
            return decline(
                RefusalCode.DEGENERATE_SCALE,
                "the residual median absolute deviation is indistinguishable from zero "
                "at the series' own scale, so no residual can be scored against it",
                season_length=period,
                scale_estimate=mad,
                floor=floor,
            )

        modified_z = _MAD_TO_SIGMA * (residual - median_residual) / mad
        flagged = [index for index, score in enumerate(modified_z) if abs(score) > threshold]
        points = [
            {
                "index": index,
                "timestamp": series.timestamps[index],
                "value": float(values[index]),
                "residual": float(residual[index]),
                "modified_z": float(modified_z[index]),
                "flagged": index in set(flagged),
            }
            for index in range(series.length)
        ]

        return ok_if_finite(
            {
                "method": "stl_mad",
                "season_length": period,
                "scale_estimate": {
                    "kind": "median_absolute_deviation",
                    "value": mad,
                    "median_residual": median_residual,
                    "mad_to_sigma": _MAD_TO_SIGMA,
                },
                "threshold": {"kind": "modified_z", "value": threshold},
                "points": points,
                "flagged_indices": flagged,
                "changepoints": [],
                "segments": [],
                "engine": {
                    "name": "statsmodels.tsa.seasonal.STL",
                    "version": str(statsmodels.__version__),
                    "robust": True,
                },
            },
            metrics={
                "observations": float(series.length),
                "flagged": float(len(flagged)),
                "scale_estimate": mad,
            },
        )

    # -- changepoint -------------------------------------------------------

    def _changepoint(self, payload: Any, series: Series) -> ProviderResult:
        import numpy as np
        import ruptures as rpt

        count = payload.get("changepoint_count")
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            return decline(
                RefusalCode.INVALID_PARAMETER,
                "changepoint_count must be a declared integer of at least 1; a "
                "penalty-selected count is a modelling choice this slot will not make "
                "silently",
                changepoint_count=count,
            )
        minimum = 2 * (count + 1)
        if series.length < minimum:
            return decline(
                RefusalCode.INSUFFICIENT_SERIES_LENGTH,
                f"{count} changepoints need at least {minimum} observations, and the "
                f"series carries {series.length}",
                observations=series.length,
                required=minimum,
                changepoint_count=count,
            )

        values = np.asarray(series.values, dtype=np.float64).reshape(-1, 1)
        algorithm = rpt.Binseg(model="l2", min_size=2, jump=1).fit(values)
        boundaries = [int(edge) for edge in algorithm.predict(n_bkps=count)]

        starts = [0, *boundaries[:-1]]
        segments = [
            {
                "start": start,
                "end": end,
                "length": end - start,
                "mean": float(values[start:end, 0].mean()),
                "std": float(values[start:end, 0].std(ddof=0)),
            }
            for start, end in zip(starts, boundaries, strict=True)
        ]

        return ok_if_finite(
            {
                "method": "changepoint",
                "season_length": 0,
                "scale_estimate": {"kind": "none", "value": None},
                "threshold": {"kind": "declared_changepoint_count", "value": float(count)},
                "points": [],
                "flagged_indices": [],
                "changepoints": boundaries[:-1],
                "segments": segments,
                "engine": {
                    "name": "ruptures.Binseg",
                    "version": str(rpt.__version__),
                    "cost_model": "l2",
                },
            },
            metrics={"observations": float(series.length), "changepoints": float(count)},
        )
