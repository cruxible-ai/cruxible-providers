"""Same input, same answer — the property identity depends on.

A track record pinned to an implementation digest is only meaningful if the
implementation behind that digest returns the same thing twice. Every fixture is
run twice in one process and the two outputs are compared **byte for byte** after
canonical serialisation, which is a stricter check than comparing floats with a
tolerance and is the right one here: within a machine there is nothing legitimate
that could move.

Cross-machine reproducibility is a weaker claim and the suite makes it in the
right place — the tolerances in ``test_implementations.py``, documented in the
README. This file is about the stronger one.

Two things this file also has to prove, because both are ways determinism gets
lost without anyone noticing:

* no implementation reads a clock, a process id, or an environment variable into
  its output. A run whose output carries a timestamp would compare equal to
  itself here only by accident of speed;
* no implementation seeds a random generator, because a seeded generator is one
  library upgrade away from a different sequence. Nothing in this package draws
  a random number at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from cruxible_provider_runtime.canonical import canonical_json

from .conftest import run_in_process
from .fixtures import FIXTURES

IDS = [fixture.fixture_id for fixture in FIXTURES]
SOURCE_DIR = Path(__file__).resolve().parent.parent / "src" / "cruxible_provider_quant"


@pytest.mark.parametrize("fixture", FIXTURES, ids=IDS)
def test_the_same_input_produces_byte_identical_output(fixture: object) -> None:
    interface_id = fixture.interface_id  # type: ignore[attr-defined]
    payload = fixture.payload  # type: ignore[attr-defined]
    first = run_in_process(interface_id, payload)
    second = run_in_process(interface_id, payload)
    assert first.status == second.status == "ok"
    assert canonical_json(first.output) == canonical_json(second.output)


@pytest.mark.parametrize("fixture", FIXTURES, ids=IDS)
def test_reordering_nothing_is_left_to_iteration_order(fixture: object) -> None:
    """A third run after the dictionaries have been rebuilt.

    Rebuilding the payload gives the interpreter fresh dictionary objects with
    the same contents. Anything that had leaked hash-iteration order into a
    result — a group order, a column order, a pair order — would be free to move.
    """

    interface_id = fixture.interface_id  # type: ignore[attr-defined]
    payload = fixture.payload  # type: ignore[attr-defined]
    rebuilt = dict(reversed(list(payload.items())))
    first = run_in_process(interface_id, payload)
    third = run_in_process(interface_id, rebuilt)
    assert canonical_json(first.output) == canonical_json(third.output)


def test_no_implementation_draws_a_random_number() -> None:
    """A seeded generator is reproducible only until the generator changes."""

    banned = ("import random", "numpy.random", "np.random", "random_state=None", "secrets.")
    for path in sorted(SOURCE_DIR.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        for needle in banned:
            assert needle not in source, f"{path.name} reaches for {needle!r}"


def test_no_output_carries_a_clock_reading_or_a_process_identity() -> None:
    """A wall-clock field in an output makes every run differ from every other."""

    banned = ("time.time", "datetime.now", "utcnow", "os.getpid", "uuid4")
    for path in sorted(SOURCE_DIR.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        for needle in banned:
            assert needle not in source, f"{path.name} reaches for {needle!r}"
