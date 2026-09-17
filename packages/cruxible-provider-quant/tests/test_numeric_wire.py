"""The native engine boundary does not leak floats into governed values."""

from __future__ import annotations

import math

import pytest
from cruxible_provider_quant.numeric import decode_numbers, encode_numbers


@pytest.mark.parametrize("value", [0.0, -0.0, 0.1, -1.25, 1e-40, 1e100, math.pi])
def test_finite_float_round_trip(value: float) -> None:
    wire = encode_numbers({"nested": [value]})
    assert isinstance(wire["nested"][0]["$decimal"], str)
    assert decode_numbers(wire)["nested"][0] == value


@pytest.mark.parametrize(
    "value", ["1.0", "-0", "NaN", "Infinity", "1e2", "not-a-number", "0." + "0" * 400 + "1"]
)
def test_ambiguous_or_unrepresentable_decimals_refuse(value: str) -> None:
    with pytest.raises(ValueError):
        decode_numbers({"$decimal": value})


def test_extra_decimal_keys_and_nonfinite_outputs_refuse() -> None:
    with pytest.raises(ValueError):
        decode_numbers({"$decimal": "1", "other": 2})
    with pytest.raises(ValueError):
        encode_numbers(float("nan"))
