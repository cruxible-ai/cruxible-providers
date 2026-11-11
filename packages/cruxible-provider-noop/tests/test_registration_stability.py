"""Drift guards on the package's registration and its manifest transcription."""

from __future__ import annotations

import tomllib
from pathlib import Path

import cruxible_provider_noop
from cruxible_provider_noop.captures import NoopCaptures
from cruxible_provider_noop.provider import NoopEcho
from cruxible_provider_runtime.manifest import ENTRYPOINT_GROUP, load_manifest
from cruxible_provider_runtime.registration import load_registration


def test_the_registration_declares_what_each_entrypoint_serves() -> None:
    bundle = load_registration(cruxible_provider_noop.PACKAGE_ROOT)
    assert [item.name for item in bundle.operations] == ["noop.captures", "noop.echo"]
    echo = bundle.operation(cruxible_provider_noop.OPERATION)
    assert echo.effect == "pure"
    assert echo.in_.spelling == "noop.echo.request@1"
    assert echo.out.spelling == "noop.echo.result@1"
    assert set(echo.refusals) == {
        cruxible_provider_noop.CREDENTIAL_MISSING,
        cruxible_provider_noop.PROVIDER_DECLINED,
    }
    assert NoopEcho.operations == (echo.name,)
    assert echo.entrypoint == "cruxible_provider_noop.provider:NoopEcho"
    captures = bundle.operation(cruxible_provider_noop.CAPTURES_OPERATION)
    assert captures.effect == "pure"
    assert captures.in_.spelling == "noop.captures.request@1"
    assert captures.out.spelling == "noop.captures.result@1"
    assert set(captures.refusals) == {"provider_declined"}
    assert NoopCaptures.operations == (captures.name,)
    assert captures.entrypoint == "cruxible_provider_noop.captures:NoopCaptures"


def test_the_package_ships_exactly_the_contracts_its_registration_pins() -> None:
    bundle = load_registration(cruxible_provider_noop.PACKAGE_ROOT)
    named = {
        f"contracts/{ref.name}/v{ref.version}.json"
        for operation in bundle.operations
        for ref in operation.contract_refs()
    }
    assert set(cruxible_provider_noop.contract_members()) == named


def test_the_manifest_version_tracks_the_package_version(manifest_path: Path) -> None:
    manifest = load_manifest(manifest_path)
    assert manifest.distribution.version == cruxible_provider_noop.__version__


def test_the_entry_points_are_the_registered_operations(manifest_path: Path) -> None:
    """The registration and the packaging metadata must name the same objects."""

    pyproject = Path(manifest_path).parents[2] / "pyproject.toml"
    document = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    declared = document["project"]["entry-points"][ENTRYPOINT_GROUP]
    assert declared == load_registration(cruxible_provider_noop.PACKAGE_ROOT).entry_points()
