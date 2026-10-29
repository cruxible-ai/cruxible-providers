"""Drift guards on the package's registration and its manifest transcription."""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

import cruxible_provider_docs
from cruxible_provider_docs import DoclingToMarkdown, PaddleOcrExtract
from cruxible_provider_runtime.manifest import ENTRYPOINT_GROUP, ProviderManifest
from cruxible_provider_runtime.registration import load_registration

ENTRYPOINTS = {
    "doc.to_markdown": "cruxible_provider_docs.to_markdown:DoclingToMarkdown",
    "ocr.extract": "cruxible_provider_docs.ocr:PaddleOcrExtract",
}


def _bundle():  # type: ignore[no-untyped-def]
    return load_registration(cruxible_provider_docs.PACKAGE_ROOT)


def test_each_entrypoint_serves_the_operation_the_registration_names() -> None:
    operations = {item.name: item for item in _bundle().operations}
    assert {name: item.entrypoint for name, item in operations.items()} == ENTRYPOINTS
    assert DoclingToMarkdown.operations == ("doc.to_markdown",)
    assert PaddleOcrExtract.operations == ("ocr.extract",)
    for name, item in operations.items():
        assert item.effect == "pure"
        assert item.in_.spelling == f"{name}.request@1"
        assert item.out.spelling == f"{name}.result@1"
        assert set(item.refusals) == {"engine_unavailable", "provider_declined"}
        assert item.refusals["provider_declined"].absorbable is True
        assert item.refusals["engine_unavailable"].absorbable is False
        for code, refusal in item.refusals.items():
            assert refusal.contract.spelling == f"{name}.refusal.{code}@1"


def test_the_declared_refusals_cover_every_code_the_code_can_return() -> None:
    """Every operation-refusal code spelled in the source is declared, and nothing else."""

    source = Path(cruxible_provider_docs.__file__).parent
    codes: set[str] = set()
    for path in source.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Assign)
                and any(
                    isinstance(t, ast.Name) and t.id in {"PROVIDER_DECLINED", "ENGINE_UNAVAILABLE"}
                    for t in node.targets
                )
                and isinstance(node.value, ast.Constant)
            ):
                codes.add(str(node.value.value))
    assert codes == {"engine_unavailable", "provider_declined"}
    for item in _bundle().operations:
        assert codes == set(item.refusals)


def test_the_manifest_version_tracks_the_package_version(manifest: ProviderManifest) -> None:
    assert manifest.distribution.version == cruxible_provider_docs.__version__


def test_the_entry_points_are_the_registered_operations(manifest_path: Path) -> None:
    pyproject = manifest_path.parents[2] / "pyproject.toml"
    declared = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["entry-points"][
        ENTRYPOINT_GROUP
    ]
    assert declared == _bundle().entry_points()


def test_the_declared_extras_exist_in_the_distribution(
    manifest: ProviderManifest, manifest_path: Path
) -> None:
    pyproject = tomllib.loads(
        (manifest_path.parents[2] / "pyproject.toml").read_text(encoding="utf-8")
    )
    available = set(pyproject["project"].get("optional-dependencies", {}))
    for implementation in manifest.implementations:
        assert implementation.requires_extras, (
            f"{implementation.entrypoint} carries an engine and must declare its extra"
        )
        assert set(implementation.requires_extras) <= available


def test_the_two_implementations_require_different_extras(manifest: ProviderManifest) -> None:
    """The property the whole extras mechanism exists to support."""

    markdown = manifest.implementation(ENTRYPOINTS["doc.to_markdown"]).requires_extras
    ocr = manifest.implementation(ENTRYPOINTS["ocr.extract"]).requires_extras
    assert set(markdown).isdisjoint(ocr)
