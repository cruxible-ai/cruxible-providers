"""``calc.calibrate`` — a deterministic calibration reducer over settled pairs.

Brier score, its three-component decomposition, reliability binning, and the
expected calibration error over the same bins. No engine beyond NumPy: every
quantity here is a closed-form reduction of the pairs, so bringing in a fitting
library would add a dependency and a stochastic surface to arithmetic that has
neither.

Calibration is *computed from settled outcomes, never asserted*. The input is a
list of predictions each of which already has an outcome and a settlement time;
this interface has nothing to say about an unsettled prediction, and it does not
invent one.

The output carries no single "how calibrated is it" number, and the omission is
the point. A Brier score, a reliability component, and an expected calibration
error disagree with each other on purpose — they weight the same miscalibration
differently — and collapsing them into one figure would destroy the disagreement
that makes them useful. Each travels as a named field beside the bin edges it
was computed over.

Determinism: the reduction is a fixed sequence of NumPy operations over a fixed
ordering, so repeated runs on the same input are bit-identical on one machine.
Across machines the last bits of a float sum can differ with the BLAS build, so
the suite asserts these values with an explicit relative tolerance of 1e-12,
which is roughly four orders of magnitude tighter than any calibration claim
anyone would make from them.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from cruxible_provider_runtime.errors import RefusalCode
from cruxible_provider_runtime.provider_api import ProviderResult, ProviderRunContext

from .outputs import ok_if_finite
from .refusals import decline

__all__ = ["DEFAULT_BIN_COUNT", "Calibrate"]

DEFAULT_BIN_COUNT = 10


class Calibrate:
    """Assess the calibration of a set of settled binary predictions."""

    interface_id = "calc.calibrate"

    def __call__(self, context: ProviderRunContext) -> ProviderResult:
        import numpy as np

        payload = context.input
        records = payload.get("predictions")
        if not isinstance(records, Sequence) or not records:
            return decline(RefusalCode.INVALID_PARAMETER, "predictions must be a non-empty array")

        predictions: list[float] = []
        outcomes: list[float] = []
        for record in records:
            if not isinstance(record, Mapping):
                return decline(
                    RefusalCode.INVALID_PARAMETER, "each prediction record must be an object"
                )
            prediction = record.get("prediction")
            outcome = record.get("outcome")
            if isinstance(outcome, bool):
                outcome = int(outcome)
            if not isinstance(prediction, int | float) or isinstance(prediction, bool):
                return decline(
                    RefusalCode.INVALID_PARAMETER,
                    "a binary calibration reading needs a numeric probability per record",
                )
            if not isinstance(outcome, int | float):
                return decline(RefusalCode.INVALID_PARAMETER, "each record needs a numeric outcome")
            if not math.isfinite(float(prediction)) or not math.isfinite(float(outcome)):
                return decline(
                    RefusalCode.NON_FINITE_INPUT,
                    "predictions and outcomes must be finite",
                )
            if not 0.0 <= float(prediction) <= 1.0:
                return decline(
                    RefusalCode.INVALID_PARAMETER,
                    "a probability outside [0, 1] is not a probability",
                    prediction=float(prediction),
                )
            predictions.append(float(prediction))
            outcomes.append(float(outcome))

        if len(predictions) != len(outcomes):  # pragma: no cover - built in lockstep above
            return decline(
                RefusalCode.MISMATCHED_LENGTHS,
                "predictions and outcomes must be the same length",
            )

        edges = self._bin_edges(payload)
        if isinstance(edges, ProviderResult):
            return edges

        p = np.asarray(predictions, dtype=np.float64)
        y = np.asarray(outcomes, dtype=np.float64)
        size = int(p.size)
        base_rate = float(y.mean())
        brier = float(np.mean((p - y) ** 2))

        bins: list[dict[str, Any]] = []
        reliability = 0.0
        resolution = 0.0
        calibration_error = 0.0
        edge_array = np.asarray(edges, dtype=np.float64)
        # Right-closed bins with the first bin closed on the left, so that both
        # 0.0 and 1.0 land in a bin and no prediction falls outside the cover.
        index = np.clip(np.searchsorted(edge_array[1:-1], p, side="right"), 0, len(edges) - 2)
        for position in range(len(edges) - 1):
            members = index == position
            count = int(members.sum())
            entry: dict[str, Any] = {
                "lower": float(edges[position]),
                "upper": float(edges[position + 1]),
                "count": count,
                "mean_prediction": None,
                "observed_frequency": None,
            }
            if count:
                mean_prediction = float(p[members].mean())
                observed = float(y[members].mean())
                entry["mean_prediction"] = mean_prediction
                entry["observed_frequency"] = observed
                weight = count / size
                reliability += weight * (mean_prediction - observed) ** 2
                resolution += weight * (observed - base_rate) ** 2
                calibration_error += weight * abs(mean_prediction - observed)
            bins.append(entry)

        uncertainty = base_rate * (1.0 - base_rate)

        return ok_if_finite(
            {
                "outcome_type": "binary",
                "sample_size": size,
                "base_rate": base_rate,
                "brier_score": brier,
                "brier_decomposition": {
                    "reliability": reliability,
                    "resolution": resolution,
                    "uncertainty": uncertainty,
                },
                "expected_calibration_error": calibration_error,
                "bin_edges": [float(edge) for edge in edges],
                "reliability_bins": bins,
                "engine": {"name": "numpy", "version": str(np.__version__)},
            },
            metrics={"sample_size": float(size), "brier_score": brier},
        )

    @staticmethod
    def _bin_edges(payload: Mapping[str, Any]) -> list[float] | ProviderResult:
        declared = payload.get("bin_edges")
        if declared is not None:
            if (
                not isinstance(declared, Sequence)
                or isinstance(declared, str | bytes)
                or len(declared) < 2
            ):
                return decline(
                    RefusalCode.INVALID_PARAMETER, "bin_edges must list at least two edges"
                )
            edges = []
            for edge in declared:
                if isinstance(edge, bool) or not isinstance(edge, int | float):
                    return decline(RefusalCode.INVALID_PARAMETER, "bin edges must be numbers")
                if not math.isfinite(float(edge)):
                    return decline(RefusalCode.NON_FINITE_INPUT, "bin edges must be finite")
                edges.append(float(edge))
            if edges != sorted(edges) or len(set(edges)) != len(edges):
                return decline(
                    RefusalCode.INVALID_PARAMETER,
                    "bin edges must be strictly increasing",
                    bin_edges=edges,
                )
            # Edges that leave part of [0, 1] uncovered have no honest reading.
            # Predictions are probabilities, so every one of them belongs
            # somewhere, and the binning clips whatever falls outside into the
            # nearest bin: with edges [0.2, 0.8] a prediction of 0.1 is reported
            # as though it had been observed inside [0.2, 0.8]. Inventing
            # underflow and overflow bins instead would put edges in the output
            # that the caller never declared, so this refuses and says what is
            # missing.
            if edges[0] > 0.0 or edges[-1] < 1.0:
                return decline(
                    RefusalCode.INVALID_PARAMETER,
                    "bin edges must cover [0, 1]; a prediction outside the declared edges "
                    "has no bin and must not be reported inside one",
                    bin_edges=edges,
                )
            return edges
        count = payload.get("bin_count", DEFAULT_BIN_COUNT)
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            return decline(
                RefusalCode.INVALID_PARAMETER,
                "bin_count must be an integer of at least 1",
                bin_count=count,
            )
        return [position / count for position in range(count + 1)]
