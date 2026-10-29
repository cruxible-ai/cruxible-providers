"""The refusals this plane declines under.

In protocol 2.0 there is no shared decline vocabulary: each operation declares
the refusals it may return in ``registration.json``, and those codes are the
operation's own. :data:`QUANT_DECLINES` is the closed set this plane's
operations draw from, and :func:`decline` builds one, refusing a code outside
it. ``tests/test_refusals.py`` asserts that every one of them has a named test,
and ``tests/test_registration_stability.py`` that each operation declares
exactly the ones its implementation can return.

**Why a closed set at all.** The runtime keeps its own codes — a budget breach,
a cache-integrity failure, an artifact whose bytes do not hash to their pin —
and reserves them: an operation cannot declare one, and an implementation that
returned one would be claiming an authority it does not have. Those conditions
are raised as the runtime's ``RefusalError`` instead.

**This never extended to integrity.** A model file whose bytes do not hash to
their pin raises the runtime's ``artifact_hash_mismatch``, and always did,
because routing it through here would convert an integrity signal into a
capability limit. The set below deliberately does not carry it.
"""

from __future__ import annotations

from typing import Any

from cruxible_provider_runtime.provider_api import ProviderResult

__all__ = [
    "DECLARED_FAMILY_MISMATCH",
    "DEGENERATE_SCALE",
    "INSUFFICIENT_SERIES_LENGTH",
    "INVALID_PARAMETER",
    "MALFORMED_MODEL_REF",
    "MISMATCHED_LENGTHS",
    "NON_FINITE_INPUT",
    "NON_FINITE_RESULT",
    "QUANT_DECLINES",
    "UNDECLARED_MATCH_PARAMETERS",
    "UNKNOWN_COLUMN",
    "UNKNOWN_METHOD",
    "UNKNOWN_TEST_NAME",
    "UNSUPPORTED_AGGREGATION",
    "decline",
]

INSUFFICIENT_SERIES_LENGTH = "insufficient_series_length"
NON_FINITE_INPUT = "non_finite_input"
NON_FINITE_RESULT = "non_finite_result"
DEGENERATE_SCALE = "degenerate_scale"
MISMATCHED_LENGTHS = "mismatched_lengths"
UNKNOWN_METHOD = "unknown_method"
UNKNOWN_TEST_NAME = "unknown_test_name"
DECLARED_FAMILY_MISMATCH = "declared_family_mismatch"
UNSUPPORTED_AGGREGATION = "unsupported_aggregation"
UNKNOWN_COLUMN = "unknown_column"
MALFORMED_MODEL_REF = "malformed_model_ref"
UNDECLARED_MATCH_PARAMETERS = "undeclared_match_parameters"
INVALID_PARAMETER = "invalid_parameter"

QUANT_DECLINES: tuple[str, ...] = (
    INSUFFICIENT_SERIES_LENGTH,
    NON_FINITE_INPUT,
    NON_FINITE_RESULT,
    DEGENERATE_SCALE,
    MISMATCHED_LENGTHS,
    UNKNOWN_METHOD,
    UNKNOWN_TEST_NAME,
    DECLARED_FAMILY_MISMATCH,
    UNSUPPORTED_AGGREGATION,
    UNKNOWN_COLUMN,
    MALFORMED_MODEL_REF,
    UNDECLARED_MATCH_PARAMETERS,
    INVALID_PARAMETER,
)
"""Every rule a quantitative operation may decline under."""


def decline(code: str, message: str, **detail: Any) -> ProviderResult:
    """Build a typed decline, refusing to build one outside this plane's set.

    The membership check is not ceremony: a code outside the set is one no
    operation here declares, and the executor would refuse it as undeclared.
    """

    if code not in QUANT_DECLINES:
        raise ValueError(f"{code!r} is not a rule this plane declines under")
    return ProviderResult.refused(code, message, **detail)
