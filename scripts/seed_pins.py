#!/usr/bin/env python
"""Print the identity pins core's seed bundle records for one provider package.

    uv run python scripts/seed_pins.py --package cruxible-provider-workspace \
        --wheel packages/cruxible-provider-workspace/dist/<wheel>.whl [--json]

For every implementation the package's manifest declares: the interface id and
digest, the entrypoint, and the **implementation digest** over the built
wheel's sha256. For the package: the lock's sha256, and one **materialization
digest** per marker environment in ``ci/marker-environments.json`` (the launch
floors), resolved from the committed lock with the wheel's sha256 as the root
identity.

The wheel is what makes these real. The conformance suites bind against a
synthetic distribution sha256 because the package is not built during a test
run; the seed bundle pins the digest of the artifact that will actually be
fetched, so the wheel is built first (``uv build --wheel``, reproducible under
hatchling) and its hash is fed in here.

Local sources are admitted, and the output says so: every plane package
depends on the runtime by path until the runtime is published, and a pin
computed over an editable edge is a development pin. Re-run this against the
published resolution before an accepted artifact carries the values.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "packages" / "cruxible-provider-runtime" / "src"))

from cruxible_provider_runtime.digests import (  # noqa: E402
    implementation_digest,
    materialization_digest,
)
from cruxible_provider_runtime.manifest import load_manifest, manifest_digest  # noqa: E402
from cruxible_provider_runtime.resolution import (  # noqa: E402
    MarkerEnvironment,
    load_uv_lock,
    resolve,
)


def load_environments(repo: Path) -> list[MarkerEnvironment]:
    document = json.loads((repo / "ci" / "marker-environments.json").read_text(encoding="utf-8"))
    return [MarkerEnvironment.model_validate(entry) for entry in document["environments"]]


def compute(repo: Path, package: str, wheel: Path) -> dict[str, Any]:
    package_dir = repo / "packages" / package
    manifest_path = next(package_dir.glob("src/*/manifest.yaml"))
    manifest = load_manifest(manifest_path)
    if manifest.distribution.name != package:
        raise SystemExit(f"manifest names {manifest.distribution.name!r}, not {package!r}")
    wheel_bytes = wheel.read_bytes()
    distribution_sha256 = "sha256:" + hashlib.sha256(wheel_bytes).hexdigest()
    lock = load_uv_lock(package_dir / "uv.lock")
    environments_path = repo / "ci" / "marker-environments.json"

    implementations = []
    for implementation in manifest.implementations:
        implementations.append(
            {
                "interface_id": implementation.interface_id,
                "interface_digest": implementation.interface_digest,
                "entrypoint": implementation.entrypoint,
                "requires_extras": list(implementation.requires_extras),
                "implementation_digest": implementation_digest(
                    interface_id=implementation.interface_id,
                    interface_digest=implementation.interface_digest,
                    entrypoint=implementation.entrypoint,
                    distribution_sha256=distribution_sha256,
                ),
            }
        )

    materializations = {}
    local_sources: set[str] = set()
    for environment in load_environments(repo):
        for implementation in manifest.implementations:
            resolved = resolve(
                lock,
                package,
                environment,
                extras=implementation.requires_extras,
                allow_editable_dev_sources=True,
            )
            local_sources.update(
                entry.artifact_id for entry in resolved.distributions if entry.is_local_source
            )
            materializations[resolved.pin_key()] = materialization_digest(
                resolved, distribution_sha256=distribution_sha256
            )

    return {
        "package": package,
        "version": manifest.distribution.version,
        "manifest_path": str(manifest_path.relative_to(repo)),
        "manifest_digest": manifest_digest(manifest),
        "distribution": {
            "filename": wheel.name,
            "sha256": distribution_sha256,
            "size": len(wheel_bytes),
        },
        "implementations": implementations,
        "lock_sha256": lock.lock_sha256,
        "marker_environments_sha256": "sha256:"
        + hashlib.sha256(environments_path.read_bytes()).hexdigest(),
        "materialization_digests": dict(sorted(materializations.items())),
        "local_sources_in_resolution": sorted(local_sources),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=REPO_ROOT)
    parser.add_argument("--package", required=True, help="the package directory name")
    parser.add_argument("--wheel", type=Path, required=True, help="the built wheel")
    parser.add_argument("--json", action="store_true", help="emit JSON rather than a table")
    args = parser.parse_args(argv)

    pins = compute(args.repo.resolve(), args.package, args.wheel.resolve())
    if args.json:
        print(json.dumps(pins, indent=2, sort_keys=True))
        return 0
    print(f"{pins['package']} {pins['version']}")
    print(f"  manifest       {pins['manifest_digest']}  ({pins['manifest_path']})")
    print(
        f"  distribution   {pins['distribution']['sha256']}  ({pins['distribution']['filename']})"
    )
    print(f"  lock           {pins['lock_sha256']}")
    print(f"  environments   {pins['marker_environments_sha256']}  (ci/marker-environments.json)")
    for implementation in pins["implementations"]:
        print(f"  {implementation['interface_id']}")
        print(f"    interface      {implementation['interface_digest']}")
        print(f"    entrypoint     {implementation['entrypoint']}")
        print(f"    implementation {implementation['implementation_digest']}")
    for key, digest in pins["materialization_digests"].items():
        print(f"  materialization {key:<22} {digest}")
    if pins["local_sources_in_resolution"]:
        print(
            "  NOTE: development pins -- the resolution admits local sources: "
            + ", ".join(pins["local_sources_in_resolution"])
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
