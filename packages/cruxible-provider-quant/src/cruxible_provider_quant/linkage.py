"""``match.record`` — probabilistic record linkage, as evidence for review.

Engine: **splink** (DuckDB backend, in process, no file and no server), running
a Fellegi-Sunter model whose parameters are **declared, never trained**. Every
comparison in the request carries its own m and u probability, and the prior —
``prior_match_probability``, splink's ``probability_two_random_records_match`` —
is required rather than defaulted. splink will happily supply a default prior
and warn about it; a provider that let that happen would be shipping a match
weight whose most influential parameter nobody chose.

That is also the determinism story. Expectation-maximisation training is
iterative and its result depends on the starting point and the data ordering, so
this implementation never runs it: with fixed m/u and a fixed prior the whole
computation is a sum of logs over comparison outcomes, identical run to run. The
suite asserts match weights to a relative tolerance of 1e-9 and comparison
vectors exactly.

**Never auto-merge.** The output is a list of scored candidate pairs and
nothing else — no clusters, no merge instructions, no surviving-record choice.
``review_required`` is ``true`` on every response, because deciding that two
records are the same entity is a governed act that happens through the pathway
that already exists for it. A linkage score is evidence brought to that
decision; it is not the decision, and this interface has no spelling for making
one.

The trained variant of this slot — same interface, same track-record key, m/u
estimated from confirmed matches — is a different implementation with its own
digest, and it will claim the ``label_availability=partial`` and ``labeled``
buckets this one deliberately does not.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from cruxible_provider_runtime.provider_api import ProviderResult, ProviderRunContext

from .outputs import ok_if_finite
from .refusals import DeclineReason, decline

__all__ = ["DEFAULT_THRESHOLD", "RecordLinkage"]

DEFAULT_THRESHOLD = 0.0
"""Return every candidate pair by default: filtering is the reviewer's call."""


