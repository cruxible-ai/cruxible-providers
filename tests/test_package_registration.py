"""Package metadata is complete without the monorepo or a Core provider catalog."""

from __future__ import annotations

import base64
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
from cruxible_provider_runtime.canonical import canonical_json, domain_digest
from cruxible_provider_runtime.egress import EgressRecorder
from cruxible_provider_runtime.errors import RefusalError
from cruxible_provider_runtime.protocol import Budgets
from cruxible_provider_runtime.provider_api import ProviderRunContext
from cruxible_provider_runtime.registration import (
    INTERFACE_DOMAIN,
    load_registration,
)
from cruxible_provider_runtime.wheels import wheel_registration
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = tuple(sorted(ROOT.glob("packages/*/src/*/registration.json")))


def _check_nested_schema(definition: dict[str, Any], direction: str, payload: Any) -> None:
    schema = definition["contracts"][direction]
    if not isinstance(schema, dict):
        return
    for name, field in schema["fields"].items():
        if "json_schema" in field:
            Draft202012Validator.check_schema(field["json_schema"])
            if name in payload:
                Draft202012Validator(field["json_schema"]).validate(payload[name])


@pytest.mark.parametrize("descriptor", PACKAGES, ids=lambda p: p.parent.name)
def test_package_metadata_is_self_contained(descriptor: Path, tmp_path: Path) -> None:
    package = tmp_path / descriptor.parent.name
    shutil.copytree(descriptor.parent, package)
    bundle = load_registration(package)
    assert bundle.definitions
    for item in bundle.descriptor.interfaces:
        assert bundle.definitions[item.interface_id]["contracts"]["input"]["allow_extra"] is False
        module, member = item.classifier.entrypoint.split(":")
        classifier = getattr(importlib.import_module(module), member)
        for fixture in bundle.fixtures[item.interface_id]:
            _check_nested_schema(
                bundle.definitions[item.interface_id], "input", fixture.canonical_input
            )
            measured = classifier(fixture.canonical_input)
            assert measured is not None
            assert (
                bundle.vocabularies[item.interface_id].bucket_id(measured)
                == fixture.measured_bucket_id
            )
        for previous in item.predecessors:
            definition = json.loads(previous.definition.read(package))
            assert domain_digest(INTERFACE_DOMAIN, definition) == previous.interface_digest
            assert definition["version"] < bundle.definitions[item.interface_id]["version"]


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


def _rewrite_descriptor(root: Path, document: dict[str, Any]) -> None:
    (root / "registration.json").write_text(json.dumps(document))


def _refresh_ref(root: Path, ref: dict[str, str]) -> None:
    ref["digest"] = "sha256:" + hashlib.sha256((root / ref["path"]).read_bytes()).hexdigest()


@pytest.fixture()
def package_copy(tmp_path: Path) -> Path:
    source = next(p.parent for p in PACKAGES if p.parent.name == "cruxible_provider_noop")
    target = tmp_path / "package"
    shutil.copytree(source, target)
    return target


@pytest.mark.parametrize("component", ["definition", "vocabulary", "fixtures", "classifier"])
def test_tampered_resource_is_refused(package_copy: Path, component: str) -> None:
    doc = json.loads((package_copy / "registration.json").read_text())
    ref = doc["interfaces"][0][component]
    if component == "classifier":
        ref = ref["source"]
    path = package_copy / ref["path"]
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="resource digest mismatch"):
        load_registration(package_copy)


@pytest.mark.parametrize(
    "path",
    ["../outside.json", "/tmp/outside.json", "contracts/../../x", "contracts\\x", "./contracts/x"],
)
def test_resource_path_cannot_escape(package_copy: Path, path: str) -> None:
    doc = json.loads((package_copy / "registration.json").read_text())
    doc["interfaces"][0]["definition"]["path"] = path
    _rewrite_descriptor(package_copy, doc)
    with pytest.raises(ValueError, match="package-relative"):
        load_registration(package_copy)


def test_symlink_escape_is_refused(package_copy: Path, tmp_path: Path) -> None:
    doc = json.loads((package_copy / "registration.json").read_text())
    path = package_copy / doc["interfaces"][0]["definition"]["path"]
    outside = tmp_path / "outside.json"
    path.rename(outside)
    path.symlink_to(outside)
    with pytest.raises(ValueError, match="escapes package"):
        load_registration(package_copy)


