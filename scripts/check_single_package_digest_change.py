#!/usr/bin/env python
"""Assert that a change moves at most one package's materialization digest.

    uv run python scripts/check_single_package_digest_change.py --base origin/main

This is the enforcement behind "no shared uv-workspace root lock". A shared lock
makes every package's environment move whenever any dependency anywhere moves,
which would re-pin — and therefore re-verify, re-build, and re-attest — providers
that did not change. Per-package locks make that impossible; this check makes it
*observable*, so the property cannot rot quietly.

The check compares each package's per-environment dependency-closure digests at
the base revision against the working tree, and fails if more than one package's
digests moved. A change that genuinely needs to move two packages should be two
commits.

**Moved**, not "differs from the base". A package that did not exist at the base
has not moved: nothing was pinned to it, no artifact recorded its environment,
and no track record can be split by it appearing. Counting additions would make
the gate fail every batch that lands two packages — which is a statement about
the batch's size, not about a bump escaping its package — so additions and
removals are reported and not counted, and the count is over packages present in
both revisions whose closure changed.

An unresolvable base is a **failure**, not a pass. A gate that waves changes
through whenever it cannot do its job is a gate that reports green in exactly
the situation nobody checked — a shallow clone, a renamed branch, a rewritten
base. ``--allow-missing-base`` exists for the one legitimate case, the initial
commit, and has to be asked for.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.dependency_closure_digests import compute, load_environments  # noqa: E402


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
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp)
        try:
            _materialize_base(repo, args.base, target)
        except subprocess.CalledProcessError:
            if args.allow_missing_base:
                print(f"cannot resolve base revision {args.base!r}; skipping as permitted")
                return 0
            print(
                f"FAIL: cannot resolve base revision {args.base!r}, so the digest-scope "
                "gate could not run. Pass --allow-missing-base only if there is genuinely "
                "no base to compare against."
            )
            return 1
        base = compute(target / "tree", environments)

    added = sorted(set(head) - set(base))
    removed = sorted(set(base) - set(head))
    changed = sorted(name for name in set(head) & set(base) if head[name] != base[name])
    for name in sorted(set(head) | set(base)):
        if name in added:
            state = "added"
        elif name in removed:
            state = "removed"
        elif name in changed:
            state = "CHANGED"
        else:
            state = "unchanged"
        print(f"{state:>9}  {name}")

    if len(changed) > 1:
        print(
            "\nFAIL: a single change moved the dependency closure of "
            f"{len(changed)} existing packages: {changed}.\n"
            "Per-package locks exist so that a dependency bump re-pins exactly one "
            "package. Split this into one commit per package."
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
