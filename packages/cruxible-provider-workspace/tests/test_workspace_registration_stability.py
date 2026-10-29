"""Drift guards on the operation the package registers, and its manifest transcription."""

from __future__ import annotations

import tomllib
from pathlib import Path

import cruxible_provider_workspace
from cruxible_provider_runtime.manifest import ENTRYPOINT_GROUP, load_manifest
from cruxible_provider_runtime.registration import load_registration
from cruxible_provider_workspace import file as adapter

from .conftest import ENTRYPOINT, OPERATION, PACKAGE_DIR


def test_the_registration_declares_the_operation_core_seeds() -> None:
    """The same signature and refusals core's built-in port declares, by name."""

    (operation,) = load_registration(cruxible_provider_workspace.PACKAGE_ROOT).operations
    assert operation.name == OPERATION
    assert operation.effect == "pure"
    assert operation.in_.spelling == "workspace.file.request@1"
    assert operation.out.spelling == "workspace.file.result@1"
    assert {code: item.contract.spelling for code, item in operation.refusals.items()} == {
        adapter.INVALID_PARAMETER: "workspace.file.refusal.invalid_parameter@1",
        adapter.MISMATCHED_LENGTHS: "workspace.file.refusal.mismatched_lengths@1",
        adapter.PROVIDER_DECLINED: "workspace.file.refusal.provider_declined@1",
    }
    assert all(item.absorbable for item in operation.refusals.values())
    assert operation.entrypoint == ENTRYPOINT
    assert adapter.WorkspaceFile.operations == (OPERATION,)


def test_the_manifest_version_tracks_the_package_version(manifest_path: Path) -> None:
    manifest = load_manifest(manifest_path)
    assert manifest.distribution.version == cruxible_provider_workspace.__version__
    pyproject = tomllib.loads((PACKAGE_DIR / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["version"] == cruxible_provider_workspace.__version__


def test_the_entry_points_are_the_registered_operations() -> None:
    pyproject = tomllib.loads((PACKAGE_DIR / "pyproject.toml").read_text(encoding="utf-8"))
    declared = pyproject["project"]["entry-points"][ENTRYPOINT_GROUP]
    assert declared == load_registration(cruxible_provider_workspace.PACKAGE_ROOT).entry_points()


def test_the_manifest_spells_the_pure_effect(manifest_path: Path) -> None:
    """Zero endpoints and deterministic: pure, the way a manifest can say it."""

    implementation = load_manifest(manifest_path).implementation(ENTRYPOINT)
    assert implementation.declared_endpoints == ()
    assert implementation.requires_extras == ()
    assert implementation.deterministic is True
    assert implementation.backends == ("local_env", "container")


def test_the_size_classes_sit_exactly_on_their_ceilings() -> None:
    assert adapter.byte_size_class(0) == "tiny"
    assert adapter.byte_size_class(4_096) == "tiny"
    assert adapter.byte_size_class(4_097) == "small"
    assert adapter.byte_size_class(65_536) == "small"
    assert adapter.byte_size_class(65_537) == "medium"
    assert adapter.byte_size_class(1_048_576) == "medium"
    assert adapter.byte_size_class(1_048_577) == "large"


def test_content_kind_is_measured_not_declared() -> None:
    assert adapter.content_kind_class(b"") == "text"
    assert adapter.content_kind_class("héllo".encode()) == "text"
    assert adapter.content_kind_class(b"text with a \x00 in it") == "binary"
    assert adapter.content_kind_class(b"\xff\xfe") == "binary"
    assert adapter.content_kind_class("héllo".encode("latin-1")) == "binary"
