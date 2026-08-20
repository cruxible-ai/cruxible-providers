"""The last gate between a computed number and a successful answer.

Every implementation in this package validates its *inputs*. That is not the
same as validating its results, and the gap between the two is where the useful
failure lives: a statistical engine handed two perfectly finite, perfectly
well-formed constant samples returns a NaN statistic and a NaN p-value, because
the test it was asked to run has no answer for that sample. Reported as
``status=ok`` with ``reject_null=False``, that is not a cautious result — it is a
statistical conclusion nobody drew, sitting in the evidence path with a
successful status on it.

So every ``ok`` this package emits goes through :func:`ok_if_finite`. A
non-finite number anywhere inside the output, the metrics, or the events turns
the answer into a typed decline naming where it was found. Two properties matter
about that:

* it declines rather than errors. A degenerate sample is not a failure of the
  implementation, it is a question the declared test cannot answer, and the
  distinction is what keeps a track record readable;
* it is checked at the boundary rather than at each call site. There are a dozen
  places an engine can hand back a NaN and one place they all leave from, and a
  rule enforced at the exit is a rule that cannot be forgotten in a new one.

The runtime enforces the same floor over every plane's envelope. This layer
exists because a generic ``non_finite_output`` refusal from the transport says
far less than a decline that names the quantity — and because a plane that
relies on someone else's backstop is a plane that reports a crash where it should
have reported a limit.
"""

from __future__ import annotations

import math
from collections.abc import Iterator, Mapping
from typing import Any

from cruxible_provider_runtime.errors import RefusalCode
from cruxible_provider_runtime.provider_api import ProviderResult

from .refusals import decline

__all__ = ["non_finite_paths", "ok_if_finite"]

_MAX_REPORTED_PATHS = 10


def non_finite_paths(value: Any, path: str = "") -> list[str]:
    """Every path inside ``value`` holding a NaN or an infinity, sorted."""

    return sorted(set(_walk(value, path)))


def _walk(value: Any, path: str) -> Iterator[str]:
    if isinstance(value, bool):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            yield path or "<root>"
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield from _walk(item, f"{path}.{key}" if path else str(key))
        return
    if isinstance(value, list | tuple):
        for index, item in enumerate(value):
            yield from _walk(item, f"{path}[{index}]")


def ok_if_finite(
    output: dict[str, Any],
    *,
    metrics: dict[str, float] | None = None,
    events: list[dict[str, Any]] | None = None,
) -> ProviderResult:
    """Return the ``ok`` result, or decline because a number is not one."""

    found = non_finite_paths(output)
    found += [f"metrics.{name}" for name in non_finite_paths(metrics or {})]
    found += [f"events.{name}" for name in non_finite_paths(events or [])]
    if found:
        return decline(
            RefusalCode.NON_FINITE_RESULT,
            "the computation produced a value that is not a number; the inputs were "
            "well-formed but the declared method has no answer for them",
            paths=sorted(found)[:_MAX_REPORTED_PATHS],
            count=len(found),
        )
    return ProviderResult.ok(output, metrics=metrics, events=events)
