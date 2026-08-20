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
        "cruxible-provider-quant",
        "cruxible-provider-runtime",
        "cruxible-provider-web",
        "cruxible-providers",
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
def test_every_package_that_ships_code_ships_a_typing_marker(package: Path) -> None:
    if not (package / "src").is_dir():
        # The umbrella. A typing marker for a distribution with nothing to type
        # would be a claim about an empty set.
        assert _is_umbrella(package), f"{package.name} has no src/ and is not the umbrella"
        return
    assert list(package.rglob("py.typed")), f"{package.name} ships no py.typed"


def _is_umbrella(package: Path) -> bool:
    document = tomllib.loads((package / "pyproject.toml").read_text(encoding="utf-8"))
    return document.get("tool", {}).get("cruxible", {}).get("role") == "umbrella"


def _exempt_packages() -> list[Path]:
    import sys

    sys.path.insert(0, str(REPO_ROOT))
    from scripts.dependency_closure_digests import is_exempt

    return [package for package in REAL_PACKAGES if is_exempt(package)]


EXEMPT_PACKAGES = _exempt_packages()


def test_the_exemption_allowlist_holds() -> None:
    """An exemption that spread would quietly disable the gate.

    Fail-closed on the name, and then — in the test below — on the property that
    justifies the name. Both halves are needed: this one catches an exemption
    appearing, and that one catches an exemption being granted to something the
    justification does not cover.
    """

    assert [package.name for package in EXEMPT_PACKAGES] == ["cruxible-providers"]


@pytest.mark.parametrize("package", EXEMPT_PACKAGES, ids=lambda p: p.name)
def test_an_exempt_package_ships_no_code(package: Path) -> None:
    """The property the exemption rests on, checked per exempt package.

    A package skips the digest-scope gate because it *is* nothing but
    dependencies: its closure moves whenever any package it names does, and it
    pins no environment, carries no implementation digest, and appears on no
    track record. That argument holds only for a distribution with no code in it
    — a distribution with code enters an implementation digest, and one that
    entered every provider's digest would re-digest the fleet on every release,
    which is the exact failure the per-plane split exists to prevent.

    Parametrised over the exempt set rather than written against a path, so that
    a future addition to the allowlist inherits the check instead of only the
    exemption.
    """

    assert _is_umbrella(package), (
        f"{package.name} is exempt from the digest-scope gate without declaring the role "
        "that justifies it"
    )
    assert not (package / "src").exists(), f"{package.name} is exempt and ships a src/ tree"
    assert not list(package.rglob("*.py")), f"{package.name} is exempt and ships code"
    document = tomllib.loads((package / "pyproject.toml").read_text(encoding="utf-8"))
    assert document["project"]["dependencies"] == [], (
        f"{package.name} is a dependency shell, so installing it bare must install nothing; "
        "everything it offers belongs behind an extra"
    )
    assert document["project"]["optional-dependencies"], (
        f"{package.name} is exempt for being nothing but extras, and declares none"
    )


def test_the_umbrella_offers_one_extra_per_plane_plus_all() -> None:
    """Specific to this umbrella, unlike the property test above.

    One extra per plane, plus `all` -- which must be exactly the full union of
    the plane extras: an `all` that means "all but one" is worse than no `all`.
    """

    document = tomllib.loads(
        (PACKAGES / "cruxible-providers" / "pyproject.toml").read_text(encoding="utf-8")
    )
    extras = document["project"]["optional-dependencies"]
    assert set(extras) == {"web", "docs", "quant", "all"}
    plane_union = {dep for name, deps in extras.items() if name != "all" for dep in deps}
    assert set(extras["all"]) == plane_union


def test_ci_typechecks_every_real_package() -> None:
    """The mypy matrix must name exactly the packages that exist.

    Parsed with a loader that refuses duplicate keys: YAML's last-key-wins is
    how a merge once dropped two packages from this matrix without a sound —
    the discarded key was still sitting in the file, reading as coverage.
    """

    import yaml

    class _NoDuplicateKeysLoader(yaml.SafeLoader):
        pass

    def _construct_mapping(loader: yaml.SafeLoader, node: yaml.MappingNode) -> dict[object, object]:
        seen: dict[object, object] = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=True)
            assert key not in seen, f"duplicate YAML key {key!r} at {key_node.start_mark}"
            seen[key] = loader.construct_object(value_node, deep=True)
        return seen

    _NoDuplicateKeysLoader.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping
    )

    workflow_text = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    workflow = yaml.load(workflow_text, Loader=_NoDuplicateKeysLoader)
    matrix = workflow["jobs"]["typecheck"]["strategy"]["matrix"]["package"]
    code_packages = {package.name for package in REAL_PACKAGES if (package / "src").is_dir()}
    assert set(matrix) == code_packages
    assert len(matrix) == len(code_packages), "the matrix lists a package twice"


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
