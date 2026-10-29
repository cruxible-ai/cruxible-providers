"""Drift guards on the package's registration and its manifest transcription."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import cruxible_provider_web
import pytest
from cruxible_provider_runtime.manifest import ENTRYPOINT_GROUP, ProviderManifest
from cruxible_provider_runtime.registration import load_registration
from cruxible_provider_web import operations
from cruxible_provider_web.fetch import WebFetch
from cruxible_provider_web.search import SearxngSearch

SRC = Path(cruxible_provider_web.__file__).resolve().parent
BUNDLE = load_registration(cruxible_provider_web.PACKAGE_ROOT)

SERVED = {
    operations.FETCH_OPERATION: WebFetch,
    operations.SEARCH_OPERATION: SearxngSearch,
}

# Every module whose code path an operation's run can reach, per operation.
REACHED_MODULES = {
    operations.FETCH_OPERATION: ("fetch.py", "http.py", "engines.py"),
    operations.SEARCH_OPERATION: ("search.py", "http.py"),
}


def test_the_registration_declares_what_each_entrypoint_serves() -> None:
    assert [item.name for item in BUNDLE.operations] == ["search.web", "web.fetch"]
    for operation in BUNDLE.operations:
        entrypoint = SERVED[operation.name]
        assert entrypoint.operations == (operation.name,)
        assert operation.entrypoint == f"{entrypoint.__module__}:{entrypoint.__qualname__}"
        assert operation.effect == "external_read"
        assert operation.in_.spelling == f"{operation.name}.request@1"
        assert operation.out.spelling == f"{operation.name}.result@1"
        for code, refusal in operation.refusals.items():
            assert refusal.contract.spelling == f"{operation.name}.refusal.{code}@1"


@pytest.mark.parametrize("name", sorted(REACHED_MODULES))
def test_the_declared_refusals_are_the_codes_the_code_can_return(name: str) -> None:
    """Every operation-refusal constant the reached code uses is declared, and no more."""

    constants = {
        attr: getattr(operations, attr)
        for attr in operations.__all__
        if attr.isupper() and not attr.endswith("_OPERATION")
    }
    used: set[str] = set()
    for module in REACHED_MODULES[name]:
        text = (SRC / module).read_text(encoding="utf-8")
        used |= {value for attr, value in constants.items() if re.search(rf"\b{attr}\b", text)}
    assert set(BUNDLE.operation(name).refusals) == used


def test_the_manifest_version_tracks_the_package_version(manifest: ProviderManifest) -> None:
    assert manifest.distribution.version == cruxible_provider_web.__version__


def test_the_entry_points_are_the_registered_operations(manifest_path: Path) -> None:
    """The registration and the packaging metadata must name the same objects."""

    pyproject = manifest_path.parents[2] / "pyproject.toml"
    declared = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["entry-points"][
        ENTRYPOINT_GROUP
    ]
    assert declared == BUNDLE.entry_points()


def test_the_declared_extras_exist_in_the_distribution(
    manifest: ProviderManifest, manifest_path: Path
) -> None:
    """An implementation cannot require an engine the distribution cannot install.

    The bind path refuses this against the lock; this catches it against the
    project metadata, which is where it would be introduced.
    """

    pyproject = tomllib.loads(
        (manifest_path.parents[2] / "pyproject.toml").read_text(encoding="utf-8")
    )
    available = set(pyproject["project"].get("optional-dependencies", {}))
    for implementation in manifest.implementations:
        assert set(implementation.requires_extras) <= available
