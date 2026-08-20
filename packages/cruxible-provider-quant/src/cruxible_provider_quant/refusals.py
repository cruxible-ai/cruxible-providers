"""The quantitative plane's closed set of decline reasons.

Why this exists at all
----------------------

Every refusal a provider emits must be typed and drawn from a closed set. The
runtime's :class:`~cruxible_provider_runtime.errors.RefusalCode` is that set for
everything the *executor* decides — an unclaimed bucket, a budget breach, an
undeclared endpoint. What it does not yet carry is the handful of conditions a
quantitative implementation is the only thing positioned to detect: a series too
short for the seasonal model the caller declared, a test name that is not a
test, a pinned model reference that does not verify.

Those belong in the runtime taxonomy. This batch may not put them there: the
runtime package is owned by a concurrent batch, and adding a code to a shared
taxonomy behind another author's back is exactly the drift the standing
"every code has a raise site and a named test" rule exists to stop. So this
package declares its own closed set and carries it inside the detail payload of
the one runtime code that genuinely fits — ``provider_declined``, whose meaning
is "the provider deliberately declined under a named rule".

The shape is deliberately liftable: each member here maps one-to-one onto a
future ``RefusalCode``, and the day the runtime carries them, the change is a
rename at the raise sites and a deletion of this module.

The discipline is the same as the taxonomy's
----------------------------------------------

``tests/test_refusals.py`` asserts that every member of :class:`DeclineReason`
is exercised by a named test. An enumerated reason nobody raises is a reason
that has drifted away from the code.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from cruxible_provider_runtime.errors import RefusalCode
from cruxible_provider_runtime.provider_api import ProviderResult

__all__ = ["DECLINE_DETAIL_KEY", "DeclineReason", "decline"]

DECLINE_DETAIL_KEY = "reason"
"""The detail key a caller reads to recover the specific reason."""


class DeclineReason(StrEnum):
    """Every rule under which a quantitative implementation declines."""

    # --- series and sample shape -------------------------------------------
    INSUFFICIENT_SERIES_LENGTH = "insufficient_series_length"
    """Fewer observations than the declared seasonal model needs to fit."""

    NON_FINITE_INPUT = "non_finite_input"
    """A NaN or an infinity where a finite number was required."""

    DEGENERATE_SCALE = "degenerate_scale"
    """A scale estimate of zero: the series carries no dispersion to score against."""

    MISMATCHED_LENGTHS = "mismatched_lengths"
    """Two inputs that must be aligned element-wise are not the same length."""

    # --- declared method ----------------------------------------------------
    UNKNOWN_METHOD = "unknown_method"
    """A method name outside the implementation's closed set."""

    UNKNOWN_TEST_NAME = "unknown_test_name"
    """A statistical test name outside the closed set. Never auto-substituted."""

    DECLARED_FAMILY_MISMATCH = "declared_family_mismatch"
    """The declared test family is not the family the named test belongs to."""

    UNSUPPORTED_AGGREGATION = "unsupported_aggregation"
    """An aggregation function outside the closed set."""

    # --- referenced data ----------------------------------------------------
    UNKNOWN_COLUMN = "unknown_column"
    """A column named in the request that the relation does not carry."""

    MALFORMED_MODEL_REF = "malformed_model_ref"
    """A pinned-model reference missing its pin, or whose bytes do not match it."""

    UNDECLARED_MATCH_PARAMETERS = "undeclared_match_parameters"
    """Linkage was asked for without the m/u probabilities it must be told."""

    INVALID_PARAMETER = "invalid_parameter"
    """A parameter present, well-typed, and outside its admissible range."""


def decline(reason: DeclineReason, message: str, **detail: Any) -> ProviderResult:
    """Build the typed refusal for ``reason``.

    The runtime code is ``provider_declined`` — the provider is declining under
    a rule it names, which is precisely that code's meaning — and the specific
    rule travels in ``detail['reason']`` where a receipt and a track record can
    both see it.
    """

    return ProviderResult.refused(
        RefusalCode.PROVIDER_DECLINED,
        message,
        **{DECLINE_DETAIL_KEY: reason.value, **detail},
    )
