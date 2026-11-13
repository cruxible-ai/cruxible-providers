"""Package registration (schema 3) is complete without the monorepo or core.

Every package ships one ``registration.json`` declaring its operations (what
each does, where its code is, the capabilities it serves, its Contracts), the
copies of every shape they reach (``declares``), and its manifest by path and
digest. Reading it imports no provider code; every cross-link is checked.

Two packages are not schema 3 yet, each for a reason recorded in
:data:`PENDING`; the runtime refuses them, and their own suites say so.

With ``CRUXIBLE_CORE_PYTHON`` set to an interpreter that has core
(``cruxible_client``), every schema-3 registration is also held to core's own
model and registration law (:func:`test_every_registration_is_valid_under_cores_model`).
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
from cruxible_provider_runtime.operations import ProviderOperation, operation_signature_digest
from cruxible_provider_runtime.registration import (
    declared_closure,
    load_registration,
    render_registration,
    write_registration,
)
from cruxible_provider_runtime.wheels import wheel_registration

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = tuple(sorted(ROOT.glob("packages/*/src/*/registration.json")))

_NO_CONTRACTS = (
    "its operations name Contracts that exist nowhere, so it cannot carry them "
    "(design note F4); out of the 0.5.0 publish set (Q5)"
)
#: Packages whose registration is not schema 3, and why. Moving one takes it off.
PENDING = {
    "cruxible_provider_docs": _NO_CONTRACTS,
    "cruxible_provider_quant": _NO_CONTRACTS,
}
MOVED = tuple(path for path in PACKAGES if path.parent.name not in PENDING)


def _schema(descriptor: Path) -> object:
    return json.loads(descriptor.read_bytes()).get("schema_version")


def test_the_pending_list_is_exactly_the_packages_not_yet_on_schema_3() -> None:
    assert {path.parent.name for path in PACKAGES if _schema(path) != 3} == set(PENDING)


@pytest.mark.parametrize(
    "descriptor",
    [path for path in PACKAGES if path.parent.name in PENDING],
    ids=lambda p: p.parent.name,
)
def test_a_pending_registration_is_refused_as_not_schema_3(descriptor: Path) -> None:
    with pytest.raises(ValueError, match="is not read; schema 3"):
        load_registration(descriptor.parent)


@pytest.mark.parametrize("descriptor", MOVED, ids=lambda p: p.parent.name)
def test_package_metadata_is_self_contained(descriptor: Path, tmp_path: Path) -> None:
    package = tmp_path / descriptor.parent.name
    shutil.copytree(descriptor.parent, package)
    bundle = load_registration(package)
    assert bundle.operations
    for operation in bundle.operations:
        assert operation.entrypoint in {item.entrypoint for item in bundle.manifest.implementations}
        assert not operation.pinned, "operations name Contracts by name@N; the install pins them"
        assert all(bundle.contract(ref) for ref in operation.contract_refs())
    assert bundle.manifest.supported_protocol_majors == (2,)


@pytest.mark.parametrize("descriptor", MOVED, ids=lambda p: p.parent.name)
def test_the_file_is_exactly_what_the_writer_renders(descriptor: Path) -> None:
    """Field order, two-space indentation, one final newline: no hand-formatting drift."""

    bundle = load_registration(descriptor.parent)
    assert descriptor.read_bytes() == render_registration(bundle.descriptor)


@pytest.mark.parametrize("descriptor", MOVED, ids=lambda p: p.parent.name)
def test_every_operation_names_its_source_in_this_repository(descriptor: Path) -> None:
    for operation in load_registration(descriptor.parent).operations:
        module = operation.entrypoint.split(":")[0]
        assert operation.source.repository == "https://github.com/cruxible-ai/cruxible-providers"
        assert operation.source.revision is None, "a build stamps the revision; the source not"
        assert (ROOT / operation.source.path).is_file()
        assert operation.source.path.endswith("/src/" + module.replace(".", "/") + ".py")


@pytest.mark.parametrize("descriptor", MOVED, ids=lambda p: p.parent.name)
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


@pytest.mark.parametrize("descriptor", MOVED, ids=lambda p: p.parent.name)
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


def _with_operations(document: dict[str, Any], operations: list[dict[str, Any]]) -> None:
    """``document`` keeping only ``operations``, its ``declares`` cut to their closure."""

    declares = declared_closure(
        [ProviderOperation.model_validate(item) for item in operations],
        document["declares"]["contracts"],
        document["declares"].get("capture_kinds", ()),
        document["declares"].get("built_against", ()),
    )
    document["operations"] = operations
    document["declares"] = declares.model_dump(mode="json")


def _copy(tmp_path: Path, name: str) -> Path:
    source = next(p.parent for p in PACKAGES if p.parent.name == name)
    target = tmp_path / name
    shutil.copytree(source, target)
    return target


@pytest.fixture()
def package_copy(tmp_path: Path) -> Path:
    return _copy(tmp_path, "cruxible_provider_noop")


@pytest.fixture()
def web_copy(tmp_path: Path) -> Path:
    """A package with five entrypoints and two runtime requirements on one of them."""

    return _copy(tmp_path, "cruxible_provider_web")


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


@pytest.mark.parametrize("version", [1, 2, 4, None])
def test_only_schema_3_is_read(package_copy: Path, version: int | None) -> None:
    """Schema 2 named Contracts the instance had to hold; schema 1 exported interfaces."""

    document = _document(package_copy)
    if version is None:
        del document["schema_version"]
    else:
        document["schema_version"] = version
    _rewrite(package_copy, document)
    with pytest.raises(ValueError, match="is not read; schema 3"):
        load_registration(package_copy)


def test_duplicate_operations_and_unknown_fields_refuse(package_copy: Path) -> None:
    document = _document(package_copy)
    document["operations"] *= 2
    _rewrite(package_copy, document)
    with pytest.raises(ValueError, match="sorted by name and each named once"):
        load_registration(package_copy)
    document["operations"] = document["operations"][:2]
    document["trust_me"] = True
    _rewrite(package_copy, document)
    with pytest.raises(ValueError, match="trust_me"):
        load_registration(package_copy)


def test_enumerates_is_refused(package_copy: Path) -> None:
    document = _document(package_copy)
    document["operations"][0]["enumerates"] = {
        "capture": "captures",
        "selector": "/*",
        "fields": {"id": {"at": "id"}},
    }
    _rewrite(package_copy, document)
    with pytest.raises(ValueError, match="enumerates"):
        load_registration(package_copy)


def test_a_package_serving_a_workspace_capability_is_refused(package_copy: Path) -> None:
    document = _document(package_copy)
    document["operations"][0]["serves"] = ["workspace.file"]
    _rewrite(package_copy, document)
    with pytest.raises(ValueError, match=r"`workspace\.file` is served by core"):
        load_registration(package_copy)


def test_declares_must_carry_every_contract_the_operations_reach(web_copy: Path) -> None:
    document = _document(web_copy)
    document["declares"]["contracts"] = [
        item
        for item in document["declares"]["contracts"]
        if item["name"] != "web.fetch.query_param"
    ]
    _rewrite(web_copy, document)
    with pytest.raises(ValueError, match=r"names web\.fetch\.query_param@1, which declares does n"):
        load_registration(web_copy)


def test_declares_may_not_carry_what_nothing_reaches(web_copy: Path) -> None:
    document = _document(web_copy)
    keep = [item for item in document["operations"] if item["name"] != "web.render"]
    document["operations"] = keep
    _rewrite(web_copy, document)
    with pytest.raises(ValueError, match=r"web\.render\.request@1, which no operation"):
        load_registration(web_copy)


def test_operations_must_be_sorted(web_copy: Path) -> None:
    document = _document(web_copy)
    document["operations"].reverse()
    _rewrite(web_copy, document)
    with pytest.raises(ValueError, match="sorted by name"):
        load_registration(web_copy)


def test_an_operation_naming_an_undeclared_entrypoint_refuses(package_copy: Path) -> None:
    document = _document(package_copy)
    document["operations"][0]["entrypoint"] = "cruxible_provider_noop.provider:Elsewhere"
    _rewrite(package_copy, document)
    with pytest.raises(ValueError, match="which the manifest does not declare"):
        load_registration(package_copy)


def test_an_implementation_serving_no_operation_refuses(web_copy: Path) -> None:
    document = _document(web_copy)
    fetch = [item for item in document["operations"] if item["name"] == "web.fetch"]
    _with_operations(document, fetch)
    document["runtime_requirements"] = []
    _rewrite(web_copy, document)
    with pytest.raises(ValueError, match="serves at least one operation"):
        load_registration(web_copy)


def test_one_entrypoint_may_serve_two_operations(package_copy: Path) -> None:
    document = _document(package_copy)
    echo = next(item for item in document["operations"] if item["name"] == "noop.echo")
    document["operations"].append({**echo, "name": "noop.shout"})
    _rewrite(package_copy, document)
    bundle = load_registration(package_copy)
    assert bundle.entry_points() == {
        "noop.captures": "cruxible_provider_noop.captures:NoopCaptures",
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


def test_a_runtime_requirement_belongs_to_a_declared_implementation(web_copy: Path) -> None:
    document = _document(web_copy)
    document["runtime_requirements"][0]["entrypoint"] = "cruxible_provider_web.x:Absent"
    _rewrite(web_copy, document)
    with pytest.raises(ValueError, match="undeclared implementation"):
        load_registration(web_copy)
    document["runtime_requirements"][0]["entrypoint"] = "cruxible_provider_web.fetch:WebFetch"
    _rewrite(web_copy, document)
    with pytest.raises(ValueError, match="undeclared extra"):
        load_registration(web_copy)


def test_a_runtime_requirement_is_keyed_by_entrypoint_not_interface(web_copy: Path) -> None:
    document = _document(web_copy)
    requirement = document["runtime_requirements"][0]
    requirement["interface_id"] = requirement.pop("entrypoint")
    _rewrite(web_copy, document)
    with pytest.raises(ValueError, match="interface_id"):
        load_registration(web_copy)


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
            names = [name for name in archive.namelist() if name.endswith("/registration.json")]
            if not names or json.loads(archive.read(names[0])).get("schema_version") != 3:
                continue
        with wheel_registration(wheel) as bundle:
            seen.add(bundle.manifest.distribution.name)
    assert seen == {load_registration(p.parent).manifest.distribution.name for p in MOVED}


# -- parity with core's model ------------------------------------------------------------------


def _core_python() -> str:
    """The interpreter with core's model; required in CI, optional on a workstation.

    CI's ``core-parity`` lane checks core out at a pinned commit and sets
    ``CRUXIBLE_CORE_PYTHON``. When ``CI`` is set and the variable is not, the
    lane lost its prerequisite, and that is a failure rather than a skip.
    """

    core_python = os.environ.get("CRUXIBLE_CORE_PYTHON")
    if core_python:
        return core_python
    message = "set CRUXIBLE_CORE_PYTHON to an interpreter with core's model (cruxible_client)"
    if os.environ.get("CI"):
        pytest.fail(f"{message}: CI runs the core-parity checks and must not skip them")
    pytest.skip(message)


def test_ci_runs_the_core_parity_lane_against_a_pinned_core() -> None:
    """The lane exists, pins core by full commit, sets the interpreter, and runs the marker."""

    import re

    import yaml

    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
    assert re.fullmatch(r"[0-9a-f]{40}", workflow["env"]["CORE_COMMIT"])
    steps = workflow["jobs"]["core-parity"]["steps"]
    checkout = next(step for step in steps if step.get("with", {}).get("path") == "core")
    assert checkout["with"] == {
        "repository": "cruxible-ai/cruxible",
        "ref": "${{ env.CORE_COMMIT }}",
        "path": "core",
    }
    runs = "\n".join(str(step.get("run", "")) for step in steps)
    assert "CRUXIBLE_CORE_PYTHON=" in runs and "core/packages/cruxible-client" in runs
    assert "-m core_parity tests/test_package_registration.py" in runs
    default = "\n".join(str(step.get("run", "")) for step in workflow["jobs"]["test"]["steps"])
    assert "not core_parity" in default


def _core_report(roots: list[str]) -> dict[str, Any]:
    result = subprocess.run(
        [_core_python(), "-c", _CORE_PARITY], input=canonical_json(roots), capture_output=True
    )
    assert result.returncode == 0, result.stderr.decode()
    report: dict[str, Any] = json.loads(result.stdout)
    assert sorted(report) == sorted(roots)
    return report


_CORE_PARITY = """
import json, sys
from pathlib import Path
from cruxible_client.contracts.provider_operations import (
    OperationSignature, operation_signature_digest,
)
from cruxible_client.contracts.provider_registration import (
    PackageRegistration, evaluate_registration_law,
)
from cruxible_client.contracts.shapes import contract_digest
report = {}
for root in json.load(sys.stdin):
    raw = (Path(root) / "registration.json").read_bytes()
    registration = PackageRegistration.model_validate_json(raw)
    resolve = registration.declares.lookup()
    signatures = {}
    for operation in registration.operations:
        def pin(ref):
            return ref.model_copy(update={"digest": contract_digest(resolve(ref)).tagged})
        signature = OperationSignature.model_validate(
            {"in": pin(operation.in_), "out": pin(operation.out), "effect": operation.effect}
        )
        signatures[operation.name] = {
            "in": signature.in_.digest, "out": signature.out.digest,
            "digest": operation_signature_digest(signature),
        }
    report[root] = {
        "problems": [f"{item.code}: {item.message}" for item in evaluate_registration_law(registration)],
        "document": registration.model_dump(mode="json", by_alias=True),
        "signatures": signatures,
    }
