"""The subset of the refusal taxonomy this plane declines under.

This module used to declare a second enum. It did so for a stated reason — the
runtime package was owned by a concurrent batch, and adding codes to a shared
taxonomy behind another author's back is exactly the drift the standing
"every code has a raise site and a named test" rule exists to stop — and it said
in as many words that the members were liftable, that each mapped one-to-one
onto a future ``RefusalCode``, and that the day the runtime carried them the
change would be a rename at the raise sites.

The runtime carries them now. So the enum is gone, the reason a caller reads is
the refusal's own ``code`` rather than a string smuggled through
``detail['reason']``, and this module is down to two things: the closed subset a
quantitative implementation may decline under, and the constructor that builds
one.

**Why a subset and not the whole taxonomy.** Most of ``RefusalCode`` belongs to
the executor — an unclaimed bucket, a budget breach, a cache-integrity failure —
and an implementation raising one of those would be claiming an authority it does
not have. :data:`QUANT_DECLINES` names the ones it does have, and
``tests/test_refusals.py`` asserts that every one of them has a named test: the
same discipline as before, over the same set, now anchored to the taxonomy
instead of running alongside it.

**This never extended to integrity.** A model file whose bytes do not hash to
their pin raises ``RefusalCode.ARTIFACT_HASH_MISMATCH``, and always did, because
routing it through here would convert an integrity signal into a capability
limit. The subset below deliberately does not carry it.
"""

from __future__ import annotations

from typing import Any

from cruxible_provider_runtime.errors import RefusalCode
from cruxible_provider_runtime.provider_api import ProviderResult

__all__ = ["QUANT_DECLINES", "decline"]

QUANT_DECLINES: tuple[RefusalCode, ...] = (
    RefusalCode.INSUFFICIENT_SERIES_LENGTH,
    RefusalCode.NON_FINITE_INPUT,
    RefusalCode.NON_FINITE_RESULT,
    RefusalCode.DEGENERATE_SCALE,
    RefusalCode.MISMATCHED_LENGTHS,
    RefusalCode.UNKNOWN_METHOD,
    RefusalCode.UNKNOWN_TEST_NAME,
    RefusalCode.DECLARED_FAMILY_MISMATCH,
    RefusalCode.UNSUPPORTED_AGGREGATION,
    RefusalCode.UNKNOWN_COLUMN,
    RefusalCode.MALFORMED_MODEL_REF,
    RefusalCode.UNDECLARED_MATCH_PARAMETERS,
    RefusalCode.INVALID_PARAMETER,
)
"""Every rule a quantitative implementation may decline under."""


def decline(code: RefusalCode, message: str, **detail: Any) -> ProviderResult:
    """Build a typed decline, refusing to build one outside this plane's subset.

    The membership check is not ceremony. The taxonomy is shared, most of it
    belongs to the executor, and a provider reaching for ``cache_integrity`` or
    ``budget_wall_clock`` would be reporting a judgement it is not positioned to
    make — as a refusal that a track record would then count against the wrong
    thing.
    """

    if code not in QUANT_DECLINES:
        raise ValueError(f"{code.value!r} is not a rule this plane declines under")
    return ProviderResult.refused(code, message, **detail)