class RecordLinkage:
    """Score candidate record pairs with a declared Fellegi-Sunter model."""

    interface_id = "match.record"

    def __call__(self, context: ProviderRunContext) -> ProviderResult:
        payload = context.input

        records = payload.get("records")
        if not isinstance(records, Sequence) or isinstance(records, str | bytes) or not records:
            return decline(DeclineReason.INVALID_PARAMETER, "records must be a non-empty array")
        if not all(isinstance(record, Mapping) for record in records):
            return decline(DeclineReason.INVALID_PARAMETER, "each record must be an object")

        identifier_field = payload.get("unique_id_field", "unique_id")
        if not isinstance(identifier_field, str) or not identifier_field:
            return decline(
                DeclineReason.INVALID_PARAMETER, "unique_id_field must be a non-empty string"
            )
        missing_id = [
            index for index, record in enumerate(records) if identifier_field not in record
        ]
        if missing_id:
            return decline(
                DeclineReason.UNKNOWN_COLUMN,
                f"records carry no {identifier_field!r} field",
                unique_id_field=identifier_field,
                positions=missing_id[:5],
            )

        prior = payload.get("prior_match_probability")
        if isinstance(prior, bool) or not isinstance(prior, int | float) or not 0.0 < prior < 1.0:
            return decline(
                DeclineReason.UNDECLARED_MATCH_PARAMETERS,
                "prior_match_probability must be declared, strictly between 0 and 1; the "
                "engine's default prior is a parameter nobody chose",
                prior_match_probability=prior,
            )

        comparisons = self._comparisons(payload, records)
        if isinstance(comparisons, ProviderResult):
            return comparisons

        blocking_fields = [
            str(field) for field in (payload.get("blocking_fields") or []) if isinstance(field, str)
        ]
        columns = {name for record in records for name in record}
        unknown_blocking = [field for field in blocking_fields if field not in columns]
        if unknown_blocking:
            return decline(
                DeclineReason.UNKNOWN_COLUMN,
                f"blocking_fields name columns the records do not carry: {unknown_blocking}",
                columns=sorted(str(name) for name in columns),
            )

        threshold = payload.get("threshold_match_probability", DEFAULT_THRESHOLD)
        if (
            isinstance(threshold, bool)
            or not isinstance(threshold, int | float)
            or not 0.0 <= threshold <= 1.0
        ):
            return decline(
                DeclineReason.INVALID_PARAMETER,
                "threshold_match_probability must be a number in [0, 1]",
                threshold_match_probability=threshold,
            )

        return self._link(
            list(records),
            str(identifier_field),
            comparisons,
            blocking_fields,
            float(prior),
            float(threshold),
        )

    # -- input -------------------------------------------------------------

    @staticmethod
    def _comparisons(
        payload: Any, records: Sequence[Any]
    ) -> list[tuple[str, float, float]] | ProviderResult:
        raw = payload.get("comparisons")
        if not isinstance(raw, Sequence) or isinstance(raw, str | bytes) or not raw:
            return decline(DeclineReason.INVALID_PARAMETER, "comparisons must be a non-empty array")
        columns = {name for record in records for name in record}
        parsed: list[tuple[str, float, float]] = []
        for comparison in raw:
            if not isinstance(comparison, Mapping):
                return decline(DeclineReason.INVALID_PARAMETER, "each comparison must be an object")
            field = comparison.get("field")
            if not isinstance(field, str) or field not in columns:
                return decline(
                    DeclineReason.UNKNOWN_COLUMN,
                    f"comparison names field {field!r}, which the records do not carry",
                    columns=sorted(str(name) for name in columns),
                )
            probabilities: list[float] = []
            for key in ("m_probability", "u_probability"):
                value = comparison.get(key)
                if (
                    isinstance(value, bool)
                    or not isinstance(value, int | float)
                    or not 0.0 < value < 1.0
                    or not math.isfinite(float(value))
                ):
                    return decline(
                        DeclineReason.UNDECLARED_MATCH_PARAMETERS,
                        f"comparison on {field!r} must declare {key} strictly between 0 "
                        "and 1; this implementation never estimates them",
                        field=field,
                        parameter=key,
                        value=value,
                    )
                probabilities.append(float(value))
            parsed.append((field, probabilities[0], probabilities[1]))
        return parsed

    # -- execution ---------------------------------------------------------

    def _link(
        self,
        records: list[Any],
        identifier_field: str,
        comparisons: list[tuple[str, float, float]],
        blocking_fields: list[str],
        prior: float,
        threshold: float,
    ) -> ProviderResult:
        import pandas as pd
        import splink
        import splink.comparison_level_library as cll
        from splink import DuckDBAPI, Linker, SettingsCreator, block_on
        from splink.comparison_library import CustomComparison

        frame = pd.DataFrame([dict(record) for record in records])
        if identifier_field != "unique_id":
            frame = frame.rename(columns={identifier_field: "unique_id"})

        settings = SettingsCreator(
            link_type="dedupe_only",
            probability_two_random_records_match=prior,
            comparisons=[
                CustomComparison(
                    output_column_name=field,
                    comparison_description=f"exact match on {field}",
                    comparison_levels=[
                        cll.NullLevel(field),
                        cll.ExactMatchLevel(field).configure(m_probability=m, u_probability=u),
                        cll.ElseLevel().configure(m_probability=1.0 - m, u_probability=1.0 - u),
                    ],
                )
                for field, m, u in comparisons
            ],
            # Defensive, and unreachable today. An empty ``blocking_fields``
            # classifies to ``blocking=none``, which this implementation does
            # not claim, so admission refuses with ``unclaimed_bucket`` before
            # anything gets here — an unblocked cross product is a different
            # scaling problem and is left to an implementation that says so.
            # The fallback stays because it is the *correct* rule if that bucket
            # is ever claimed (``block_on("1")`` is the full cross product,
            # which is exactly what ``blocking=none`` means), and because an
            # empty rule list would otherwise reach splink as a silent
            # misconfiguration rather than as an explicit choice.
            blocking_rules_to_generate_predictions=[block_on(field) for field in blocking_fields]
            or [block_on("1")],
            retain_intermediate_calculation_columns=True,
        )

        linker = Linker(frame, settings, db_api=DuckDBAPI())
        # splink ships a py.typed marker but leaves this accessor unannotated,
        # so a strict check reads it as an untyped call in typed context. The
        # ignore is pinned to the one call rather than widened to the module:
        # everything else splink exposes here does type-check, and the row
        # values are re-typed field by field immediately below.
        predicted = linker.inference.predict(
            threshold_match_probability=threshold
        ).as_pandas_dataframe()  # type: ignore[no-untyped-call]

        pairs: list[dict[str, Any]] = []
        for row in predicted.to_dict("records"):
            pairs.append(
                {
                    "left_id": str(row["unique_id_l"]),
                    "right_id": str(row["unique_id_r"]),
                    "match_weight": float(row["match_weight"]),
                    "match_probability": float(row["match_probability"]),
                    "comparison_vector": {
                        field: int(row[f"gamma_{field}"]) for field, _, _ in comparisons
                    },
                }
            )
        pairs.sort(key=lambda pair: (pair["left_id"], pair["right_id"]))

        return ok_if_finite(
            {
                "link_type": "dedupe_only",
                "prior_match_probability": prior,
                "threshold_match_probability": threshold,
                "blocking_fields": blocking_fields,
                "candidate_pair_count": len(pairs),
                "pairs": pairs,
                # Standing, and not a computed field. A linkage score is evidence
                # brought to a governed identity decision; this interface has no
                # spelling for making one.
                "review_required": True,
                "engine": {
                    "name": "splink",
                    "version": str(splink.__version__),
                    "backend": "duckdb",
                    "parameters": "declared",
                    "trained": False,
                },
            },
            metrics={"records": float(len(records)), "candidate_pairs": float(len(pairs))},
        )
