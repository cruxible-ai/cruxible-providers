"""Stub registrations for the seven quantitative slot interfaces.

**These are stubs.** Real slot interfaces are registered in core with a digest
over their input/output/refusal schema, and the bucket vocabularies under
``vocab/interfaces/`` are the draft data core will register. Neither surface
exists yet, so this module pins a stub digest per interface so that RP-2's
conformance suite has something to bind against, exactly as
``cruxible_provider_noop.interface`` does for ``noop.echo``.

Each digest is a **literal**, not a value recomputed at import time: an identity
that recomputes itself is an identity that can drift silently. A test asserts
each literal still matches the preimage beside it.

The preimages are also where the plane's two standing product laws are written
down in a form a reviewer can check:

* **no generic confidence score.** Not one output schema below carries a
  ``confidence``, a ``certainty``, or any other single number claiming to
  summarise how much to believe the answer. What they carry instead are named,
  defined quantities: prediction intervals at declared levels, a p-value beside
  the test that produced it, a Fellegi-Sunter match weight, a Brier score, a
  modified z-score against a stated scale estimate. Each is interpretable only
  against its own definition, which is the property a unified confidence
  destroys.
* **grade is the CaptureContract's, not the provider's.** No output schema has a
  ``grade`` field. A forecast, an anomaly flag, and a score are derived readings;
  the declared capture-contract families are named for it, and a test asserts
  every one of them is a ``derived`` family.
"""

from __future__ import annotations

from typing import Any

from cruxible_provider_runtime.buckets import BucketVocabulary
from cruxible_provider_runtime.canonical import domain_digest
from cruxible_provider_runtime.registry import InterfaceRegistration

from .classifiers import CLASSIFIERS

__all__ = [
    "INTERFACE_DIGESTS",
    "INTERFACE_IDS",
    "INTERFACE_PREIMAGES",
    "STUB_INTERFACE_DOMAIN_TAG",
    "recompute_interface_digest",
    "registration",
]

STUB_INTERFACE_DOMAIN_TAG = "cruxible.interface.stub.v1"

_SERIES_INPUT = {
    "series": {
        "type": "array",
        "required": True,
        "items": {"timestamp": "iso8601|epoch_seconds", "value": "number|null"},
    },
    "season_length": {"type": "integer|array<integer>", "required": False},
    "value_kind": {"type": "string", "required": False, "enum": ["categorical_encoded"]},
    "value_bounds": {"type": "array<number>[2]", "required": False},
}

