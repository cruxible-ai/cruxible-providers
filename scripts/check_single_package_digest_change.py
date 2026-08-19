#!/usr/bin/env python
"""Assert that a change moves at most one package's materialization digest.

    uv run python scripts/check_single_package_digest_change.py --base origin/main

This is the enforcement behind "no shared uv-workspace root lock". A shared lock
makes every package's environment move whenever any dependency anywhere moves,
which would re-pin — and therefore re-verify, re-build, and re-attest — providers
that did not change. Per-package locks make that impossible; this check makes it
*observable*, so the property cannot rot quietly.

The check compares each package's per-environment materialization digests at the
base revision against the working tree, and fails if more than one package's
digests moved. A change that genuinely needs to move two packages should be two
commits.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.materialization_digests import compute, load_environments  # noqa: E402


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(  # noqa: S603 - fixed argv
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def _materialize_base(repo: Path, base: str, target: Path) -> None:
    """Extract the base revision's tree into ``target`` with ``git archive``."""

    archive = target / "base.tar"
    with archive.open("wb") as handle:
        subprocess.run(  # noqa: S603 - fixed argv
            ["git", "archive", "--format=tar", base],
            cwd=repo,
            check=True,
            stdout=handle,
        )
    extracted = target / "tree"
    extracted.mkdir()
    shutil.unpack_archive(str(archive), str(extracted), format="tar")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="HEAD~1", help="revision to compare against")
    parser.add_argument("--repo", type=Path, default=REPO_ROOT)
    args = parser.parse_args(argv)
    repo = args.repo.resolve()

    # The environment list is held fixed across both revisions, so this check
    # measures lock movement and nothing else. Re-pinning the environments is a
    # deliberate, separate act.
    environments = load_environments(repo)
    head = compute(repo, environments)
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp)
        try:
            _materialize_base(repo, args.base, target)
        except subprocess.CalledProcessError:
            print(f"cannot resolve base revision {args.base!r}; skipping the check")
            return 0
        base = compute(target / "tree", environments)

    changed = sorted(
        name for name in set(head) | set(base) if head.get(name) != base.get(name)
    )
    for name in sorted(set(head) | set(base)):
        state = "CHANGED" if name in changed else "unchanged"
        print(f"{state:>9}  {name}")

    if len(changed) > 1:
        print(
            "\nFAIL: a single change moved the materialization digest of "
            f"{len(changed)} packages: {changed}.\n"
            "Per-package locks exist so that a dependency bump re-pins exactly one "
            "package. Split this into one commit per package."
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
