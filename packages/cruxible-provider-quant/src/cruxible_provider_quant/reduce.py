"""``calc.reduce`` — deterministic windowed aggregation over a relation.

Engine: **Polars**, not DuckDB. The relation arrives as a typed payload over the
protocol pipe and is in memory by the time the provider sees it, so a dataframe
engine reduces it where it already is; DuckDB would mean standing up a database
and registering a table to run one aggregate. DuckDB earns its place the moment
this interface takes a *reference* to storage instead of the rows — an
``out_of_core`` implementation is a different implementation with its own digest
and its own claimed buckets, which is exactly how the bucket cube is meant to be
divided.

Determinism, concretely:

* group keys are sorted before the result is rendered, so group order never
  depends on hash iteration;
* rows inside a window are ordered by an explicitly named ``order_by`` column,
  and a window without one refuses rather than reducing over whatever order the
  payload happened to arrive in;
* the reduction asks Polars for a single thread. Floating-point addition is not
  associative, so a sum split across worker threads can differ in the last bits
  from run to run, and a baseline whose answer moves between two runs of the
  same input is not a baseline. The request is made through
  ``POLARS_MAX_THREADS`` at import time and it is only effective if nothing in
  the process imported Polars first — so the *observed* pool size is reported in
  the output's ``engine`` block rather than the requested one. Measured, not
  claimed, for the same reason buckets are.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from typing import Any

from cruxible_provider_runtime.provider_api import ProviderResult, ProviderRunContext

from .refusals import DeclineReason, decline
from .stdio import stdout_to_stderr

__all__ = ["AGGREGATIONS", "Reduce"]

# Must be set before the first ``import polars`` anywhere in the process. Set
# with ``setdefault`` so an operator who has deliberately configured the pool
# keeps their setting; the effective value is reported, never assumed.
os.environ.setdefault("POLARS_MAX_THREADS", "1")

AGGREGATIONS: tuple[str, ...] = ("sum", "mean", "min", "max", "count", "median", "std")
"""The closed set of reductions. An unknown name refuses; nothing is guessed."""

WINDOW_FUNCTIONS: tuple[str, ...] = ("sum", "mean", "min", "max", "std")


class Reduce:
    """Reduce a tabular payload to aggregates, in one of three shapes."""

    interface_id = "calc.reduce"

    def __call__(self, context: ProviderRunContext) -> ProviderResult:
        import polars as pl

        payload = context.input
        rows = payload.get("rows")
        if not isinstance(rows, Sequence) or not rows:
            return decline(DeclineReason.INVALID_PARAMETER, "rows must be a non-empty array")

        with stdout_to_stderr():
            frame = pl.DataFrame(list(rows))
        columns = set(frame.columns)

        window = payload.get("window")
        if isinstance(window, Mapping):
            return self._windowed(pl, frame, columns, window)

        group_by = [str(name) for name in (payload.get("group_by") or [])]
        missing = [name for name in group_by if name not in columns]
        if missing:
            return decline(
                DeclineReason.UNKNOWN_COLUMN,
                f"group_by names columns the relation does not carry: {missing}",
                columns=sorted(columns),
                missing=missing,
            )

        aggregations = payload.get("aggregations")
        if not isinstance(aggregations, Sequence) or not aggregations:
            return decline(
                DeclineReason.INVALID_PARAMETER,
                "a reduction without a window must name at least one aggregation",
            )
        expressions: list[Any] = []
        for spec in aggregations:
            built = self._aggregation(pl, spec, columns)
            if isinstance(built, ProviderResult):
                return built
            expressions.append(built)

        with stdout_to_stderr():
            if group_by:
                reduced = frame.group_by(group_by).agg(expressions).sort(group_by)
                kind = "grouped_aggregate"
            else:
                reduced = frame.select(expressions)
                kind = "scalar_aggregate"
            rendered = reduced.to_dicts()

        return ProviderResult.ok(
            {
                "reduction_kind": kind,
                "input_row_count": frame.height,
                "columns": list(reduced.columns),
                "rows": rendered,
                "engine": self._engine(pl),
            },
            metrics={"input_rows": float(frame.height), "output_rows": float(len(rendered))},
        )

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _engine(pl: Any) -> dict[str, Any]:
        return {
            "name": "polars",
            "version": str(pl.__version__),
            "thread_pool_size": int(pl.thread_pool_size()),
        }

    def _aggregation(self, pl: Any, spec: Any, columns: set[str]) -> Any | ProviderResult:
        if not isinstance(spec, Mapping):
            return decline(DeclineReason.INVALID_PARAMETER, "each aggregation must be an object")
        column = spec.get("column")
        function = spec.get("function")
        if not isinstance(column, str) or column not in columns:
            return decline(
                DeclineReason.UNKNOWN_COLUMN,
                f"aggregation names column {column!r}, which the relation does not carry",
                columns=sorted(columns),
            )
        if function not in AGGREGATIONS:
            return decline(
                DeclineReason.UNSUPPORTED_AGGREGATION,
                f"aggregation function {function!r} is not supported",
                supported=list(AGGREGATIONS),
            )
        alias = spec.get("alias") or f"{function}_{column}"
        return getattr(pl.col(column), str(function))().alias(str(alias))

    def _windowed(
        self, pl: Any, frame: Any, columns: set[str], window: Mapping[str, Any]
    ) -> ProviderResult:
        column = window.get("column")
        order_by = window.get("order_by")
        function = window.get("function")
        size = window.get("size")
        for name, value in (("column", column), ("order_by", order_by)):
            if not isinstance(value, str) or value not in columns:
                return decline(
                    DeclineReason.UNKNOWN_COLUMN,
                    f"window.{name} names {value!r}, which the relation does not carry",
                    columns=sorted(columns),
                )
        if function not in WINDOW_FUNCTIONS:
            return decline(
                DeclineReason.UNSUPPORTED_AGGREGATION,
                f"window function {function!r} is not supported",
                supported=list(WINDOW_FUNCTIONS),
            )
        if isinstance(size, bool) or not isinstance(size, int) or size < 1:
            return decline(
                DeclineReason.INVALID_PARAMETER,
                "window.size must be an integer of at least 1",
                size=size,
            )
        alias = str(window.get("alias") or f"{function}_{column}_{size}")

        with stdout_to_stderr():
            ordered = frame.sort(str(order_by))
            rolling = getattr(ordered[str(column)], f"rolling_{function}")(window_size=size)
            reduced = ordered.with_columns(rolling.alias(alias))
            rendered = reduced.to_dicts()

        return ProviderResult.ok(
            {
                "reduction_kind": "windowed",
                "input_row_count": frame.height,
                "columns": list(reduced.columns),
                "rows": rendered,
                "engine": {**self._engine(pl), "window_size": size},
            },
            metrics={"input_rows": float(frame.height), "output_rows": float(len(rendered))},
        )
