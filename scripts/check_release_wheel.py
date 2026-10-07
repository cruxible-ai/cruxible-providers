#!/usr/bin/env python
"""Refuse a release wheel that an install by name could not reproduce.

    uv run python scripts/check_release_wheel.py PACKAGE_DIR WHEEL [--tag TAG]

A provider installed from an index gets exactly two things from it: the wheel
and the lock embedded in the wheel. So a provider wheel must carry the lock its
package committed, byte for byte, and a tag must name the version it releases.
Packages without a registration descriptor (the runtime, the umbrella) carry no
lock: nothing materializes them on their own.
"""

from __future__ import annotations

import argparse
import sys
import tomllib
from pathlib import Path
from zipfile import ZipFile

LOCK_MEMBER = "extra_metadata/uv.lock"


def check(package_dir: Path, wheel: Path, tag: str | None) -> list[str]:
    project = tomllib.loads((package_dir / "pyproject.toml").read_text())["project"]
    name, version = project["name"], project["version"]
    problems = []
    if tag is not None and tag != f"{name}-v{version}":
        problems.append(f"tag {tag!r} does not name {name} {version}")
    with ZipFile(wheel) as archive:
        members = archive.namelist()
        dist_info = f"{name.replace('-', '_')}-{version}.dist-info/"
        if not any(member.startswith(dist_info) for member in members):
            problems.append(f"{wheel.name} is not {name} {version}")
        provider = any(member.endswith("/registration.json") for member in members)
        embedded = dist_info + LOCK_MEMBER
        if provider:
            if embedded not in members:
                problems.append(f"{wheel.name} does not embed its package lock")
            elif archive.read(embedded) != (package_dir / "uv.lock").read_bytes():
                problems.append(f"{wheel.name} embeds a lock that differs from the committed one")
        elif embedded in members:
            problems.append(f"{wheel.name} is not a provider but embeds a lock")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("package_dir", type=Path)
    parser.add_argument("wheel", type=Path)
    parser.add_argument("--tag")
    arguments = parser.parse_args()
    problems = check(arguments.package_dir, arguments.wheel, arguments.tag)
    for problem in problems:
        print(problem, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
