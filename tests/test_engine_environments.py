"""Which engine environments the declared marker environments can pin.

This is the claim ``docs/packaging.md`` makes, asserted against the committed
locks and the committed ``ci/marker-environments.json``. It is here rather than
in a package's own suite because it is a statement about the *repository's*
pins: it discovers every implementation's engine extras from the committed
manifests, then reads every plane package's lock and every declared environment
at once.

The declared floors are glibc 2.28 and macOS 14.0. They admit every engine
closure the launch manifests require, including Docling's tensor stack, so each
environment can be pinned in an accepted artifact. Raising those floors re-pins
every package at once; this test makes the capability gained by that deliberate
global re-key structural rather than a hand-maintained list.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from cruxible_provider_runtime.manifest import load_manifest
from cruxible_provider_runtime.resolution import (
    MarkerEnvironment,
    ResolvedSet,
    load_uv_lock,
    resolve,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

DECLARED_ENVIRONMENTS = [
    MarkerEnvironment.model_validate(entry)
    for entry in json.loads(
        (REPO_ROOT / "ci" / "marker-environments.json").read_text(encoding="utf-8")
    )["environments"]
]

RATIFIED_ENVIRONMENT_PAYLOADS = {
    "linux-cp311": {
        "markers": {
            "implementation_name": "cpython",
            "os_name": "posix",
            "platform_machine": "x86_64",
            "python_full_version": "3.11.9",
            "python_version": "3.11",
            "sys_platform": "linux",
        },
        "tags": [
            "cp311-cp311-manylinux_2_28_x86_64",
            "cp311-abi3-manylinux_2_28_x86_64",
            "py3-none-any",
        ],
    },
    "linux-cp312": {
        "markers": {
            "implementation_name": "cpython",
            "os_name": "posix",
            "platform_machine": "x86_64",
            "python_full_version": "3.12.6",
            "python_version": "3.12",
            "sys_platform": "linux",
        },
        "tags": [
            "cp312-cp312-manylinux_2_28_x86_64",
            "cp312-abi3-manylinux_2_28_x86_64",
            "py3-none-any",
        ],
    },
    "macos-arm-cp312": {
        "markers": {
            "implementation_name": "cpython",
            "os_name": "posix",
            "platform_machine": "arm64",
            "python_full_version": "3.12.6",
            "python_version": "3.12",
            "sys_platform": "darwin",
        },
        "tags": [
            "cp312-cp312-macosx_14_0_arm64",
            "cp312-abi3-macosx_14_0_arm64",
            "py3-none-any",
        ],
    },
}

EXPECTED_ENGINE_ENVIRONMENTS = {
    "doc.to_markdown+docling",
    "ocr.extract+paddleocr",
    "web.fetch+browser",
}
ENGINE_ENVIRONMENTS: list[tuple[str, str, tuple[str, ...]]] = []
for manifest_path in sorted((REPO_ROOT / "packages").glob("*/src/*/manifest.yaml")):
    manifest = load_manifest(manifest_path)
    for implementation in manifest.implementations:
        if implementation.requires_extras:
            subject = f"{implementation.interface_id}+{'+'.join(implementation.requires_extras)}"
            ENGINE_ENVIRONMENTS.append(
                (subject, manifest.distribution.name, implementation.requires_extras)
            )

ENGINE_ENVIRONMENT_PARAMS = [
    pytest.param(package, extras, id=subject) for subject, package, extras in ENGINE_ENVIRONMENTS
]


def _resolve(package: str, extras: tuple[str, ...], environment: MarkerEnvironment) -> ResolvedSet:
    lock = load_uv_lock(REPO_ROOT / "packages" / package / "uv.lock")
    return resolve(lock, package, environment, extras=extras, allow_editable_dev_sources=True)


def test_declared_environments_pin_the_ratified_launch_floors() -> None:
    assert {
        environment.id: environment.digest_payload() for environment in DECLARED_ENVIRONMENTS
    } == RATIFIED_ENVIRONMENT_PAYLOADS


def test_engine_environment_inventory_is_complete() -> None:
    assert {subject for subject, _, _ in ENGINE_ENVIRONMENTS} == (EXPECTED_ENGINE_ENVIRONMENTS)


@pytest.mark.parametrize("environment", DECLARED_ENVIRONMENTS, ids=lambda env: env.id)
@pytest.mark.parametrize(("package", "extras"), ENGINE_ENVIRONMENT_PARAMS)
def test_every_engine_environment_resolves_for_every_declared_environment(
    package: str, extras: tuple[str, ...], environment: MarkerEnvironment
) -> None:
    resolved = _resolve(package, extras, environment)
    assert resolved.distributions


@pytest.mark.parametrize("environment", DECLARED_ENVIRONMENTS, ids=lambda env: env.id)
@pytest.mark.parametrize(
    "package",
    [
        "cruxible-provider-docs",
        "cruxible-provider-noop",
        "cruxible-provider-quant",
        "cruxible-provider-runtime",
        "cruxible-provider-web",
    ],
)
def test_every_base_environment_still_resolves(
    package: str, environment: MarkerEnvironment
) -> None:
    """The half that was already true remains true across the global re-key."""

    lock = load_uv_lock(REPO_ROOT / "packages" / package / "uv.lock")
    resolve(lock, package, environment, allow_editable_dev_sources=True)
