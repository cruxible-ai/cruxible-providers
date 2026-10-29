"""Package registration (schema 2) is complete without the monorepo or core.

Every package ships one ``registration.json`` declaring its typed operations
and naming its manifest by path and digest. Reading it imports no provider
code; every cross-link between the two declarations is checked.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any
from zipfile import ZipFile

import pytest
from cruxible_provider_runtime.canonical import canonical_json
from cruxible_provider_runtime.registration import load_registration
from cruxible_provider_runtime.registry import stub_contract_digest
from cruxible_provider_runtime.wheels import wheel_registration

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = tuple(sorted(ROOT.glob("packages/*/src/*/registration.json")))


@pytest.mark.parametrize("descriptor", PACKAGES, ids=lambda p: p.parent.name)
def test_package_metadata_is_self_contained(descriptor: Path, tmp_path: Path) -> None:
    package = tmp_path / descriptor.parent.name
    shutil.copytree(descriptor.parent, package)
    bundle = load_registration(package)
    assert bundle.operations
    for operation in bundle.operations:
        assert operation.entrypoint in {item.entrypoint for item in bundle.manifest.implementations}
        assert not operation.pinned, "a package names Contracts by name@N; the install pins them"
    assert bundle.manifest.supported_protocol_majors == (2,)


@pytest.mark.parametrize("descriptor", PACKAGES, ids=lambda p: p.parent.name)
def test_every_entrypoint_declares_exactly_the_operations_registered_for_it(
    descriptor: Path,
) -> None:
    """The child dispatches by entrypoint and checks the operation against this."""

    bundle = load_registration(descriptor.parent)
    for implementation in bundle.manifest.implementations:
        module, member = implementation.entrypoint.split(":")
        declared = getattr(importlib.import_module(module), member).operations
        registered = [
            item.name for item in bundle.operations if item.entrypoint == implementation.entrypoint
        ]
        assert sorted(declared) == registered


@pytest.mark.parametrize("descriptor", PACKAGES, ids=lambda p: p.parent.name)
def test_inspection_does_not_import_provider_code(
    descriptor: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = importlib.import_module

    def guarded(name: str, package: str | None = None) -> Any:
        if name.startswith("cruxible_provider_") and not name.startswith(
            "cruxible_provider_runtime"
        ):
            raise AssertionError("inspection executed a provider import")
        return original(name, package)

    monkeypatch.setattr(importlib, "import_module", guarded)
    assert load_registration(descriptor.parent).manifest


def _document(root: Path) -> dict[str, Any]:
    document: dict[str, Any] = json.loads((root / "registration.json").read_text())
    return document


def _rewrite(root: Path, document: dict[str, Any]) -> None:
    (root / "registration.json").write_text(json.dumps(document))


def _refresh_manifest(root: Path, document: dict[str, Any]) -> None:
    ref = document["manifest"]
    ref["digest"] = "sha256:" + hashlib.sha256((root / ref["path"]).read_bytes()).hexdigest()


@pytest.fixture()
def package_copy(tmp_path: Path) -> Path:
    source = next(p.parent for p in PACKAGES if p.parent.name == "cruxible_provider_noop")
    target = tmp_path / "package"
    shutil.copytree(source, target)
    return target


@pytest.fixture()
def docs_copy(tmp_path: Path) -> Path:
    """A package with two entrypoints and a runtime requirement on each."""

    source = next(p.parent for p in PACKAGES if p.parent.name == "cruxible_provider_docs")
    target = tmp_path / "docs"
    shutil.copytree(source, target)
    return target


def test_a_tampered_manifest_is_refused(package_copy: Path) -> None:
    path = package_copy / "manifest.yaml"
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="resource digest mismatch"):
        load_registration(package_copy)


@pytest.mark.parametrize(
    "path", ["../outside.yaml", "/tmp/outside.yaml", "a/../../x", "a\\x", "./manifest.yaml"]
)
def test_the_manifest_path_cannot_escape(package_copy: Path, path: str) -> None:
    document = _document(package_copy)
    document["manifest"]["path"] = path
    _rewrite(package_copy, document)
    with pytest.raises(ValueError, match="package-relative"):
        load_registration(package_copy)


def test_a_symlink_escape_is_refused(package_copy: Path, tmp_path: Path) -> None:
    path = package_copy / "manifest.yaml"
    outside = tmp_path / "outside.yaml"
    path.rename(outside)
    path.symlink_to(outside)
    with pytest.raises(ValueError, match="escapes package"):
        load_registration(package_copy)


@pytest.mark.parametrize("version", [1, 3, None])
def test_only_schema_2_is_read(package_copy: Path, version: int | None) -> None:
    """A schema-1 registration exported interfaces; it is refused as such."""

    document = _document(package_copy)
    if version is None:
        del document["schema_version"]
    else:
        document["schema_version"] = version
    _rewrite(package_copy, document)
    with pytest.raises(ValueError, match="is not read"):
        load_registration(package_copy)


def test_a_schema_1_registration_is_refused_whole(package_copy: Path) -> None:
    document = {
        "schema_version": 1,
        "manifest": _document(package_copy)["manifest"],
        "interfaces": [{"interface_id": "noop.echo"}],
        "runtime_requirements": [],
    }
    _rewrite(package_copy, document)
    with pytest.raises(ValueError, match="schema 1"):
        load_registration(package_copy)


def test_duplicate_operations_and_unknown_fields_refuse(package_copy: Path) -> None:
    document = _document(package_copy)
    document["operations"] *= 2
    _rewrite(package_copy, document)
    with pytest.raises(ValueError, match="sorted by name and each named once"):
        load_registration(package_copy)
    document["operations"] = document["operations"][:1]
    document["trust_me"] = True
    _rewrite(package_copy, document)
    with pytest.raises(ValueError, match="trust_me"):
        load_registration(package_copy)


def test_operations_must_be_sorted(docs_copy: Path) -> None:
    document = _document(docs_copy)
    document["operations"].reverse()
    _rewrite(docs_copy, document)
    with pytest.raises(ValueError, match="sorted by name"):
        load_registration(docs_copy)


def test_an_operation_naming_an_undeclared_entrypoint_refuses(package_copy: Path) -> None:
    document = _document(package_copy)
    document["operations"][0]["entrypoint"] = "cruxible_provider_noop.provider:Elsewhere"
    _rewrite(package_copy, document)
    with pytest.raises(ValueError, match="which the manifest does not declare"):
        load_registration(package_copy)


def test_an_implementation_serving_no_operation_refuses(docs_copy: Path) -> None:
    document = _document(docs_copy)
    document["operations"] = document["operations"][:1]
    document["runtime_requirements"] = [
        item
        for item in document["runtime_requirements"]
        if item["entrypoint"] == document["operations"][0]["entrypoint"]
    ]
    _rewrite(docs_copy, document)
    with pytest.raises(ValueError, match="serves at least one operation"):
        load_registration(docs_copy)


def test_one_entrypoint_may_serve_two_operations(package_copy: Path) -> None:
    document = _document(package_copy)
    second = {**document["operations"][0], "name": "noop.shout"}
    document["operations"].append(second)
    _rewrite(package_copy, document)
    bundle = load_registration(package_copy)
    assert bundle.entry_points() == {
        "noop.echo": "cruxible_provider_noop.provider:NoopEcho",
        "noop.shout": "cruxible_provider_noop.provider:NoopEcho",
    }


@pytest.mark.parametrize("code", ["budget_wall_clock", "undeclared_operation", "secret_leak"])
def test_an_operation_may_not_declare_a_runtime_refusal_code(package_copy: Path, code: str) -> None:
    document = _document(package_copy)
    document["operations"][0]["refusals"] = {code: {"contract": "noop.echo.refusal.x@1"}}
    _rewrite(package_copy, document)
    with pytest.raises(ValueError, match="reserved by the runtime"):
        load_registration(package_copy)


def test_a_runtime_requirement_belongs_to_a_declared_implementation(docs_copy: Path) -> None:
    document = _document(docs_copy)
    document["runtime_requirements"][0]["entrypoint"] = "cruxible_provider_docs.x:Absent"
    _rewrite(docs_copy, document)
    with pytest.raises(ValueError, match="undeclared implementation"):
        load_registration(docs_copy)
    document["runtime_requirements"][0]["entrypoint"] = document["operations"][1]["entrypoint"]
    _rewrite(docs_copy, document)
    with pytest.raises(ValueError, match="undeclared extra"):
        load_registration(docs_copy)


def test_a_runtime_requirement_is_keyed_by_entrypoint_not_interface(docs_copy: Path) -> None:
    document = _document(docs_copy)
    requirement = document["runtime_requirements"][0]
    requirement["interface_id"] = requirement.pop("entrypoint")
    _rewrite(docs_copy, document)
    with pytest.raises(ValueError, match="interface_id"):
        load_registration(docs_copy)


def test_a_manifest_edit_needs_the_registration_digest_refreshed(package_copy: Path) -> None:
    path = package_copy / "manifest.yaml"
    path.write_text(path.read_text().replace("deterministic: true", "deterministic: false"))
    with pytest.raises(ValueError, match="resource digest mismatch"):
        load_registration(package_copy)
    document = _document(package_copy)
    _refresh_manifest(package_copy, document)
    _rewrite(package_copy, document)
    assert load_registration(package_copy).manifest.implementations[0].deterministic is False


def test_built_wheels_are_discoverable_without_imports() -> None:
    directory = os.environ.get("CRUXIBLE_PROVIDER_WHEEL_DIR")
    if not directory:
        pytest.skip("set CRUXIBLE_PROVIDER_WHEEL_DIR to verify built distributions")
    wheels = list(Path(directory).glob("*.whl"))
    assert wheels
    seen = set()
    for wheel in wheels:
        with ZipFile(wheel) as archive:
            if not any(name.endswith("/registration.json") for name in archive.namelist()):
                continue
        with wheel_registration(wheel) as bundle:
            seen.add(bundle.manifest.distribution.name)
    assert seen == {load_registration(p.parent).manifest.distribution.name for p in PACKAGES}


def test_exported_packages_lower_through_core_without_provider_imports() -> None:
    """Core's own schema-2 reader, pinning and build lowering, in core's interpreter.

    Each package directory is read with ``read_package_registration``; every
    Contract it names is pinned to the stub digest this repository uses; the
    build is lowered and round-trips through core's Provider law; and core's
    signature digest of every pinned operation equals this runtime's.
    """

    core_python = os.environ.get("CRUXIBLE_CORE_PYTHON")
    if not core_python:
        pytest.skip("set CRUXIBLE_CORE_PYTHON for package-to-Core registration integration")
    cases = []
    for descriptor in PACKAGES:
        bundle = load_registration(descriptor.parent)
        cases.append(
            {
                "root": str(descriptor.parent),
                "digests": {
                    ref.spelling: stub_contract_digest(ref)
                    for operation in bundle.operations
                    for ref in operation.contract_refs()
                },
                "signatures": {
                    operation.name: operation.model_copy(
                        update={
                            "in_": operation.in_.model_copy(
                                update={"digest": stub_contract_digest(operation.in_)}
                            ),
                            "out": operation.out.model_copy(
                                update={"digest": stub_contract_digest(operation.out)}
                            ),
                        }
                    ).signature_digest()
                    for operation in bundle.operations
                },
            }
        )
    code = """