INTERFACE_PREIMAGES: dict[str, dict[str, Any]] = {
    "calc.reduce": {
        "interface_id": "calc.reduce",
        "version": 1,
        "input": {
            "rows": {"type": "array<object>", "required": True},
            "group_by": {"type": "array<string>", "required": False},
            "aggregations": {
                "type": "array<object>",
                "required": False,
                "items": {"column": "string", "function": "string", "alias": "string"},
            },
            "window": {
                "type": "object",
                "required": False,
                "items": {
                    "column": "string",
                    "function": "string",
                    "size": "integer",
                    "order_by": "string",
                    "alias": "string",
                },
            },
        },
        "output": {
            "reduction_kind": {"type": "string"},
            "input_row_count": {"type": "integer"},
            "columns": {"type": "array<string>"},
            "rows": {"type": "array<object>"},
            "engine": {"type": "object"},
        },
        "refusals": [
            "non_finite_result",
            "unknown_column",
            "unsupported_aggregation",
            "invalid_parameter",
        ],
    },
    "ts.anomaly": {
        "interface_id": "ts.anomaly",
        "version": 1,
        "input": {
            **_SERIES_INPUT,
            "method": {"type": "string", "required": True, "enum": ["stl_mad", "changepoint"]},
            "threshold_modified_z": {"type": "number", "required": False},
            "changepoint_count": {"type": "integer", "required": False},
        },
        "output": {
            "method": {"type": "string"},
            "scale_estimate": {"type": "object"},
            "threshold": {"type": "object"},
            "points": {"type": "array<object>"},
            "flagged_indices": {"type": "array<integer>"},
            "changepoints": {"type": "array<integer>"},
            "segments": {"type": "array<object>"},
            "engine": {"type": "object"},
        },
        "refusals": [
            "insufficient_series_length",
            "non_finite_input",
            "non_finite_result",
            "degenerate_scale",
            "unknown_method",
            "invalid_parameter",
        ],
    },
    "ts.forecast": {
        "interface_id": "ts.forecast",
        "version": 1,
        "input": {
            **_SERIES_INPUT,
            "horizon": {"type": "integer", "required": True},
            "model": {"type": "string", "required": True, "enum": ["auto_arima", "auto_ets"]},
            "interval_levels": {"type": "array<number>", "required": False},
            "covariates": {"type": "object", "required": False},
        },
        "output": {
            "model": {"type": "string"},
            "model_selected": {"type": "string"},
            "horizon": {"type": "integer"},
            "season_length": {"type": "integer"},
            "point_forecast": {"type": "array<number>"},
            "prediction_intervals": {
                "type": "array<object>",
                "items": {
                    "level": "number",
                    "lower": "array<number>",
                    "upper": "array<number>",
                },
            },
            "engine": {"type": "object"},
        },
        "refusals": [
            "insufficient_series_length",
            "non_finite_input",
            "non_finite_result",
            "unknown_method",
            "invalid_parameter",
        ],
    },
    "stat.test": {
        "interface_id": "stat.test",
        "version": 1,
        "input": {
            "test": {"type": "string", "required": True},
            "test_family": {"type": "string", "required": True},
            "design": {"type": "string", "required": True},
            "alpha": {"type": "number", "required": True},
            "alternative": {"type": "string", "required": False},
            "declared_assumptions": {"type": "array<string>", "required": True},
            "samples": {"type": "object", "required": True},
        },
        "output": {
            "test": {"type": "string"},
            "test_family": {"type": "string"},
            "alternative": {"type": "string"},
            "alpha": {"type": "number"},
            "statistic": {"type": "number"},
            "statistic_kind": {"type": "string"},
            "p_value": {"type": "number"},
            "degrees_of_freedom": {"type": "number|null"},
            "reject_null": {"type": "boolean"},
            "effect": {"type": "object|null"},
            "assumptions": {
                "type": "array<object>",
                "items": {
                    "name": "string",
                    "declared": "boolean",
                    "checked": "boolean",
                    "check": "string|null",
                    "p_value": "number|null",
                    "holds": "boolean|null",
                },
            },
            "assumptions_satisfied": {"type": "boolean|null"},
            "engine": {"type": "object"},
        },
        "refusals": [
            "unknown_test_name",
            "declared_family_mismatch",
            "invalid_parameter",
            "mismatched_lengths",
            "non_finite_input",
            "non_finite_result",
        ],
    },
    "score.rank": {
        "interface_id": "score.rank",
        "version": 1,
        "input": {
            "mode": {"type": "string", "required": True, "enum": ["weighted", "pinned_model"]},
            "objective": {"type": "string", "required": True},
            "items": {
                "type": "array<object>",
                "required": True,
                "items": {"id": "string", "signals": "object", "label": "number|null"},
            },
            "weights": {"type": "object", "required": False},
            "model_ref": {
                "type": "object",
                "required": False,
                "items": {
                    "kind": "string",
                    "path": "string",
                    "sha256": "string",
                    "feature_order": "array<string>",
                    "score_kind": "string",
                },
            },
        },
        "output": {
            "mode": {"type": "string"},
            "objective": {"type": "string"},
            "score_kind": {"type": "string"},
            "tie_break": {"type": "string"},
            "signals_used": {"type": "array<string>"},
            "ranking": {
                "type": "array<object>",
                "items": {
                    "rank": "integer",
                    "id": "string",
                    "score": "number",
                    "tied_with": "array<string>",
                },
            },
            "engine": {"type": "object"},
        },
        "refusals": [
            "unknown_method",
            "malformed_model_ref",
            "unknown_column",
            "invalid_parameter",
            "non_finite_input",
            "non_finite_result",
            "artifact_hash_mismatch",
        ],
    },
    "match.record": {
        "interface_id": "match.record",
        "version": 1,
        "input": {
            "records": {"type": "array<object>", "required": True},
            "unique_id_field": {"type": "string", "required": False},
            "comparisons": {
                "type": "array<object>",
                "required": True,
                "items": {
                    "field": "string",
                    "m_probability": "number",
                    "u_probability": "number",
                },
            },
            "blocking_fields": {"type": "array<string>", "required": False},
            "prior_match_probability": {"type": "number", "required": True},
            "threshold_match_probability": {"type": "number", "required": False},
            "known_matches": {"type": "array<object>", "required": False},
        },
        "output": {
            "link_type": {"type": "string"},
            "prior_match_probability": {"type": "number"},
            "threshold_match_probability": {"type": "number"},
            "blocking_fields": {"type": "array<string>"},
            "candidate_pair_count": {"type": "integer"},
            "pairs": {
                "type": "array<object>",
                "items": {
                    "left_id": "string",
                    "right_id": "string",
                    "match_weight": "number",
                    "match_probability": "number",
                    "comparison_vector": "object",
                },
            },
            "review_required": {"type": "boolean"},
            "engine": {"type": "object"},
        },
        "refusals": [
            "undeclared_match_parameters",
            "unknown_column",
            "invalid_parameter",
            "non_finite_result",
        ],
    },
    "calc.calibrate": {
        "interface_id": "calc.calibrate",
        "version": 1,
        "input": {
            "predictions": {
                "type": "array<object>",
                "required": True,
                "items": {
                    "prediction": "number|object",
                    "outcome": "number",
                    "made_at": "number",
                    "settled_at": "number",
                },
            },
            "bin_edges": {"type": "array<number>", "required": False},
            "bin_count": {"type": "integer", "required": False},
        },
        "output": {
            "outcome_type": {"type": "string"},
            "sample_size": {"type": "integer"},
            "base_rate": {"type": "number"},
            "brier_score": {"type": "number"},
            "brier_decomposition": {
                "type": "object",
                "items": {
                    "reliability": "number",
                    "resolution": "number",
                    "uncertainty": "number",
                },
            },
            "expected_calibration_error": {"type": "number"},
            "bin_edges": {"type": "array<number>"},
            "reliability_bins": {
                "type": "array<object>",
                "items": {
                    "lower": "number",
                    "upper": "number",
                    "count": "integer",
                    "mean_prediction": "number|null",
                    "observed_frequency": "number|null",
                },
            },
            "engine": {"type": "object"},
        },
        "refusals": [
            "mismatched_lengths",
            "non_finite_input",
            "invalid_parameter",
            "non_finite_result",
        ],
    },
}