assert not any(name.startswith("cruxible_provider_") for name in sys.modules)
json.dump(report, sys.stdout)
"""


def _filled_template(tmp_path: Path) -> Path:
    """The template's manifest and registration with ``PLANE`` named and the digest filled in."""

    template = ROOT / "packages" / "_template" / "src" / "cruxible_provider_PLANE"
    root = tmp_path / "cruxible_provider_sample"
    root.mkdir()
    manifest = (template / "manifest.yaml.template").read_text().replace("PLANE", "sample")
    (root / "manifest.yaml").write_text(manifest)
    digest = "sha256:" + hashlib.sha256(manifest.encode()).hexdigest()
    registration = (template / "registration.json.template").read_text()
    registration = registration.replace("PLANE", "sample").replace(
        "sha256:REPLACE_WITH_THE_SHA256_OF_MANIFEST_YAML", digest
    )
    (root / "registration.json").write_text(registration)
    return root


def test_the_template_filled_in_is_a_schema_3_package(tmp_path: Path) -> None:
    bundle = load_registration(_filled_template(tmp_path))
    assert [item.serves for item in bundle.operations] == [("sample.capability",)]


@pytest.mark.core_parity
def test_every_registration_is_valid_under_cores_model(tmp_path: Path) -> None:
    """Core's own schema-3 model and registration law, in core's interpreter.

    Each schema-3 ``registration.json`` this repository builds parses as core's
    ``PackageRegistration`` and passes ``evaluate_registration_law`` with no
    problem (every reference resolves inside ``declares``, every nested pin is
    the carried copy's digest, nothing unreached, no ``workspace.`` capability).
    Core reads the same document the runtime reads, field for field, and core's
    signature digest of every operation, over the carried copies' digests,
    equals this runtime's. The package template, filled in, is held to the same.
    """

    roots = [str(path.parent) for path in MOVED] + [str(_filled_template(tmp_path))]
    report = _core_report(roots)
    for root, found in report.items():
        assert found["problems"] == [], (root, found["problems"])
        bundle = load_registration(Path(root))
        assert found["document"] == bundle.descriptor.model_dump(mode="json", by_alias=True)
        for operation in bundle.operations:
            pinned = found["signatures"][operation.name]
            assert pinned["digest"] == operation_signature_digest(
                in_digest=pinned["in"], out_digest=pinned["out"], effect=operation.effect
            )