import json, sys
from pathlib import Path
from cruxible_client.contracts.provider_operations import operation_signature_digest
from cruxible_client.contracts.providers import (
    ProviderLocalDistributionPin, ProviderLocalEnvBackendPin, parse_provider, provider_digest,
    provider_path, render_provider,
)
from cruxible_core.providers.package_metadata import read_package_registration
count = 0
for case in json.load(sys.stdin):
    document = read_package_registration(Path(case["root"]))
    pinned = document.pinned_operations(lambda ref: case["digests"].get(ref.spelling))
    for operation in pinned:
        assert operation_signature_digest(operation.signature()) == case["signatures"][operation.name]
        count += 1
    distribution = document.manifest.distribution
    provider = document.provider_definition(
        distribution=ProviderLocalDistributionPin(
            name=distribution.name, version=distribution.version,
            filename="fixture.whl", sha256="sha256:" + "a" * 64),
        local_env=ProviderLocalEnvBackendPin(
            lock_sha256="sha256:" + "b" * 64,
            materialization_digests={"linux-cp312": "sha256:" + "c" * 64}),
        control_domain="operator",
        operations=pinned,
    )
    parsed = parse_provider(render_provider(provider), path=provider_path(provider.identity.name))
    assert provider_digest(parsed) == provider_digest(provider)
assert count == 13, count
assert not any(name.startswith("cruxible_provider_") for name in sys.modules)
"""
    result = subprocess.run(
        [core_python, "-c", code], input=canonical_json(cases), capture_output=True
    )
    assert result.returncode == 0, result.stderr.decode()
