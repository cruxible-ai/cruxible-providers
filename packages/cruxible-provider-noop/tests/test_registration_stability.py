"""Drift guards on the package's registration and its manifest transcription."""

from __future__ import annotations

import tomllib
from pathlib import Path

import cruxible_provider_noop
from cruxible_provider_noop.provider import NoopEcho
from cruxible_provider_runtime.manifest import ENTRYPOINT_GROUP, load_manifest
from cruxible_provider_runtime.registration import load_registration


def test_the_registration_declares_the_one_operation_the_entrypoint_serves() -> None:
    bundle = load_registration(cruxible_provider_noop.PACKAGE_ROOT)
    (operation,) = bundle.operations
    assert operation.name == cruxible_provider_noop.OPERATION
    assert operation.effect == "pure"
    assert operation.in_.spelling == "noop.echo.request@1"
    assert operation.out.spelling == "noop.echo.result@1"
    assert set(operation.refusals) == {
        cruxible_provider_noop.CREDENTIAL_MISSING,
        cruxible_provider_noop.PROVIDER_DECLINED,
    }
    assert NoopEcho.operations == (operation.name,)
    assert operation.entrypoint == "cruxible_provider_noop.provider:NoopEcho"


def test_the_manifest_version_tracks_the_package_version(manifest_path: Path) -> None:
    manifest = load_manifest(manifest_path)
    assert manifest.distribution.version == cruxible_provider_noop.__version__


def test_the_entry_points_are_the_registered_operations(manifest_path: Path) -> None:
    """The registration and the packaging metadata must name the same objects."""

    pyproject = Path(manifest_path).parents[2] / "pyproject.toml"
    document = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    declared = document["project"]["entry-points"][ENTRYPOINT_GROUP]
    assert declared == load_registration(cruxible_provider_noop.PACKAGE_ROOT).entry_points()
