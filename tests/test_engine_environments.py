"""Which engine environments the declared marker environments can pin.

This is the claim ``docs/packaging.md`` makes, asserted against the committed
locks and the committed ``ci/marker-environments.json`` rather than against a
test environment built to be permissive. It is here rather than in a package's
own suite because it is a statement about the *repository's* pins: it reads
every plane package's lock and every declared environment at once.

Two outcomes appear below and they mean different things.

**Resolves.** The tag ordering admits what an installer would admit, so the
engine environment can be pinned in an accepted artifact.

**Refuses, naming a package.** Not a matching bug — the reverse. The declared
environments target glibc 2.17 and macOS 11.0, and `torchvision` publishes only
``manylinux_2_28`` and ``macosx_14_0`` wheels. Those genuinely do not install on
those floors, and pretending otherwise is the one failure mode a pinning
mechanism cannot have. Making that closure pinnable is a decision to raise the
declared floors, which re-pins every package at once and is deliberately not a
change any single package's batch gets to make.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from cruxible_provider_runtime.errors import RefusalCode, RefusalError
from cruxible_provider_runtime.resolution import MarkerEnvironment, load_uv_lock, resolve

REPO_ROOT = Path(__file__).resolve().parent.parent

DECLARED_ENVIRONMENTS = [
    MarkerEnvironment.model_validate(entry)
    for entry in json.loads(
        (REPO_ROOT / "ci" / "marker-environments.json").read_text(encoding="utf-8")
    )["environments"]
]

PINNABLE = [
    pytest.param("cruxible-provider-web", "browser", id="web+browser"),
    pytest.param("cruxible-provider-docs", "paddleocr", id="docs+paddleocr"),
]

# The extra whose closure the declared floors cannot carry, and the package that
# decides it. Named rather than skipped: a refusal that stops being about
# torchvision is a change somebody needs to read.
UNPINNABLE = [pytest.param("cruxible-provider-docs", "docling", "torchvision", id="docs+docling")]


def _resolve(package: str, extra: str, environment: MarkerEnvironment) -> object:
    lock = load_uv_lock(REPO_ROOT / "packages" / package / "uv.lock")
    return resolve(lock, package, environment, extras=[extra], allow_editable_dev_sources=True)


@pytest.mark.parametrize("environment", DECLARED_ENVIRONMENTS, ids=lambda env: env.id)
@pytest.mark.parametrize(("package", "extra"), PINNABLE)
def test_an_engine_environment_resolves_for_every_declared_environment(
    package: str, extra: str, environment: MarkerEnvironment
) -> None:
    resolved = _resolve(package, extra, environment)
    assert resolved.distributions  # type: ignore[attr-defined]


@pytest.mark.parametrize("environment", DECLARED_ENVIRONMENTS, ids=lambda env: env.id)
@pytest.mark.parametrize(("package", "extra", "blocker"), UNPINNABLE)
def test_an_engine_the_declared_floors_cannot_carry_refuses_and_names_it(
    package: str, extra: str, blocker: str, environment: MarkerEnvironment
) -> None:
    with pytest.raises(RefusalError) as exc:
        _resolve(package, extra, environment)
    assert exc.value.code is RefusalCode.NO_COMPATIBLE_ARTIFACT
    assert exc.value.refusal.detail["package"] == blocker


@pytest.mark.parametrize("environment", DECLARED_ENVIRONMENTS, ids=lambda env: env.id)
@pytest.mark.parametrize(
    "package",
    [
        "cruxible-provider-docs",
        "cruxible-provider-noop",
        "cruxible-provider-quant",
        "cruxible-provider-runtime",
        "cruxible-provider-web",
    ],
)
def test_every_base_environment_still_resolves(
    package: str, environment: MarkerEnvironment
) -> None:
    """The half that was already true, kept true: the ordering only widens."""

    lock = load_uv_lock(REPO_ROOT / "packages" / package / "uv.lock")
    resolve(lock, package, environment, allow_editable_dev_sources=True)
