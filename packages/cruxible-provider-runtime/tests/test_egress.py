"""Egress instrumentation: declared versus observed."""

from __future__ import annotations

import socket

import pytest
from cruxible_provider_runtime.egress import (
    EgressRecorder,
    compare_egress,
    enforce_egress,
    no_network,
    normalize_endpoint,
)
from cruxible_provider_runtime.errors import RefusalCode, RefusalError

DIGEST = "sha256:" + "0f" * 32


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://api.example/v1/things?q=1", "https://api.example"),
        ("https://API.Example:443/", "https://api.example"),
        ("https://api.example:8443/x", "https://api.example:8443"),
        ("http://api.example/", "http://api.example"),
        ("api.example", "https://api.example"),
    ],
)
def test_normalization_keeps_who_not_what(raw: str, expected: str) -> None:
    assert normalize_endpoint(raw) == expected


def test_recorder_deduplicates() -> None:
    recorder = EgressRecorder()
    recorder.record("https://api.example/a")
    recorder.record("https://api.example/b")
    recorder.record("https://other.example/")
    assert recorder.observed() == ["https://api.example", "https://other.example"]


def test_conformant_when_observed_is_a_subset() -> None:
    comparison = compare_egress(["https://a.example", "https://b.example"], ["https://a.example"])
    assert comparison.conformant
    assert comparison.unused == ("https://b.example",)


def test_undeclared_endpoint_refuses_and_names_the_implementation() -> None:
    with pytest.raises(RefusalError) as exc:
        enforce_egress(
            ["https://a.example"],
            ["https://a.example", "https://sneaky.example"],
            implementation_digest=DIGEST,
        )
    assert exc.value.code is RefusalCode.UNDECLARED_EGRESS
    assert exc.value.refusal.detail["implementation_digest"] == DIGEST
    assert exc.value.refusal.detail["undeclared"] == ["https://sneaky.example"]


def test_zero_declared_endpoints_means_zero_observed() -> None:
    assert enforce_egress([], [], implementation_digest=DIGEST).conformant
    with pytest.raises(RefusalError):
        enforce_egress([], ["https://anything.example"], implementation_digest=DIGEST)


def test_no_network_blocks_outbound_sockets() -> None:
    with no_network(), pytest.raises(RefusalError) as exc:
        socket.create_connection(("example.invalid", 80), timeout=1)
    assert exc.value.code is RefusalCode.UNDECLARED_EGRESS


def test_no_network_restores_the_socket_module() -> None:
    original = socket.socket.connect
    with no_network():
        assert socket.socket.connect is not original
    assert socket.socket.connect is original
