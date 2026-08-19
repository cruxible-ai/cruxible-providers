"""The provider-facing surface."""

from __future__ import annotations

import dataclasses

import pytest
from cruxible_provider_runtime.egress import EgressRecorder
from cruxible_provider_runtime.errors import RefusalCode
from cruxible_provider_runtime.protocol import Budgets
from cruxible_provider_runtime.provider_api import ProviderResult, ProviderRunContext

CREDENTIAL = "dummy-credential-c0ffee"


def _context() -> ProviderRunContext:
    return ProviderRunContext(
        run_id="run-1",
        interface_id="test.slot",
        interface_digest="sha256:" + "1" * 64,
        implementation_digest="sha256:" + "2" * 64,
        input_bucket="size=small",
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

    refused = ProviderResult.refused(RefusalCode.PROVIDER_DECLINED, "no")
    assert refused.status == "refused"
    assert refused.output is None and refused.error is None

    failed = ProviderResult.failed("Kind", "boom")
    assert failed.status == "error"
    assert failed.output is None and failed.refusal is None


def test_result_status_is_a_closed_literal() -> None:
    from typing import get_args

    from cruxible_provider_runtime.provider_api import ProviderStatus

    assert set(get_args(ProviderStatus)) == {"ok", "refused", "error"}


def test_the_run_context_is_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        _context().run_id = "other"  # type: ignore[misc]
