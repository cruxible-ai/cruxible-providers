"""The closure-version check: a released package whose closure moved has a new version.

The rule: for every package present at the base and the head whose
dependency-closure digest moved, its version must have changed too, unless the
package has never been released (``PUBLISHABLE`` in the publish workflow names
it at neither revision). A wheel embeds its lock, so a released version is one
environment; a moved lock under an old version would publish a second
environment under a name an operator may already have pinned.

These tests exercise the check against a synthetic repository rather than
against real history, so they assert the check's behaviour rather than a
property of whatever happens to be committed.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.check_closure_moves_are_versioned import main as check_main  # noqa: E402
from scripts.check_closure_moves_are_versioned import publishable  # noqa: E402
from scripts.dependency_closure_digests import compute, load_environments  # noqa: E402


def test_every_package_digests_for_every_marker_environment() -> None:
    environments = {env.id for env in load_environments(REPO_ROOT)}
    digests = compute(REPO_ROOT)
    assert digests
    for per_env in digests.values():
        assert set(per_env) == environments


def test_a_digest_is_computed_per_package_not_per_repository() -> None:
    """Each package resolves from its own lock, so each has its own answer.

    The umbrella is absent, and deliberately: it declares itself exempt because
    its entire content is other packages, so its closure moves whenever any
    plane's does, and it pins no environment of its own.
    """

    digests = compute(REPO_ROOT)
    assert set(digests) == {
        "cruxible-provider-docs",
        "cruxible-provider-noop",
        "cruxible-provider-quant",
        "cruxible-provider-runtime",
        "cruxible-provider-web",
    }


def test_released_means_the_publish_workflow_can_publish_it() -> None:
    """The repository's own rule for "released": no network, the publishable set."""

    assert publishable(REPO_ROOT) == {"cruxible-provider-runtime", "cruxible-provider-web"}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def _publish_workflow(repo: Path, *names: str) -> None:
    workflow = repo / ".github" / "workflows" / "publish.yml"
    workflow.parent.mkdir(parents=True, exist_ok=True)
    workflow.write_text(
        f'name: publish\nenv:\n  PUBLISHABLE: "{" ".join(names)}"\njobs: {{}}\n',
        encoding="utf-8",
    )


def _pyproject(package: Path, name: str, version: str) -> None:
    (package / "pyproject.toml").write_text(
        f'[project]\nname = "cruxible-provider-{name}"\nversion = "{version}"\n',
        encoding="utf-8",
    )


@pytest.fixture()
def synthetic_repo(tmp_path: Path) -> Path:
    """Two independently locked packages: ``alpha`` released, ``beta`` never released."""

    repo = tmp_path / "repo"
    (repo / "ci").mkdir(parents=True)
    shutil.copyfile(
        REPO_ROOT / "ci" / "marker-environments.json", repo / "ci" / "marker-environments.json"
    )
    _publish_workflow(repo, "cruxible-provider-alpha")
    for name in ("alpha", "beta"):
        package = repo / "packages" / f"cruxible-provider-{name}"
        package.mkdir(parents=True)
        _pyproject(package, name, "0.1.0")
        (package / "uv.lock").write_text(_lock(name, "1.0.0"), encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "ci@example.invalid")
    _git(repo, "config", "user.name", "ci")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


def _lock(name: str, dependency_version: str, editable_on: str | None = None) -> str:
    digest = "a" * 64 if dependency_version == "1.0.0" else "b" * 64
    sibling_dependency = (
        f'    {{ name = "cruxible-provider-{editable_on}" }},\n' if editable_on else ""
    )
    sibling_package = (
        f"""
[[package]]
name = "cruxible-provider-{editable_on}"
version = "0.1.0"
source = {{ editable = "../cruxible-provider-{editable_on}" }}
dependencies = [
    {{ name = "shared-leaf" }},
]
"""
        if editable_on
        else ""
    )
    return f"""version = 1
revision = 3
requires-python = ">=3.11"

[[package]]
name = "cruxible-provider-{name}"
version = "0.1.0"
source = {{ editable = "." }}
dependencies = [
    {{ name = "shared-leaf" }},
{sibling_dependency}]
{sibling_package}
[[package]]
name = "shared-leaf"
version = "{dependency_version}"
source = {{ registry = "https://pypi.example/simple" }}
wheels = [
    {{ url = "https://pypi.example/files/shared_leaf-{dependency_version}-py3-none-any.whl", hash = "sha256:{digest}" }},
]
"""


def _move(repo: Path, name: str, *, version: str | None = None) -> Path:
    """Move ``name``'s closure (its shared dependency to 2.0.0), optionally bumping it."""

    package = repo / "packages" / f"cruxible-provider-{name}"
    (package / "uv.lock").write_text(_lock(name, "2.0.0"), encoding="utf-8")
    if version is not None:
        _pyproject(package, name, version)
    return package


def _check(repo: Path, *extra: str) -> int:
    return check_main(["--repo", str(repo), "--base", "HEAD", *extra])


def test_a_released_package_moved_and_bumped_passes(synthetic_repo: Path) -> None:
    _move(synthetic_repo, "alpha", version="0.2.0")
    assert _check(synthetic_repo) == 0


