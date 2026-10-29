"""The provider-facing surface."""

from __future__ import annotations

import dataclasses

import pytest
from cruxible_provider_runtime.egress import EgressRecorder
from cruxible_provider_runtime.errors import Declined, RefusalCode, decline
from cruxible_provider_runtime.protocol import Budgets
from cruxible_provider_runtime.provider_api import ProviderResult, ProviderRunContext

CREDENTIAL = "dummy-credential-c0ffee"


def _context() -> ProviderRunContext:
    return ProviderRunContext(
        run_id="run-1",
        operation="test.op",
        operation_signature_digest="sha256:" + "1" * 64,
        build_digest="sha256:" + "3" * 64,
        implementation_digest="sha256:" + "2" * 64,
        input={"text": "x"},
        coordinates={},
        budgets=Budgets(wall_clock_seconds=5.0, output_bytes=1024),
        declared_endpoints=(),
        capture_contract=None,
        secrets={"provider.api_key": CREDENTIAL},
        egress=EgressRecorder(),
    )


def test_the_run_context_repr_does_not_print_credentials() -> None:
    """A repr is what lands in a traceback, a log line, and a debugger transcript."""

    context = _context()
    assert CREDENTIAL not in repr(context)
    assert CREDENTIAL not in f"{context}"
    assert CREDENTIAL not in str(dataclasses.replace(context, run_id="run-2"))


def test_the_credentials_are_still_readable_by_the_provider() -> None:
    """Hiding them from repr must not hide them from the provider."""

    assert _context().secrets["provider.api_key"] == CREDENTIAL


def test_a_result_carries_exactly_one_outcome() -> None:
    ok = ProviderResult.ok({"a": 1}, metrics={"n": 1.0})
    assert ok.status == "ok"
    assert ok.refusal is None and ok.error is None

    refused = ProviderResult.refused("provider_declined", "no")
    assert refused.status == "refused"
    assert refused.output is None and refused.error is None

    failed = ProviderResult.failed("Kind", "boom")
    assert failed.status == "error"
    assert failed.output is None and failed.refusal is None


@pytest.mark.parametrize("code", sorted(RefusalCode))
def test_an_operation_refusal_cannot_use_a_runtime_code(code: RefusalCode) -> None:
    """The runtime's codes are reserved; an operation refuses under its own."""

    with pytest.raises(ValueError, match="reserved by the runtime"):
        ProviderResult.refused(code.value, "no")
    with pytest.raises(ValueError, match="reserved by the runtime"):
        Declined(code.value, "no")


@pytest.mark.parametrize("code", ["Not A Code", "", "9lives", "x" * 65])
def test_an_operation_refusal_code_is_a_canonical_identifier(code: str) -> None:
    with pytest.raises(ValueError, match="canonical identifier"):
        ProviderResult.refused(code, "no")


def test_a_decline_raised_from_depth_carries_the_operation_code() -> None:
    error = decline("too_long", "the text is too long", limit=10)
    assert isinstance(error, Declined)
    assert error.refusal.code == "too_long"
    assert error.refusal.detail == {"limit": 10}
    assert not error.refusal.is_runtime


def test_result_status_is_a_closed_literal() -> None:
    from typing import get_args

    from cruxible_provider_runtime.provider_api import ProviderStatus

    assert set(get_args(ProviderStatus)) == {"ok", "refused", "error"}


def test_the_run_context_is_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        _context().run_id = "other"  # type: ignore[misc]