INTERFACE_IDS: tuple[str, ...] = tuple(sorted(INTERFACE_PREIMAGES))

INTERFACE_DIGESTS: dict[str, str] = {
    "calc.calibrate": "sha256:68ed76a4d56a0ccaf0dd80380d20b84072bdd3bee2424ede0ec519cfb0d559a8",
    "calc.reduce": "sha256:148c0044ea289d8014b2223f5bd269248972e6213bca046ed476f426642e586e",
    "match.record": "sha256:a0f902bdb0bf13ede311b32808c36124e85d0fb5a639398c512135ffec66fd4e",
    "score.rank": "sha256:96d0ff67eac67537d304273de990221b1342c2e637e393e472dc7abf644b2c80",
    "stat.test": "sha256:ed085bcc3fbda9dea5005432f2f0be723f165b2804df71e3e20bb69c43cec937",
    "ts.anomaly": "sha256:c9dcccbecdfb991df310853aa4b1193032f58480ddfe26d9f065ddc7557736e0",
    "ts.forecast": "sha256:a350e46acd787ad8eebfcc6242f8fd99cd0a1a47bf4e9d5cf6930b0a8cdd4baa",
}


def recompute_interface_digest(interface_id: str) -> str:
    """Recompute one stub digest from its preimage (used by the drift test)."""

    return domain_digest(STUB_INTERFACE_DOMAIN_TAG, INTERFACE_PREIMAGES[interface_id])


def registration(
    interface_id: str, vocabulary: BucketVocabulary, description: str = ""
) -> InterfaceRegistration:
    """Build the registration a stub registry is seeded with.

    The vocabulary is passed in rather than shipped here: it is core's data,
    committed once under ``vocab/interfaces/``, and a second copy inside a
    provider package would be a second source of truth for something the
    provider does not own.
    """

    return InterfaceRegistration(
        interface_id=interface_id,
        interface_digest=INTERFACE_DIGESTS[interface_id],
        bucket_vocabulary=vocabulary,
        classifier=CLASSIFIERS[interface_id],
        description=description or vocabulary.description.strip(),
    )
