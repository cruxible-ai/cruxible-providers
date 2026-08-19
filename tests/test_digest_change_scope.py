"""The one-package-one-digest-change check.

The rule: a dependency bump in one package must change exactly one package's
materialization digest. That is the whole reason each package keeps its own lock
rather than sharing a workspace root lock — a shared lock would re-pin, and
therefore re-verify and re-attest, providers that did not change.

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

from scripts.check_single_package_digest_change import main as check_main  # noqa: E402
from scripts.materialization_digests import compute, load_environments  # noqa: E402


def test_every_package_digests_for_every_marker_environment() -> None:
    environments = {env.id for env in load_environments(REPO_ROOT)}
    digests = compute(REPO_ROOT)
    assert digests
    for per_env in digests.values():
        assert set(per_env) == environments


def test_a_digest_is_computed_per_package_not_per_repository() -> None:
    """Each package resolves from its own lock, so each has its own answer."""

    digests = compute(REPO_ROOT)
    assert set(digests) == {"cruxible-provider-noop", "cruxible-provider-runtime"}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture()
def synthetic_repo(tmp_path: Path) -> Path:
    """A miniature repository with two independently locked packages."""

    repo = tmp_path / "repo"
    (repo / "ci").mkdir(parents=True)
    shutil.copyfile(
        REPO_ROOT / "ci" / "marker-environments.json", repo / "ci" / "marker-environments.json"
    )
    for name, version in (("alpha", "1.0.0"), ("beta", "1.0.0")):
        package = repo / "packages" / f"cruxible-provider-{name}"
        package.mkdir(parents=True)
        (package / "pyproject.toml").write_text(
            f'[project]\nname = "cruxible-provider-{name}"\nversion = "0.1.0"\n',
            encoding="utf-8",
        )
        (package / "uv.lock").write_text(_lock(name, version), encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "ci@example.invalid")
    _git(repo, "config", "user.name", "ci")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


def _lock(name: str, dependency_version: str) -> str:
    digest = "a" * 64 if dependency_version == "1.0.0" else "b" * 64
    return f"""version = 1
revision = 3
requires-python = ">=3.11"

[[package]]
name = "cruxible-provider-{name}"
version = "0.1.0"
source = {{ editable = "." }}
dependencies = [
    {{ name = "shared-leaf" }},
]

[[package]]
name = "shared-leaf"
version = "{dependency_version}"
source = {{ registry = "https://pypi.example/simple" }}
wheels = [
    {{ url = "https://pypi.example/files/shared_leaf-{dependency_version}-py3-none-any.whl", hash = "sha256:{digest}" }},
]
"""


def test_bumping_one_package_passes(synthetic_repo: Path) -> None:
    package = synthetic_repo / "packages" / "cruxible-provider-alpha"
    (package / "uv.lock").write_text(_lock("alpha", "2.0.0"), encoding="utf-8")
    assert check_main(["--repo", str(synthetic_repo), "--base", "HEAD"]) == 0


def test_bumping_two_packages_fails(synthetic_repo: Path) -> None:
    for name in ("alpha", "beta"):
        package = synthetic_repo / "packages" / f"cruxible-provider-{name}"
        (package / "uv.lock").write_text(_lock(name, "2.0.0"), encoding="utf-8")
    assert check_main(["--repo", str(synthetic_repo), "--base", "HEAD"]) == 1


def test_no_change_passes(synthetic_repo: Path) -> None:
    assert check_main(["--repo", str(synthetic_repo), "--base", "HEAD"]) == 0


def test_an_unresolvable_base_is_skipped_not_failed(synthetic_repo: Path) -> None:
    assert check_main(["--repo", str(synthetic_repo), "--base", "no-such-ref"]) == 0
