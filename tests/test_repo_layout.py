"""Repository-level invariants.

These are the structural rules the RP-0 contract states about *packaging*, as
opposed to about any one package. They are tested here because there is nowhere
else they could be: a rule about the absence of a shared lock cannot be checked
from inside a package.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGES = REPO_ROOT / "packages"
REAL_PACKAGES = sorted(
    path for path in PACKAGES.iterdir() if path.is_dir() and not path.name.startswith("_")
)


def test_there_are_real_packages() -> None:
    assert [path.name for path in REAL_PACKAGES] == [
        "cruxible-provider-docs",
        "cruxible-provider-noop",
        "cruxible-provider-runtime",
        "cruxible-provider-web",
    ]


@pytest.mark.parametrize("package", REAL_PACKAGES, ids=lambda p: p.name)
def test_every_package_carries_its_own_lock(package: Path) -> None:
    """The per-package lock is the identity source; there is no shared root lock."""

    assert (package / "uv.lock").is_file()


@pytest.mark.parametrize("package", REAL_PACKAGES, ids=lambda p: p.name)
def test_every_package_carries_its_own_license_and_notice(package: Path) -> None:
    assert (package / "LICENSE").is_file()
    assert (package / "NOTICE").is_file()
    assert "Apache License" in (package / "LICENSE").read_text(encoding="utf-8")


@pytest.mark.parametrize("package", REAL_PACKAGES, ids=lambda p: p.name)
def test_every_package_declares_apache_2_0(package: Path) -> None:
    document = tomllib.loads((package / "pyproject.toml").read_text(encoding="utf-8"))
    assert document["project"]["license"] == "Apache-2.0"
    assert set(document["project"]["license-files"]) == {"LICENSE", "NOTICE"}


@pytest.mark.parametrize("package", REAL_PACKAGES, ids=lambda p: p.name)
def test_every_package_ships_a_typing_marker(package: Path) -> None:
    markers = list(package.rglob("py.typed"))
    assert markers, f"{package.name} ships no py.typed"


def test_no_test_directory_is_an_importable_package() -> None:
    """Two packages named ``tests`` collide the moment both are collected.

    The failure is not subtle and it is not local: pytest imports the second
    ``tests/conftest.py`` under the first one's module name and refuses to
    register it, so adding a plane package breaks an unrelated package's suite.
    """

    offenders = [
        str(path.relative_to(REPO_ROOT))
        for package in REAL_PACKAGES
        for path in [package / "tests" / "__init__.py"]
        if path.exists()
    ]
    assert not offenders, f"test directories must not be importable packages: {offenders}"


def test_the_suite_imports_test_modules_by_path() -> None:
    """The other half of the same rule.

    Without ``__init__.py`` a test module's importable name is its bare basename
    under the default import mode, so two ``test_full_loop.py`` files in
    different packages would collide instead. ``importlib`` mode derives a unique
    module name from the path, which is what lets every package name its test
    modules whatever suits it and still keep ``from .conftest import`` working.
    """

    document = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert "--import-mode=importlib" in document["tool"]["pytest"]["ini_options"]["addopts"]


def test_the_default_run_excludes_the_engine_lane() -> None:
    """Opt-in by construction, not by whoever remembers the flag.

    An engine test needs a browser, a tensor stack, or an OCR runtime that the
    base install deliberately does not carry. A lane that is opt-*out* is a lane
    somebody runs by accident on a machine with no engines, where it reads as a
    broken repository rather than as an absent extra.
    """

    document = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    options = document["tool"]["pytest"]["ini_options"]
    assert "not engine" in options["addopts"]
    assert any(marker.startswith("engine:") for marker in options["markers"])


def test_the_root_is_not_a_uv_workspace() -> None:
    """A workspace root would suppress the member locks that carry identity."""

    document = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert "workspace" not in document.get("tool", {}).get("uv", {})
    assert document["tool"]["uv"]["package"] is False


def test_the_root_project_is_not_published() -> None:
    document = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert document["project"]["name"] == "cruxible-providers-dev"
    assert "build-system" not in document


def test_the_root_license_is_apache_2_0() -> None:
    assert "Apache License" in (REPO_ROOT / "LICENSE").read_text(encoding="utf-8")
    assert (REPO_ROOT / "NOTICE").is_file()


def test_the_template_is_not_a_package() -> None:
    """The template must not be installable, lockable, or collectible."""

    template = PACKAGES / "_template"
    assert not (template / "pyproject.toml").exists()
    assert not (template / "uv.lock").exists()
    assert (template / "pyproject.toml.template").is_file()
    assert (template / "LICENSE").is_file()
    assert (template / "NOTICE").is_file()
    assert not list(template.rglob("*.py")), "template sources carry a .template suffix"


def test_no_source_file_names_a_person() -> None:
    """Attribution is by role. Public artifacts carry no personal names."""

    document = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert "authors" not in document["project"]
    for package in REAL_PACKAGES:
        package_document = tomllib.loads((package / "pyproject.toml").read_text(encoding="utf-8"))
        assert package_document["project"]["authors"] == [{"name": "The Cruxible maintainers"}]
