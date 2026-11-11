#!/usr/bin/env python
"""Assert that every released package whose dependency closure moved has a new version.

    uv run python scripts/check_closure_moves_are_versioned.py --base origin/main

A provider wheel embeds its package's ``uv.lock``, and an install by name builds
the environment from that embedded lock. So a released version *is* one
dependency closure: if a package's lock moves and its version does not, the next
release would publish a different environment under a version an operator may
already have installed and pinned, and the two could not be told apart by name.
This check makes that impossible to merge.

**The rule.** For every package present at both the base revision and the
working tree whose per-environment dependency-closure digests moved, its
``[project] version`` must differ between the two -- unless the package has
never been released.

**Released** is decided from the repository alone, with no network: a package
is released when ``.github/workflows/publish.yml`` names it in its
``PUBLISHABLE`` set at the base or at the head. That set is the only way a
package reaches the index (the publish workflow refuses every other name), so a
package outside it has no published version a moved closure could contradict. A
package taken off the list in the same change still counts, from the base.

**Moved** is compared over packages present in both revisions. A package added
or removed is reported and not judged: nothing was published from a package
that did not exist at the base, and a removed package publishes nothing more.

The umbrella declares itself out of this measurement (``[tool.cruxible]
digest_scope = "exempt"``): its entire content is other packages, so its closure
moves whenever any plane's does, and it pins no environment of its own.

An unresolvable base is a **failure**, not a pass. A gate that waves changes
through whenever it cannot do its job reports green in exactly the situation
nobody checked -- a shallow clone, a renamed branch, a rewritten base.
``--allow-missing-base`` exists for the one legitimate case, the initial
commit, and has to be asked for.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.dependency_closure_digests import (  # noqa: E402
    compute,
    distribution_name,
    load_environments,
    package_dirs,
)

PUBLISH_WORKFLOW = Path(".github/workflows/publish.yml")


def _materialize_base(repo: Path, base: str, target: Path) -> None:
    """Extract the base revision's tree into ``target`` with ``git archive``."""

    archive = target / "base.tar"
    with archive.open("wb") as handle:
        subprocess.run(
            ["git", "archive", "--format=tar", base],
            cwd=repo,
            check=True,
            stdout=handle,
        )
    extracted = target / "tree"
    extracted.mkdir()
    with tarfile.open(archive) as tar:
        if hasattr(tarfile, "data_filter"):
            tar.extractall(extracted, filter="data")
        else:  # pragma: no cover - only on interpreters without the tar filters
            tar.extractall(extracted)


def versions(repo: Path) -> dict[str, str]:
    """Each measured package's declared ``[project] version``, by distribution name."""

    found: dict[str, str] = {}
    for package_dir in package_dirs(repo):
        document = tomllib.loads((package_dir / "pyproject.toml").read_text(encoding="utf-8"))
        found[distribution_name(package_dir)] = str(document["project"]["version"])
    return found


def publishable(repo: Path) -> set[str]:
    """The ``PUBLISHABLE`` names in ``repo``'s publish workflow; none if it has no such set."""

    workflow = repo / PUBLISH_WORKFLOW
    if not workflow.is_file():
        return set()
    document = yaml.safe_load(workflow.read_text(encoding="utf-8")) or {}
    env = document.get("env") or {}
    return set(str(env.get("PUBLISHABLE", "")).split())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="HEAD~1", help="revision to compare against")
    parser.add_argument("--repo", type=Path, default=REPO_ROOT)
    parser.add_argument(
        "--allow-missing-base",
        action="store_true",
        help="treat an unresolvable base as a pass; for the initial commit only",
    )
    args = parser.parse_args(argv)
    repo = args.repo.resolve()

    # The environment list is held fixed across both revisions, so this check
    # measures lock movement and nothing else. Re-pinning the environments is a
    # deliberate, separate act.
    environments = load_environments(repo)
    head = compute(repo, environments)
    head_versions = versions(repo)
    released = publishable(repo)
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp)
        try:
            _materialize_base(repo, args.base, target)
        except subprocess.CalledProcessError:
            if args.allow_missing_base:
                print(f"cannot resolve base revision {args.base!r}; skipping as permitted")
                return 0
            print(
                f"FAIL: cannot resolve base revision {args.base!r}, so the closure-version "
                "check could not run. Pass --allow-missing-base only if there is genuinely "
                "no base to compare against."
            )
            return 1
        base_tree = target / "tree"
        base = compute(base_tree, environments)
        base_versions = versions(base_tree)
        released |= publishable(base_tree)

    added = sorted(set(head) - set(base))
    removed = sorted(set(base) - set(head))
    unversioned: list[str] = []
    for name in sorted(set(head) | set(base)):
        status = "released" if name in released else "not released"
        if name in added:
            print(f"{'added':>9}  {name} {head_versions[name]}  ({status})")
            continue
        if name in removed:
            print(f"{'removed':>9}  {name} {base_versions[name]}  ({status})")
            continue
        before, after = base_versions[name], head_versions[name]
        version = before if before == after else f"{before} -> {after}"
        if head[name] == base[name]:
            print(f"{'unchanged':>9}  {name} {version}  ({status})")
            continue
        verdict = ""
        if before == after:
            if name in released:
                unversioned.append(name)
                verdict = "  <- version not changed"
            else:
                verdict = "  (version not changed; allowed, never released)"
        print(f"{'MOVED':>9}  {name} {version}  ({status}){verdict}")

    if unversioned:
        print(
            f"\nFAIL: the dependency closure of released package(s) {unversioned} moved "
            "without a version change.\nA wheel embeds its lock, so a released version is "
            "one environment: give each a new version in its pyproject.toml (and wherever "
            "the package states its version) before this lands."
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
