"""Out-of-process budget enforcement.

The distinction under test is the contract's, not an implementation detail: a
budget breach is a typed *refusal*, so it never reaches a provider's track
record as a failed answer.
"""

from __future__ import annotations

import sys

import pytest
from cruxible_provider_runtime.budget import enforce_cost_budget, minimal_env, run_with_budget
from cruxible_provider_runtime.errors import RefusalCode, RefusalError
from cruxible_provider_runtime.protocol import Budgets

FAST = Budgets(wall_clock_seconds=10.0, output_bytes=1_000_000)


def test_ordinary_run_returns_its_output() -> None:
    outcome = run_with_budget(
        [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read().upper())"],
        stdin_bytes=b"hello",
        budgets=FAST,
    )
    assert outcome.returncode == 0
    assert outcome.stdout == b"HELLO"


def test_wall_clock_breach_is_a_refusal_not_an_error() -> None:
    with pytest.raises(RefusalError) as exc:
        run_with_budget(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdin_bytes=b"",
            budgets=Budgets(wall_clock_seconds=0.5, output_bytes=1_000_000),
        )
    assert exc.value.code is RefusalCode.BUDGET_WALL_CLOCK
    assert exc.value.refusal.detail["wall_clock_budget"] == 0.5


def test_output_size_breach_is_a_refusal() -> None:
    with pytest.raises(RefusalError) as exc:
        run_with_budget(
            [
                sys.executable,
                "-c",
                "import sys\nwhile True:\n    sys.stdout.write('x' * 8192)\n    sys.stdout.flush()",
            ],
            stdin_bytes=b"",
            budgets=Budgets(wall_clock_seconds=20.0, output_bytes=32_768),
        )
    assert exc.value.code is RefusalCode.BUDGET_OUTPUT_SIZE


def test_child_environment_is_built_from_scratch() -> None:
    """The ambient environment is never inherited: a credential cannot ride in."""

    outcome = run_with_budget(
        [sys.executable, "-c", "import os, json; print(json.dumps(sorted(os.environ)))"],
        stdin_bytes=b"",
        budgets=FAST,
    )
    names = set(eval(outcome.stdout.decode()))
    assert names <= set(minimal_env()) | {"PYTHONHASHSEED", "__CF_USER_TEXT_ENCODING"}


def test_stderr_is_captured_separately() -> None:
    outcome = run_with_budget(
        [sys.executable, "-c", "import sys; sys.stderr.write('warned')"],
        stdin_bytes=b"",
        budgets=FAST,
    )
    assert outcome.stderr == b"warned"
    assert outcome.stdout == b""


def test_nonzero_exit_is_reported_not_raised() -> None:
    outcome = run_with_budget(
        [sys.executable, "-c", "raise SystemExit(3)"], stdin_bytes=b"", budgets=FAST
    )
    assert outcome.returncode == 3


def test_cost_budget_seam_raises_a_typed_refusal() -> None:
    """RP-0 meters nothing; the substrate raises through this seam."""

    budgets = Budgets(wall_clock_seconds=10.0, output_bytes=1024, cost_units=5.0)
    with pytest.raises(RefusalError) as exc:
        enforce_cost_budget(budgets, consumed_cost_units=5.5, meter="cloud-metering")
    assert exc.value.code is RefusalCode.BUDGET_COST
    assert exc.value.refusal.detail["cost_budget"] == 5.0
    assert exc.value.refusal.detail["consumed_cost_units"] == 5.5
    assert exc.value.refusal.detail["meter"] == "cloud-metering"


def test_spending_within_the_cost_budget_passes() -> None:
    budgets = Budgets(wall_clock_seconds=10.0, output_bytes=1024, cost_units=5.0)
    enforce_cost_budget(budgets, consumed_cost_units=5.0)


def test_an_absent_cost_budget_is_not_a_zero_budget() -> None:
    enforce_cost_budget(FAST, consumed_cost_units=1_000_000.0)
