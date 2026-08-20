"""Every refusal code must be exercised by a test.

A taxonomy with unexercised members is a taxonomy that has drifted from the code
without anyone noticing — the fail-closed paths are exactly the paths nobody
takes by accident, so they only stay correct if something insists on taking
them.

The check is static: a code counts as exercised when some test file asserts on
it by name. That is weaker than tracing raises at runtime and stronger than
nothing, and it fails in the useful direction — adding a code without a test
breaks the build.
"""

from __future__ import annotations

from pathlib import Path

from cruxible_provider_runtime.errors import RefusalCode

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_DIRS = [
    REPO_ROOT / "tests",
    REPO_ROOT / "packages" / "cruxible-provider-runtime" / "tests",
    REPO_ROOT / "packages" / "cruxible-provider-noop" / "tests",
    REPO_ROOT / "packages" / "cruxible-provider-quant" / "tests",
]


def _test_sources() -> str:
    chunks: list[str] = []
    for directory in TEST_DIRS:
        for path in sorted(directory.rglob("test_*.py")):
            if path.name == Path(__file__).name:
                continue
            chunks.append(path.read_text(encoding="utf-8"))
    return "\n".join(chunks)


def test_every_refusal_code_is_asserted_somewhere() -> None:
    sources = _test_sources()
    unexercised = sorted(
        code.name for code in RefusalCode if f"RefusalCode.{code.name}" not in sources
    )
    assert not unexercised, (
        "these refusal codes are not asserted by any test: "
        f"{unexercised}. Either exercise the path or remove the code."
    )


def test_refusal_code_values_are_snake_case_of_their_names() -> None:
    for code in RefusalCode:
        assert code.value == code.name.lower()


def test_the_taxonomy_is_a_closed_set() -> None:
    """No code may be constructed that the taxonomy does not declare."""

    import pytest

    with pytest.raises(ValueError, match="is not a valid RefusalCode"):
        RefusalCode("improvised_reason")