def test_duplicate_exports_and_unknown_fields_refuse(package_copy: Path) -> None:
    doc = json.loads((package_copy / "registration.json").read_text())
    doc["interfaces"] *= 2
    _rewrite_descriptor(package_copy, doc)
    with pytest.raises(ValueError, match="duplicate"):
        load_registration(package_copy)
    doc["interfaces"] = doc["interfaces"][:1]
    doc["trust_me"] = True
    _rewrite_descriptor(package_copy, doc)
    with pytest.raises(ValueError):
        load_registration(package_copy)


def test_uncovered_selector_refuses_even_with_valid_resource_hash(package_copy: Path) -> None:
    doc = json.loads((package_copy / "registration.json").read_text())
    ref = doc["interfaces"][0]["fixtures"]
    rows = json.loads((package_copy / ref["path"]).read_text())
    (package_copy / ref["path"]).write_text(json.dumps(rows[:1]))
    _refresh_ref(package_copy, ref)
    _rewrite_descriptor(package_copy, doc)
    with pytest.raises(ValueError, match="cover declared selector"):
        load_registration(package_copy)


def test_wrong_interface_and_predecessor_refuse(package_copy: Path) -> None:
    doc = json.loads((package_copy / "registration.json").read_text())
    doc["interfaces"][0]["predecessors"][0]["interface_digest"] = "sha256:" + "0" * 64
    _rewrite_descriptor(package_copy, doc)
    with pytest.raises(ValueError, match="predecessor"):
        load_registration(package_copy)


def test_fixture_measurement_cannot_be_a_wildcard(package_copy: Path) -> None:
    doc = json.loads((package_copy / "registration.json").read_text())
    ref = doc["interfaces"][0]["fixtures"]
    path = package_copy / ref["path"]
    rows = json.loads(path.read_text())
    dimension = rows[0]["measured_bucket_id"].split("=", 1)[0]
    rows[0]["measured_bucket_id"] = dimension + "=*"
    path.write_text(json.dumps(rows))
    _refresh_ref(package_copy, ref)
    _rewrite_descriptor(package_copy, doc)
    with pytest.raises(RefusalError, match="not registered"):
        load_registration(package_copy)


def test_non_object_definition_refuses(package_copy: Path) -> None:
    doc = json.loads((package_copy / "registration.json").read_text())
    ref = doc["interfaces"][0]["definition"]
    (package_copy / ref["path"]).write_text("[]")
    _refresh_ref(package_copy, ref)
    _rewrite_descriptor(package_copy, doc)
    with pytest.raises(ValueError, match="identity"):
        load_registration(package_copy)


def test_built_wheels_are_discoverable_without_imports(tmp_path: Path) -> None:
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
    core_python = os.environ.get("CRUXIBLE_CORE_PYTHON")
    if not core_python:
        pytest.skip("set CRUXIBLE_CORE_PYTHON for package-to-Core registration integration")
    documents = [load_registration(path.parent).export_document() for path in PACKAGES]
    code = """
import json, sys
from cruxible_core.providers.package_registration import PackageRegistrationDocumentV1
from cruxible_client.contracts.providers import ProviderLocalDistributionPinV1, ProviderLocalEnvBackendPinV1
from cruxible_client.contracts.provider_interfaces import evaluate_provider_interface_law, provider_interface_path
from cruxible_client.contracts.providers import provider_digest, render_provider, parse_provider, provider_path
count = 0
for document in json.load(sys.stdin):
    package = PackageRegistrationDocumentV1.model_validate(document)
    interfaces = package.interface_registrations()
    for interface in interfaces:
        verdict = evaluate_provider_interface_law(interface, path=provider_interface_path(interface.interface_id),
                                                  predecessor=None, conformance_fixtures={})
        assert verdict.verdict == 'accepted', verdict
        count += 1
    distribution = package.manifest.distribution
    provider = package.provider_definition(
        distribution=ProviderLocalDistributionPinV1(name=distribution.name, version=distribution.version,
            filename='fixture.whl', sha256='sha256:'+'a'*64),
        local_env=ProviderLocalEnvBackendPinV1(lock_sha256='sha256:'+'b'*64,
                                             materialization_digests={'linux-cp312':'sha256:'+'c'*64}),
        control_domain='operator', interfaces=interfaces)
    assert provider_digest(parse_provider(render_provider(provider), path=provider_path(provider.identity.name))) == provider_digest(provider)
assert count == 13, count
assert not any(name.startswith('cruxible_provider_') for name in sys.modules)
"""
    result = subprocess.run(
        [core_python, "-c", code], input=canonical_json(documents), capture_output=True
    )
    assert result.returncode == 0, result.stderr.decode()