@pytest.mark.core_parity
@pytest.mark.parametrize("script", ["render_web_contracts.py", "render_noop_contracts.py"])
def test_the_carried_contracts_are_what_the_renderers_write(script: str) -> None:
    """The renderers build each copy with core's Contract model; the files hold their output."""

    check = subprocess.run(
        [_core_python(), str(ROOT / "scripts" / script), "--check"], capture_output=True
    )
    assert check.returncode == 0, check.stderr.decode()


@pytest.mark.core_parity
def test_the_writer_output_for_a_spaced_layout_type_passes_cores_law(tmp_path: Path) -> None:
    """A layout rule typed ``demo.dep @ 1`` (legal whitespace): the writer carries ``demo.dep``.

    The noop package with one more request field, ``document: capture<demo.rows>``,
    and the kind ``demo.rows`` whose header names ``demo.dep @ 1``. The runtime
    loads what the writer wrote, and core's model and law accept the same file.
    """

    root = _copy(tmp_path, "cruxible_provider_noop")
    document = _document(root)
    contracts = list(document["declares"]["contracts"])
    for index, item in enumerate(contracts):
        if item["name"] == "noop.captures.request":
            fields = {
                **item["fields"],
                "document": {
                    "optional": True,
                    "type": {"kind": "capture", "capture_kind": "demo.rows"},
                },
            }
            contracts[index] = {**item, "fields": fields}
    contracts += [
        {"artifact_format": "cruxible-contract-v1", "name": "demo.dep", "version": 1, "fields": {}},
        {
            "artifact_format": "cruxible-contract-v1",
            "name": "demo.row",
            "version": 1,
            "fields": {"value": {"type": {"kind": "string"}}},
        },
    ]
    kind = {
        "name": "demo.rows",
        "decoding": {
            "format": "json",
            "encoding": "identity",
            "limits": {"decoded_bytes": 1024, "depth": 8, "records": 100},
        },
        "layout": {
            "header": {"extra": {"at": "/extra", "type": "demo.dep @ 1"}},
            "records": {
                "rows": {
                    "at": "/rows/*",
                    "row": {"name": "demo.row", "version": 1},
                    "fields": {"value": {"at": "value", "type": "string"}},
                }
            },
        },
    }
    write_registration(root, contracts=contracts, capture_kinds=[kind])
    bundle = load_registration(root)
    assert {"demo.dep", "demo.row"} <= {item.name for item in bundle.declares.contracts}
    report = _core_report([str(root)])
    assert report[str(root)]["problems"] == []
