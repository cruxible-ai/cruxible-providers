"""Shared time-series parsing, used by the implementations and the classifiers.

A series arrives as a list of ``{"timestamp": ..., "value": ...}`` records. The
timestamps are not decoration: the classifier derives the ``frequency`` bucket
from the spacing between them, and a bucket derived from the actual input is the
only kind the contract allows. A payload that carried bare values would leave
frequency unmeasurable, and an implementation that then declared a frequency
would be claiming a bucket rather than being measured into one.

Timestamps are accepted as ISO-8601 strings or as numeric epoch seconds, and are
normalised to float seconds. Naive ISO timestamps are read as UTC — stated
rather than guessed, because the alternative is a series whose spacing depends
on the machine's zone.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from typing import Any

__all__ = ["Series", "parse_series", "spacing_seconds"]


@dataclass(frozen=True)
class Series:
    """A parsed series: aligned timestamps and values, plus what was missing."""

    timestamps: tuple[float, ...]
    values: tuple[float, ...]
    missing_indices: tuple[int, ...]
    """Positions whose value was null in the payload, dropped from ``values``."""

    @property
    def length(self) -> int:
        return len(self.values)


def _timestamp_seconds(raw: Any) -> float | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int | float):
        return float(raw)
    if isinstance(raw, str):
        try:
            moment = datetime.fromisoformat(raw)
        except ValueError:
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        return moment.timestamp()
    return None


def parse_series(payload: Mapping[str, Any]) -> Series | None:
    """Parse ``payload['series']``, or return ``None`` if it is not a series.

    ``None`` is the classifier's "I cannot place this input", which the registry
    turns into ``unclassified_input`` rather than a guess. The implementations
    call this too, so a malformed series can never reach an engine.
    """

    raw = payload.get("series")
    if not isinstance(raw, Sequence) or isinstance(raw, str | bytes) or not raw:
        return None
    timestamps: list[float] = []
    values: list[float] = []
    missing: list[int] = []
    for index, record in enumerate(raw):
        if not isinstance(record, Mapping):
            return None
        moment = _timestamp_seconds(record.get("timestamp"))
        if moment is None:
            return None
        timestamps.append(moment)
        value = record.get("value")
        if value is None:
            missing.append(index)
            continue
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        values.append(float(value))
    if len(timestamps) != len(set(timestamps)) or list(timestamps) != sorted(timestamps):
        # A series whose timestamps repeat or run backwards is not a series, and
        # silently sorting one would change the thing being modelled.
        return None
    return Series(
        timestamps=tuple(timestamps),
        values=tuple(values),
        missing_indices=tuple(missing),
    )


def spacing_seconds(timestamps: Sequence[float]) -> tuple[float, float] | None:
    """Mean spacing and its coefficient of variation, or ``None`` if undefined."""

    if len(timestamps) < 2:
        return None
    gaps = [later - earlier for earlier, later in pairwise(timestamps)]
    mean = sum(gaps) / len(gaps)
    if mean <= 0:
        return None
    variance = sum((gap - mean) ** 2 for gap in gaps) / len(gaps)
    return mean, math.sqrt(variance) / mean
