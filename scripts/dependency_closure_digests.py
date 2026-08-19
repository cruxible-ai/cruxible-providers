#!/usr/bin/env python
"""Compute every package's dependency-closure digests from its own committed lock.

    uv run python scripts/dependency_closure_digests.py [--repo DIR] [--json]

Each package is resolved independently, from its own ``uv.lock``, for every
marker environment in ``ci/marker-environments.json``. There is no shared root
lock in this computation on purpose: the per-package lock is the identity
source, and the one-package-one-digest-change gate depends on that being true.

**Closure digest, not materialization digest.** A materialization digest also
pins the root distribution's sha256, which does not exist until the package is
built and which moves on every release by construction. The closure digest
covers the root's name, the marker environment, and the resolved dependency set
— exactly the part a *dependency* bump moves, and therefore the right instrument
for a gate that is asking whether a dependency bump escaped its package.

Local sources are admitted here, and only here: a monorepo's packages depend on
each other by path before they are published, and the gate has to see those
edges. Production binds refuse them.
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "packages" / "cruxible-provider-runtime" / "src"))

from cruxible_provider_runtime.digests import dependency_closure_digest  # noqa: E402
from cruxible_provider_runtime.resolution import (  # noqa: E402
    MarkerEnvironment,
    load_uv_lock,
    resolve,
)


def load_environments(repo: Path) -> list[MarkerEnvironment]:
    document = json.loads((repo / "ci" / "marker-environments.json").read_text(encoding="utf-8"))
    return [MarkerEnvironment.model_validate(entry) for entry in document["environments"]]


def package_dirs(repo: Path) -> list[Path]:
    """Every real package: a directory with both a pyproject and its own lock."""

    return sorted(
        path
        for path in (repo / "packages").iterdir()
        if (path / "pyproject.toml").is_file() and (path / "uv.lock").is_file()
    )


def distribution_name(package_dir: Path) -> str:
    document = tomllib.loads((package_dir / "pyproject.toml").read_text(encoding="utf-8"))
    return str(document["project"]["name"])


def compute(
    repo: Path, environments: list[MarkerEnvironment] | None = None
) -> dict[str, dict[str, str]]:
    """Digest every package in ``repo``.

    ``environments`` is injectable so that a comparison across revisions can hold
    the environment list fixed: otherwise a change to the environment file would
    read as "every package moved", hiding whatever else moved with it.
    """

    environments = environments if environments is not None else load_environments(repo)
    digests: dict[str, dict[str, str]] = {}
    for package_dir in package_dirs(repo):
        name = distribution_name(package_dir)
        lock = load_uv_lock(package_dir / "uv.lock")
        digests[name] = {
            env.id: dependency_closure_digest(
                resolve(lock, name, env, allow_editable_dev_sources=True)
            )
            for env in environments
        }
    return digests


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=REPO_ROOT)
    parser.add_argument("--json", action="store_true", help="emit JSON rather than a table")
    args = parser.parse_args(argv)

    digests = compute(args.repo.resolve())
    if args.json:
        print(json.dumps(digests, indent=2, sort_keys=True))
        return 0
    for package, per_env in sorted(digests.items()):
        print(package)
        for env_id, digest in sorted(per_env.items()):
            print(f"  {env_id:<20} {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
