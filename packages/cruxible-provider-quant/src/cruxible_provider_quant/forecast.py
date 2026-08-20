"""``ts.forecast`` — classical forecasting with explicit horizon and intervals.

Engine: **statsforecast**, ``AutoARIMA`` and ``AutoETS``. The model is declared
by the caller and executed as declared. There is deliberately no "pick the best
model" mode: automatic model *selection across families* is the thing a later
narrow-ML implementation of this slot will do, and if this baseline did it too,
the track-record comparison between them would be a comparison of two different
questions.

Within a family the search is the model's own — ``AutoARIMA`` searches ARIMA
orders and ``AutoETS`` searches error/trend/season forms, both by information
criterion — and the selected specification is reported in ``model_selected`` so
that a receipt records what actually ran, not only what was asked for.

The output is typed and there is no confidence score anywhere in it. The horizon
is explicit, and the uncertainty is carried by **prediction intervals at
declared levels**: each level travels with its own lower and upper array, so a
reader can see the 80% and the 95% band disagree about how far the answer might
be off. That disagreement is the information. A single number claiming to
summarise it would be a worse artifact, and this is the slot where the
prediction Claim and its ResolutionContract will eventually settle — a
settlement can check an interval, and cannot check a confidence.

Determinism and tolerances:

* the same input produces bit-identical output within a machine, which the suite
  asserts by running each fixture twice;
* across machines the optimiser can land on marginally different coefficients
  with a different BLAS, so the suite asserts point forecasts to a relative
  tolerance of 1e-6 and asserts the *structural* properties exactly — interval
  nesting (95% contains 80%), lower below point below upper, one value per step,
  every value finite. Those are the properties a consumer relies on, and they
  are exact.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

from cruxible_provider_runtime.provider_api import ProviderResult, ProviderRunContext

from .outputs import ok_if_finite
from .refusals import DeclineReason, decline
from .series import Series, parse_series

__all__ = ["DEFAULT_INTERVAL_LEVELS", "MODELS", "Forecast"]

MODELS: tuple[str, ...] = ("auto_arima", "auto_ets")
DEFAULT_INTERVAL_LEVELS: tuple[int, ...] = (80, 95)
MAX_HORIZON = 512
"""A cap, so that a mistyped horizon refuses instead of allocating for an hour."""


class Forecast:
    """Forecast one series with a declared classical model."""

    interface_id = "ts.forecast"

    def __call__(self, context: ProviderRunContext) -> ProviderResult:
        payload = context.input
        series = parse_series(payload)
        if series is None:
            return decline(
                DeclineReason.INVALID_PARAMETER,
                "series must be a non-empty, strictly time-ordered array of "
                "{timestamp, value} records",
            )
        if series.missing_indices:
            return decline(
                DeclineReason.INVALID_PARAMETER,
                "this baseline forecasts a gapless series; imputation is a modelling "
                "act and belongs to the caller or to an implementation that declares it",
                missing=list(series.missing_indices),
            )
        if any(not math.isfinite(value) for value in series.values):
            return decline(DeclineReason.NON_FINITE_INPUT, "every series value must be finite")

        model_name = payload.get("model")
        if model_name not in MODELS:
            return decline(
                DeclineReason.UNKNOWN_METHOD,
                f"model {model_name!r} is not one this implementation fits",
                supported=list(MODELS),
            )

        horizon = payload.get("horizon")
        if (
            isinstance(horizon, bool)
            or not isinstance(horizon, int)
            or not 1 <= horizon <= MAX_HORIZON
        ):
            return decline(
                DeclineReason.INVALID_PARAMETER,
                f"horizon must be an integer between 1 and {MAX_HORIZON}",
                horizon=horizon,
            )

        season_length = self._season_length(payload)
        if isinstance(season_length, ProviderResult):
            return season_length

        required = max(2 * season_length, 10)
        if series.length < required:
            return decline(
                DeclineReason.INSUFFICIENT_SERIES_LENGTH,
                f"a seasonal fit at period {season_length} needs at least {required} "
                f"observations, and the series carries {series.length}",
                observations=series.length,
                required=required,
                season_length=season_length,
            )

        levels = self._levels(payload)
        if isinstance(levels, ProviderResult):
            return levels

        return self._fit(str(model_name), series, horizon, season_length, levels)

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _season_length(payload: Any) -> int | ProviderResult:
        declared = payload.get("season_length", 1)
        if isinstance(declared, Sequence) and not isinstance(declared, str | bytes):
            if len(declared) != 1:
                return decline(
                    DeclineReason.INVALID_PARAMETER,
                    "this baseline fits one seasonal period; a multi-period series "
                    "needs an implementation that declares it",
                    season_length=list(declared),
                )
            declared = declared[0]
        if isinstance(declared, bool) or not isinstance(declared, int) or declared < 1:
            return decline(
                DeclineReason.INVALID_PARAMETER,
                "season_length must be an integer of at least 1",
                season_length=payload.get("season_length"),
            )
        return declared

    @staticmethod
    def _levels(payload: Any) -> list[int] | ProviderResult:
        declared = payload.get("interval_levels", list(DEFAULT_INTERVAL_LEVELS))
        if not isinstance(declared, Sequence) or isinstance(declared, str | bytes) or not declared:
            return decline(
                DeclineReason.INVALID_PARAMETER,
                "interval_levels must be a non-empty array of levels",
                interval_levels=declared,
            )
        levels: list[int] = []
        for level in declared:
            if isinstance(level, bool) or not isinstance(level, int) or not 1 <= level <= 99:
                return decline(
                    DeclineReason.INVALID_PARAMETER,
                    "every interval level must be an integer percentage between 1 and 99",
                    interval_levels=list(declared),
                )
            levels.append(level)
        return sorted(set(levels))

    def _fit(
        self, model_name: str, series: Series, horizon: int, season_length: int, levels: list[int]
    ) -> ProviderResult:
        import numpy as np
        import statsforecast
        from statsforecast.models import AutoARIMA, AutoETS

        values = np.asarray(series.values, dtype=np.float64)
        model: Any = (
            AutoARIMA(season_length=season_length)
            if model_name == "auto_arima"
            else AutoETS(season_length=season_length)
        )
        model.fit(values)
        predicted = model.predict(h=horizon, level=levels)

        point = [float(value) for value in np.asarray(predicted["mean"], dtype=np.float64)]
        intervals = [
            {
                "level": level,
                "lower": [float(v) for v in np.asarray(predicted[f"lo-{level}"], dtype=np.float64)],
                "upper": [float(v) for v in np.asarray(predicted[f"hi-{level}"], dtype=np.float64)],
            }
            for level in levels
        ]
        if not all(math.isfinite(value) for value in point):
            return decline(
                DeclineReason.NON_FINITE_INPUT,
                "the fitted model produced a non-finite forecast; the series is not one "
                "this baseline can fit",
                model=model_name,
            )

        return ok_if_finite(
            {
                "model": model_name,
                "model_selected": self._selected(model),
                "horizon": horizon,
                "season_length": season_length,
                "point_forecast": point,
                "prediction_intervals": intervals,
                "engine": {
                    "name": f"statsforecast.{type(model).__name__}",
                    "version": str(statsforecast.__version__),
                },
            },
            metrics={
                "observations": float(series.length),
                "horizon": float(horizon),
            },
        )

    @staticmethod
    def _selected(model: Any) -> str:
        """The specification the family search landed on, as the model reports it."""

        try:
            return str(model.model_["arma"] if "arma" in model.model_ else model.model_["method"])
        except (AttributeError, KeyError, TypeError):  # pragma: no cover - version drift
            return type(model).__name__
