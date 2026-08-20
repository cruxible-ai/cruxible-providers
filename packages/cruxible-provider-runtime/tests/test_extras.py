"""Extras as a resolution input, and as part of an environment's identity.

A provider package keeps its heavy engines behind per-engine extras so that the
base distribution stays light. That makes the extras selection part of *which
environment* a bind materializes: one lock, several environments, one per extras
set. These tests pin the three consequences —

1. selecting an extra changes the resolved set, and therefore the
   materialization digest, without touching the lock;
2. dependency-level extras are followed (the document plane's real lock reaches
   its engine through three of them);
3. an extra nothing declares refuses instead of quietly resolving to the base
   set, which would materialize an environment without the engine an
   implementation said it needed.

The extras deliberately do **not** appear in any digest preimage of their own.
Their effect is the packages they pull in, and those are already triples in the
preimage; a second, independent statement of the same fact is a second thing
that can be wrong.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from cruxible_provider_runtime.digests import materialization_digest
from cruxible_provider_runtime.errors import RefusalCode, RefusalError
from cruxible_provider_runtime.resolution import (
    MarkerEnvironment,
    UvLock,
    environment_pin_key,
    load_uv_lock,
    resolve,
)

FIXTURES = Path(__file__).parent / "fixtures"
ROOT = "sample-provider"
DISTRIBUTION_SHA256 = "sha256:" + "9c" * 32


@pytest.fixture(scope="session")
def extras_lock() -> UvLock:
    return load_uv_lock(FIXTURES / "extras.uv.lock")


def _names(lock: UvLock, env: MarkerEnvironment, *extras: str) -> set[str]:
    return {entry.name for entry in resolve(lock, ROOT, env, extras=extras).distributions}


def test_the_base_resolution_carries_no_extra(
    extras_lock: UvLock, linux_env: MarkerEnvironment
) -> None:
    assert _names(extras_lock, linux_env) == {"leaf-base"}


def test_selecting_an_extra_pulls_its_packages_in(
    extras_lock: UvLock, linux_env: MarkerEnvironment
) -> None:
    assert _names(extras_lock, linux_env, "engine") == {
        "leaf-base",
        "heavy-engine",
        "engine-core",
        # Reached only through the dependency-level extra on heavy-engine.
        "accel-kernel",
    }


def test_a_dependency_level_extra_is_followed(
    extras_lock: UvLock, linux_env: MarkerEnvironment
) -> None:
    """The edge the first cut of the resolver dropped.

    ``{ name = "engine-core", extra = ["accel"] }`` is how a lock spells "this
    dependency, with that extra". Ignoring the ``extra`` key resolves
    engine-core's base dependencies and stops, producing an environment pin that
    does not cover part of the environment — the same class of hole that
    silently-dropped local sources were.
    """

    with_extra = _names(extras_lock, linux_env, "engine")
    assert "accel-kernel" in with_extra
    assert "accel-kernel" not in _names(extras_lock, linux_env)


def test_two_extras_sets_over_one_lock_are_two_environments(
    extras_lock: UvLock, linux_env: MarkerEnvironment
) -> None:
    def pin(*extras: str) -> str:
        resolved = resolve(extras_lock, ROOT, linux_env, extras=extras)
        return materialization_digest(resolved, distribution_sha256=DISTRIBUTION_SHA256)

    base, engine, vision, both = pin(), pin("engine"), pin("vision"), pin("engine", "vision")
    assert len({base, engine, vision, both}) == 4


def test_extras_order_does_not_move_the_pin(
    extras_lock: UvLock, linux_env: MarkerEnvironment
) -> None:
    """The selection is a set. Spelling it in two orders is one environment."""

    one = resolve(extras_lock, ROOT, linux_env, extras=("engine", "vision"))
    other = resolve(extras_lock, ROOT, linux_env, extras=("vision", "engine"))
    assert one.extras == other.extras == ("engine", "vision")
    assert materialization_digest(one, distribution_sha256=DISTRIBUTION_SHA256) == (
        materialization_digest(other, distribution_sha256=DISTRIBUTION_SHA256)
    )


def test_an_undeclared_root_extra_refuses(
    extras_lock: UvLock, linux_env: MarkerEnvironment
) -> None:
    with pytest.raises(RefusalError) as exc:
        resolve(extras_lock, ROOT, linux_env, extras=("gpu",))
    assert exc.value.code is RefusalCode.UNKNOWN_EXTRA
    assert exc.value.refusal.detail["extra"] == "gpu"


def test_an_undeclared_dependency_extra_refuses(
    extras_lock: UvLock, linux_env: MarkerEnvironment
) -> None:
    """A lock asking for an extra of a package that has none is not resolvable.

    Resolving to the base set here would be the fail-open reading: the
    environment would come out one engine short and every digest over it would
    still verify.
    """

    with pytest.raises(RefusalError) as exc:
        resolve(extras_lock, ROOT, linux_env, extras=("unresolvable",))
    assert exc.value.code is RefusalCode.UNKNOWN_EXTRA
    assert exc.value.refusal.detail["package"] == "leaf-base"


def test_the_resolution_records_which_extras_it_is_a_resolution_of(
    extras_lock: UvLock, linux_env: MarkerEnvironment
) -> None:
    resolved = resolve(extras_lock, ROOT, linux_env, extras=("vision", "engine", "engine"))
    assert resolved.extras == ("engine", "vision")
    assert resolved.pin_key() == "linux-cp311+engine+vision"


def test_the_extras_do_not_enter_the_preimage_on_their_own(
    extras_lock: UvLock, linux_env: MarkerEnvironment
) -> None:
    """Extras reach identity through the resolved set, and only through it.

    Constructed here by hand: a resolution that *claims* extras but resolved to
    the same packages digests identically to one that claims none. That is the
    intended property — the environment is what it contains — and it is what
    keeps every pin written before extras existed valid.
    """

    plain = resolve(extras_lock, ROOT, linux_env)
    relabelled = plain.model_copy(update={"extras": ("engine",)})
    assert materialization_digest(plain, distribution_sha256=DISTRIBUTION_SHA256) == (
        materialization_digest(relabelled, distribution_sha256=DISTRIBUTION_SHA256)
    )


def test_the_pin_key_of_no_extras_is_the_bare_environment_id() -> None:
    """Every pin written before extras existed still reads correctly."""

    assert environment_pin_key("linux-cp311") == "linux-cp311"
    assert environment_pin_key("linux-cp311", []) == "linux-cp311"
    assert environment_pin_key("linux-cp311", ["docling"]) == "linux-cp311+docling"
    assert environment_pin_key("linux-cp311", ["b", "a"]) == "linux-cp311+a+b"