def test_a_released_package_moved_without_a_bump_fails(
    synthetic_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _move(synthetic_repo, "alpha")
    assert _check(synthetic_repo) == 1
    out = capsys.readouterr().out
    assert "MOVED  cruxible-provider-alpha 0.1.0  (released)  <- version not changed" in out
    assert "FAIL" in out and "cruxible-provider-alpha" in out


def test_an_unreleased_package_moved_without_a_bump_passes(
    synthetic_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _move(synthetic_repo, "beta")
    assert _check(synthetic_repo) == 0
    out = capsys.readouterr().out
    assert "MOVED  cruxible-provider-beta 0.1.0  (not released)" in out
    assert "unchanged  cruxible-provider-alpha 0.1.0  (released)" in out


def test_no_change_passes(synthetic_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _check(synthetic_repo) == 0
    out = capsys.readouterr().out
    assert "unchanged  cruxible-provider-alpha" in out
    assert "unchanged  cruxible-provider-beta" in out


def test_moving_several_packages_is_no_longer_a_failure_by_itself(synthetic_repo: Path) -> None:
    """The count of moved packages is not the rule; each released one needs a version."""

    _move(synthetic_repo, "alpha", version="0.2.0")
    _move(synthetic_repo, "beta")
    assert _check(synthetic_repo) == 0


def test_a_package_taken_off_the_publishable_list_still_counts_as_released(
    synthetic_repo: Path,
) -> None:
    """Released at the base is released: dropping the name cannot license the move."""

    _publish_workflow(synthetic_repo)
    _move(synthetic_repo, "alpha")
    assert _check(synthetic_repo) == 1


def test_a_package_added_to_the_publishable_list_is_judged_as_released(
    synthetic_repo: Path,
) -> None:
    """Listed at the head means it is about to be published: its version must say so."""

    _publish_workflow(synthetic_repo, "cruxible-provider-alpha", "cruxible-provider-beta")
    _move(synthetic_repo, "beta")
    assert _check(synthetic_repo) == 1


def test_an_unresolvable_base_fails(synthetic_repo: Path) -> None:
    """A gate that cannot run reports failure, never green.

    Reporting green when the comparison did not happen is worse than reporting
    nothing: it is the shallow-clone, renamed-branch, rewritten-base case, which
    is exactly when a reviewer most needs to be told the gate was blind.
    """

    assert check_main(["--repo", str(synthetic_repo), "--base", "no-such-ref"]) == 1


def test_an_unresolvable_base_can_be_waived_explicitly(synthetic_repo: Path) -> None:
    """For the initial commit, and it has to be asked for."""

    assert (
        check_main(["--repo", str(synthetic_repo), "--base", "no-such-ref", "--allow-missing-base"])
        == 0
    )


def test_a_cross_package_editable_bump_is_visible_to_the_check(synthetic_repo: Path) -> None:
    """The scenario the silent local-source skip used to hide.

    ``beta`` depends on ``alpha`` by path. Bumping the dependency both share
    moves both closures, and the check has to see it: the released ``alpha``
    needs a new version, the never-released ``beta`` does not.
    """

    alpha = synthetic_repo / "packages" / "cruxible-provider-alpha"
    beta = synthetic_repo / "packages" / "cruxible-provider-beta"
    (beta / "uv.lock").write_text(_lock("beta", "1.0.0", editable_on="alpha"), encoding="utf-8")
    _git(synthetic_repo, "add", "-A")
    _git(synthetic_repo, "commit", "-q", "-m", "beta depends on alpha by path")

    (alpha / "uv.lock").write_text(_lock("alpha", "2.0.0"), encoding="utf-8")
    (beta / "uv.lock").write_text(_lock("beta", "2.0.0", editable_on="alpha"), encoding="utf-8")
    assert _check(synthetic_repo) == 1
    _pyproject(alpha, "alpha", "0.2.0")
    assert _check(synthetic_repo) == 0


def test_the_editable_edge_itself_enters_the_closure(synthetic_repo: Path) -> None:
    """Not just the shared dependency: the path edge is in the resolved set."""

    beta = synthetic_repo / "packages" / "cruxible-provider-beta"
    (beta / "uv.lock").write_text(_lock("beta", "1.0.0", editable_on="alpha"), encoding="utf-8")

    from cruxible_provider_runtime.resolution import load_uv_lock, resolve

    environments = load_environments(REPO_ROOT)
    resolved = resolve(
        load_uv_lock(beta / "uv.lock"),
        "cruxible-provider-beta",
        environments[0],
        allow_editable_dev_sources=True,
    )
    local = [entry for entry in resolved.distributions if entry.is_local_source]
    assert [entry.name for entry in local] == ["cruxible-provider-alpha"]
    assert local[0].artifact_id == "editable:../cruxible-provider-alpha"


def test_added_and_removed_packages_are_reported_not_judged(
    synthetic_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Nothing was published from a package absent at the base; a removed one publishes no more."""

    gamma = synthetic_repo / "packages" / "cruxible-provider-gamma"
    gamma.mkdir(parents=True)
    _pyproject(gamma, "gamma", "0.1.0")
    (gamma / "uv.lock").write_text(_lock("gamma", "2.0.0"), encoding="utf-8")
    shutil.rmtree(synthetic_repo / "packages" / "cruxible-provider-alpha")
    assert _check(synthetic_repo) == 0
    out = capsys.readouterr().out
    assert "added  cruxible-provider-gamma 0.1.0" in out
    assert "removed  cruxible-provider-alpha 0.1.0  (released)" in out


def test_an_addition_does_not_license_an_unversioned_move(synthetic_repo: Path) -> None:
    gamma = synthetic_repo / "packages" / "cruxible-provider-gamma"
    gamma.mkdir(parents=True)
    _pyproject(gamma, "gamma", "0.1.0")
    (gamma / "uv.lock").write_text(_lock("gamma", "1.0.0"), encoding="utf-8")
    _move(synthetic_repo, "alpha")
    assert _check(synthetic_repo) == 1