def test_fixture_inputs_and_real_outputs_pass_core_contracts() -> None:
    """Exercise the real provider engines, then validate in Core's own interpreter.

    The explicit environment setting avoids developer paths and a production
    dependency on Core. This is a cross-repository integration check.
    """
    core_python = os.environ.get("CRUXIBLE_CORE_PYTHON")
    if not core_python:
        pytest.skip("set CRUXIBLE_CORE_PYTHON to validate against the Core contract reader")
    cases = []
    for descriptor in PACKAGES:
        bundle = load_registration(descriptor.parent)
        for ident, definition in bundle.definitions.items():
            impl = bundle.manifest.implementation(ident)
            module, member = impl.entrypoint.split(":")
            provider = getattr(importlib.import_module(module), member)()
            for fixture in bundle.fixtures[ident]:
                # These require separately installed heavy document engines.
                if ident in {"doc.to_markdown", "ocr.extract"}:
                    cases.append({"definition": definition, "input": fixture.canonical_input})
                    continue
                context = ProviderRunContext(
                    run_id="contract-parity",
                    interface_id=ident,
                    interface_digest=impl.interface_digest,
                    implementation_digest="sha256:" + "0" * 64,
                    input_bucket=fixture.measured_bucket_id,
                    input=fixture.canonical_input,
                    coordinates={
                        "instance_url": "https://fixture.invalid",
                        "as_of": "2026-04-01T00:00:00Z",
                    },
                    budgets=Budgets(wall_clock_seconds=120, output_bytes=16_000_000),
                    declared_endpoints=impl.declared_endpoints,
                    capture_contract=None,
                    secrets={},
                    egress=EgressRecorder(),
                )
                result = provider(context)
                assert result.status == "ok", (ident, fixture.fixture_id, result)
                _check_nested_schema(definition, "output", result.output)
                cases.append(
                    {
                        "definition": definition,
                        "input": fixture.canonical_input,
                        "output": result.output,
                    }
                )
                if ident == "search.web":
                    assert result.output is not None
                    material = json.loads(base64.b64decode(result.output["content_base64"]))
                    raw = base64.b64decode(material["retrieved"]["body_base64"])
                    assert (
                        "sha256:" + hashlib.sha256(raw).hexdigest()
                        == material["retrieved"]["body_sha256"]
                    )
                    assert json.loads(raw)["results"]
                    assert material["derived"]["kind"] == "recency_filtered_ranking"
    code = """
import json,sys
from cruxible_client.contracts.provider_contracts import read_provider_operation_contract,validate_provider_value
errors=[]
checked_required=set()
for case in json.load(sys.stdin):
    definition=case['definition']
    contract=read_provider_operation_contract(json.dumps(definition).encode().hex())
    for direction in ('input','output'):
        if direction in case:
            try: validate_provider_value(contract,case[direction],direction=direction)
            except Exception as e: errors.append((definition['interface_id'],direction,str(e)))
            schema=definition['contracts'][direction]
            if not isinstance(schema,dict): continue
            for name,field in schema['fields'].items():
                key=(definition['interface_id'],direction,name)
                if field.get('optional',False) or key in checked_required: continue
                checked_required.add(key)
                missing={k:v for k,v in case[direction].items() if k!=name}
                try: validate_provider_value(contract,missing,direction=direction)
                except Exception: pass
                else: errors.append((*key,'missing required field accepted'))
assert not errors, errors
"""
    result = subprocess.run(
        [core_python, "-c", code], input=canonical_json(cases), capture_output=True
    )
    assert result.returncode == 0, result.stderr.decode()
