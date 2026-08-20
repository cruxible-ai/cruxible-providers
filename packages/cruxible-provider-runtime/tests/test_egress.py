"""Egress instrumentation: declared versus observed."""

from __future__ import annotations

import socket

import pytest
from cruxible_provider_runtime.egress import (
    DYNAMIC_TARGET_FROM_RUN_INPUT,
    EgressRecorder,
    compare_egress,
    enforce_egress,
    no_network,
    normalize_endpoint,
    partition_declared,
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


def test_a_dynamic_declaration_admits_a_run_determined_target() -> None:
    """EXPERIMENTAL form. An adapter whose target IS the run input.

    ``web.fetch`` cannot enumerate its endpoints at acceptance time without
    deleting the interface. The declaration therefore says the set is dynamic,
    and what governs is the recording.
    """

    comparison = compare_egress([DYNAMIC_TARGET_FROM_RUN_INPUT], ["https://whatever.example/page"])
    assert comparison.conformant
    assert comparison.observed == ("https://whatever.example",)
    assert comparison.dynamic_forms == (DYNAMIC_TARGET_FROM_RUN_INPUT,)


def test_a_dynamic_declaration_says_so_rather_than_looking_like_an_allowlist() -> None:
    """The half that keeps the form honest.

    An empty ``undeclared`` under a dynamic form must not read like an allowlist
    that held, so the form is carried on the comparison and lands in the
    receipt. Without this, a dynamic declaration and a satisfied static one are
    indistinguishable on the record.
    """

    dynamic = compare_egress([DYNAMIC_TARGET_FROM_RUN_INPUT], ["https://a.example"])
    static = compare_egress(["https://a.example"], ["https://a.example"])
    assert dynamic.conformant and static.conformant
    assert dynamic.dynamic_forms and not static.dynamic_forms


def test_a_dynamic_form_mixes_with_concrete_endpoints() -> None:
    endpoints, dynamic = partition_declared(
        ["https://index.example", DYNAMIC_TARGET_FROM_RUN_INPUT]
    )
    assert endpoints == ("https://index.example",)
    assert dynamic == (DYNAMIC_TARGET_FROM_RUN_INPUT,)


def test_enforce_never_refuses_under_a_dynamic_declaration() -> None:
    comparison = enforce_egress(
        [DYNAMIC_TARGET_FROM_RUN_INPUT],
        ["https://one.example", "https://two.example"],
        implementation_digest=DIGEST,
    )
    assert comparison.observed == ("https://one.example", "https://two.example")


def test_no_network_blocks_outbound_sockets() -> None:
    with no_network(), pytest.raises(RefusalError) as exc:
        socket.create_connection(("example.invalid", 80), timeout=1)
    assert exc.value.code is RefusalCode.UNDECLARED_EGRESS


def test_no_network_restores_the_socket_module() -> None:
    original = socket.socket.connect
    with no_network():
        assert socket.socket.connect is not original
    assert socket.socket.connect is original
