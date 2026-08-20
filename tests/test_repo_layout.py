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
        "cruxible-provider-noop",
        "cruxible-provider-quant",
        "cruxible-provider-runtime",
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
