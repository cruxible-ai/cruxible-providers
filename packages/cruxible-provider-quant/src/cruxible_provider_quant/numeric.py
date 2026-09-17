"""Canonical decimal transport around native numerical engines.

Core's governed values contain no binary floats. Engines still compute with
native numbers; the public output spells every finite float as a canonical
$decimal object. This conversion changes representation, not model semantics.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from functools import wraps
from typing import Any, TypeVar

from cruxible_provider_runtime.errors import RefusalCode
from cruxible_provider_runtime.provider_api import ProviderResult, ProviderRunContext

SelfT = TypeVar("SelfT")


def _spelling(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if value == 0 else text


def encode_numbers(value: Any) -> Any:
    """Encode engine floats losslessly for their shortest round-trip spelling."""
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("cannot encode a non-finite result")
        return {"$decimal": _spelling(Decimal(str(value)))}
    if isinstance(value, Mapping):
        return {key: encode_numbers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [encode_numbers(item) for item in value]
    return value


def decode_numbers(value: Any) -> Any:
    """Decode explicit decimals at the numerical-engine boundary only."""
    if isinstance(value, Mapping):
        if "$decimal" in value:
            text = value["$decimal"]
            if set(value) != {"$decimal"} or not isinstance(text, str) or len(text) > 768:
                raise ValueError("invalid canonical decimal")
            try:
                decimal = Decimal(text)
            except InvalidOperation as exc:
                raise ValueError("invalid canonical decimal") from exc
            if not decimal.is_finite() or "e" in text.lower() or _spelling(decimal) != text:
                raise ValueError("invalid canonical decimal")
            number = float(decimal)
            if not math.isfinite(number) or (number == 0 and decimal != 0):
                raise ValueError("decimal is outside the numerical engine range")
            return number
        return {key: decode_numbers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [decode_numbers(item) for item in value]
    return value


def canonical_call(
    method: Callable[[SelfT, ProviderRunContext], ProviderResult],
) -> Callable[[SelfT, ProviderRunContext], ProviderResult]:
    @wraps(method)
    def invoke(self: SelfT, context: ProviderRunContext) -> ProviderResult:
        try:
            payload = decode_numbers(context.input)
        except ValueError as exc:
            return ProviderResult.refused(RefusalCode.INVALID_PARAMETER, str(exc))
        result = method(self, replace(context, input=payload))
        if result.output is not None:
            try:
                return replace(result, output=encode_numbers(result.output))
            except ValueError as exc:
                return ProviderResult.refused(RefusalCode.NON_FINITE_RESULT, str(exc))
        return result

    return invoke


def numeric_classifier(
    classifier: Callable[[Mapping[str, Any]], Mapping[str, str] | None],
) -> Callable[[Mapping[str, Any]], Mapping[str, str] | None]:
    @wraps(classifier)
    def classify(payload: Mapping[str, Any]) -> Mapping[str, str] | None:
        try:
            return classifier(decode_numbers(payload))
        except ValueError:
            return None

    return classify
