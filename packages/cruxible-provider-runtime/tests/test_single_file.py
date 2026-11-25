"""Single-file providers: load a ``.py`` file, discover its derives, build schema-3 metadata."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import textwrap
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from cruxible_provider_runtime import (
    CaptureView,
    DeriveDefinitionError,
    SingleFileError,
    SingleFilePackage,
    build_single_file_package,
    discover_derives,
    load_single_file_module,
)
from cruxible_provider_runtime.acquisition import external_capture_result
from cruxible_provider_runtime.backends import CHILD_MODULE
from cruxible_provider_runtime.manifest import load_manifest
from cruxible_provider_runtime.protocol import (
    PROTOCOL_VERSION,
    Budgets,
    RunContext,
    parse_result_envelope,
)
from cruxible_provider_runtime.registration import REGISTRATION_FILE, load_registration
from cruxible_provider_runtime.single_file import MANIFEST_FILE, run_cli

RUNTIME_SRC = Path(__file__).resolve().parents[1] / "src"
OBSERVED = datetime(2026, 11, 4, 8, 30, tzinfo=UTC)

PROVIDER = '''
"""Triage: a single-file provider of two derives."""

from dataclasses import dataclass

from cruxible_provider_runtime import CaptureView, decline, derive

print("a provider that prints while it is imported")


@dataclass(frozen=True)
class Finding:
    text: str


@derive(
    "triage.summarize",
    serves="triage.summarize",
    input_contract="triage.input@1",
    output_contract="triage.output@1",
    refusals={"empty": "triage.refusal.empty@1"},
)
def summarize(source: CaptureView, max_items: int = 2) -> dict:
    """Summarizes the advisory's top findings into one document."""
    findings = [Finding(item) for item in source.json()["findings"]]
    if not findings:
        raise decline("empty", "the advisory lists no findings")
    return {"summary": source.derive({"findings": [item.text for item in findings[:max_items]]})}


@derive(
    "triage.count",
    serves=["triage.count", "triage.measure"],
    input_contract="triage.input@1",
    output_contract="triage.count@1",
)
def count(source: CaptureView) -> dict:
    """Counts the advisory's bytes."""
    return {"bytes": source.byte_length}


def helper() -> None:
    """Not an operation."""
'''


def contract(
    name: str, fields: dict[str, Any], *, description: str | None = None
) -> dict[str, Any]:
    document: dict[str, Any] = {
        "artifact_format": "cruxible-contract-v1",
        "name": name,
        "version": 1,
        "fields": fields,
    }
    if description:
        document["description"] = description
    return document


CONTRACTS = [
    contract(
        "triage.input",
        {
            "source": {"type": {"kind": "capture", "capture_kind": "*"}},
            "max_items": {"optional": True, "type": {"kind": "int"}},
        },
        description="One fetched advisory.",
    ),
    contract(
        "triage.output",
        {"summary": {"type": {"kind": "capture", "capture_kind": "triage.summary"}}},
    ),
    contract("triage.count", {"bytes": {"type": {"kind": "int"}}}),
    contract("triage.refusal.empty", {}),
    contract("unrelated.shape", {"value": {"type": {"kind": "string"}}}),
]
SUMMARY_KIND = {
    "name": "triage.summary",
    "decoding": {
        "format": "json",
        "encoding": "identity",
        "limits": {"decoded_bytes": 65536, "depth": 8, "records": 100},
    },
}
UNRELATED_KIND = {"name": "unrelated.kind"}


def write(path: Path, source: str) -> Path:
    path.write_text(textwrap.dedent(source), encoding="utf-8")
    return path


@pytest.fixture()
def provider_file(tmp_path: Path) -> Path:
    return write(tmp_path / "triage.py", PROVIDER)


def build(module: Any, **overrides: Any) -> SingleFilePackage:
    arguments: dict[str, Any] = {
        "distribution": "triage-provider",
        "version": "0.1.0",
        "contracts": CONTRACTS,
        "capture_kinds": [SUMMARY_KIND, UNRELATED_KIND],
        "module_name": "triage_provider",
    }
    arguments.update(overrides)
    return build_single_file_package(module, **arguments)


def wire(content: bytes) -> dict[str, Any]:
    return external_capture_result(
        content,
        source_identity="web.response",
        coordinate_type="http-response-v1",
        coordinate={"url": "https://example.test/advisory"},
        observed_at=OBSERVED,
        selector_type="whole-response-v1",
    )


# -- loading -----------------------------------------------------------------------------------


def test_a_file_loads_under_the_name_it_is_given_and_leaves_no_import(provider_file: Path) -> None:
    module = load_single_file_module(provider_file, module_name="triage_staging")
    assert module.__name__ == "triage_staging"
    assert "triage_staging" not in sys.modules
    assert module.summarize.fn.__module__ == "triage_staging"
    assert module.Finding("x").text == "x"  # a dataclass resolves its module while loading
    default = load_single_file_module(provider_file)
    assert default.__name__ == "triage"


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("not-an-identifier", "not a Python identifier"),
        ("class", "not a Python identifier"),
        ("json", "standard-library"),
        ("pydantic", "already an importable module"),
        ("cruxible_provider_runtime", "already an importable module"),
    ],
)
def test_a_module_name_that_would_collide_is_refused(
    provider_file: Path, name: str, message: str
) -> None:
    with pytest.raises(SingleFileError, match=message):
        load_single_file_module(provider_file, module_name=name)


def test_only_a_python_file_loads(tmp_path: Path) -> None:
    with pytest.raises(SingleFileError, match="not a Python file"):
        load_single_file_module(tmp_path / "missing.py")
    with pytest.raises(SingleFileError, match="not a Python file"):
        load_single_file_module(write(tmp_path / "notes.txt", "x = 1\n"))


def test_a_file_that_fails_to_import_raises_and_leaves_no_import(tmp_path: Path) -> None:
    path = write(tmp_path / "exploding.py", "raise RuntimeError('boom')\n")
    with pytest.raises(RuntimeError, match="boom"):
        load_single_file_module(path)
    assert "exploding" not in sys.modules


# -- discovery ---------------------------------------------------------------------------------


def test_discovery_finds_every_derive_sorted_by_operation_name(provider_file: Path) -> None:
    module = load_single_file_module(provider_file, module_name="triage_discovery")
    found = discover_derives(module)
    assert [(item.attribute, item.operation.name) for item in found] == [
        ("count", "triage.count"),
        ("summarize", "triage.summarize"),
    ]
    assert found[0].entrypoint("triage_provider") == "triage_provider:count"


def test_a_module_with_no_derive_is_refused(tmp_path: Path) -> None:
    module = load_single_file_module(write(tmp_path / "empty_ops.py", "def f():\n    pass\n"))
    with pytest.raises(SingleFileError, match="defines no @derive operation"):
        discover_derives(module)


HEADER = """
from cruxible_provider_runtime import derive

def declare(name):
    return derive(name, serves="x.y", input_contract="x.in@1", output_contract="x.out@1")
"""


def test_an_operation_defined_twice_is_refused(tmp_path: Path) -> None:
    twice = HEADER + textwrap.dedent(
        '''
        @declare("x.same")
        def first():
            """One."""
            return {}

        @declare("x.same")
        def second():
            """Two."""
            return {}
        '''
    )
    module = load_single_file_module(write(tmp_path / "twice.py", twice))
    with pytest.raises(
        SingleFileError, match=r"defined more than once: 'x\.same' at first, second"
    ):
        discover_derives(module)


def test_one_derive_bound_to_two_attributes_is_refused(tmp_path: Path) -> None:
    aliased = HEADER + textwrap.dedent(
        '''
        @declare("x.one")
        def original():
            """One."""
            return {}

        alias = original
        '''
    )
    module = load_single_file_module(write(tmp_path / "aliased.py", aliased))
    with pytest.raises(SingleFileError, match=r"'x\.one' at alias, original"):
        discover_derives(module)


def test_a_derive_imported_from_another_module_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(
        tmp_path / "shared_derives.py",
        HEADER + '\n@declare("x.shared")\ndef shared():\n    """Shared."""\n    return {}\n',
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importer = write(tmp_path / "importer.py", "from shared_derives import shared\n")
    try:
        module = load_single_file_module(importer)
        with pytest.raises(SingleFileError, match="defined in module 'shared_derives'"):
            discover_derives(module)
    finally:
        sys.modules.pop("shared_derives", None)


def test_an_annotation_that_never_resolves_is_reported_by_discovery(tmp_path: Path) -> None:
    source = (
        "from __future__ import annotations\n"
        + HEADER
        + '\n@declare("x.bad")\ndef bad(value: Nowhere):\n    """Bad."""\n    return {}\n'
    )
    module = load_single_file_module(write(tmp_path / "unresolved.py", source))
    with pytest.raises(DeriveDefinitionError, match="do not resolve"):
        discover_derives(module)


# -- building ----------------------------------------------------------------------------------


def test_the_package_is_schema_3_metadata_the_runtime_reads(
    provider_file: Path, tmp_path: Path
) -> None:
    module = load_single_file_module(provider_file, module_name="triage_staging")
    package = build(module)
    assert package.module_name == "triage_provider"
    assert (package.distribution, package.version) == ("triage-provider", "0.1.0")
    assert package.entry_points == {
        "triage.count": "triage_provider:count",
        "triage.summarize": "triage_provider:summarize",
    }

    root = tmp_path / "triage_provider"
    written = package.write(root)
    assert sorted(written) == [MANIFEST_FILE, REGISTRATION_FILE]
    bundle = load_registration(root)
    assert bundle.descriptor == package.registration
    assert bundle.manifest == package.manifest == load_manifest(root / MANIFEST_FILE)
    assert bundle.entry_points() == package.entry_points

    document = json.loads(package.registration_bytes)
    assert document["schema_version"] == 3
    assert document["manifest"] == {
        "path": MANIFEST_FILE,
        "digest": "sha256:" + hashlib.sha256(package.manifest_bytes).hexdigest(),
    }
    assert document["operations"] == [
        {
            "name": "triage.count",
            "description": "Counts the advisory's bytes.",
            "source": {"path": "triage.py"},
            "serves": ["triage.count", "triage.measure"],
            "in": "triage.input@1",
            "out": "triage.count@1",
            "effect": "pure",
            "entrypoint": "triage_provider:count",
        },
        {
            "name": "triage.summarize",
            "description": "Summarizes the advisory's top findings into one document.",
            "source": {"path": "triage.py"},
            "serves": ["triage.summarize"],
            "in": "triage.input@1",
            "out": "triage.output@1",
            "effect": "pure",
            "refusals": {
                "empty": {"contract": "triage.refusal.empty@1", "absorbable": False},
            },
            "entrypoint": "triage_provider:summarize",
        },
    ]
    assert document["runtime_requirements"] == []
    # declares carries exactly what the operations reach: the unrelated shapes stay out.
    assert [item["name"] for item in document["declares"]["contracts"]] == [
        "triage.count",
        "triage.input",
        "triage.output",
        "triage.refusal.empty",
    ]
    assert document["declares"]["capture_kinds"] == [SUMMARY_KIND]


def test_each_operation_has_a_pure_implementation_row(provider_file: Path) -> None:
    package = build(load_single_file_module(provider_file, module_name="triage_rows"))
    manifest = json.loads(package.manifest_bytes)
    assert manifest == {
        "schema_version": 2,
        "provider_id": "triage-provider",
        "distribution": {"name": "triage-provider", "version": "0.1.0"},
        "entrypoint_group": "cruxible.providers",
        "supported_protocol_majors": [PROTOCOL_VERSION.major],
        "implementations": [
            {
                "entrypoint": f"triage_provider:{attribute}",
                "backends": ["local_env"],
                "declared_endpoints": [],
                "requires_extras": [],
                "deterministic": True,
            }
            for attribute in ("count", "summarize")
        ],
    }
    assert all(item.effect == "pure" for item in package.operations)
    with_image = build(
        load_single_file_module(provider_file, module_name="triage_rows"),
        backends=("local_env", "container"),
    )
    assert with_image.manifest.implementations[0].backends == ("local_env", "container")


def test_building_is_deterministic(provider_file: Path) -> None:
    first = build(load_single_file_module(provider_file, module_name="triage_one"))
    second = build(load_single_file_module(provider_file, module_name="triage_two"))
    assert first.files() == second.files()


def test_the_module_name_defaults_to_the_loaded_name(provider_file: Path) -> None:
    module = load_single_file_module(provider_file, module_name="triage_named")
    package = build(module, module_name=None)
    assert package.module_name == "triage_named"
    assert set(package.entry_points.values()) == {"triage_named:count", "triage_named:summarize"}


def test_where_the_code_is_can_be_stated(provider_file: Path) -> None:
    module = load_single_file_module(provider_file, module_name="triage_source")
    package = build(
        module,
        source_path="providers/triage.py",
        repository="https://github.com/example/providers",
        revision="0123456789abcdef0123456789abcdef01234567",
    )
    assert package.operations[0].source.model_dump(exclude_none=True) == {
        "path": "providers/triage.py",
        "repository": "https://github.com/example/providers",
        "revision": "0123456789abcdef0123456789abcdef01234567",
    }
    with pytest.raises(SingleFileError, match="names its repository"):
        build(module, revision="0123456789abcdef0123456789abcdef01234567")
    with pytest.raises(SingleFileError, match="relative POSIX path"):
        build(module, source_path="/abs/triage.py")


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"distribution": "Triage_Provider"}, "use 'triage-provider'"),
        ({"version": "1.0-beta"}, "use '1.0b0'"),
        ({"version": "not a version"}, "not a valid version"),
        ({"module_name": "triage-provider"}, "not a Python identifier"),
        ({"module_name": "os"}, "standard-library"),
        ({"contracts": CONTRACTS[1:]}, "triage.input@1, which declares does not carry"),
        ({"capture_kinds": []}, "no copy of CaptureKind `triage.summary`"),
        ({"contracts": []}, "declares"),
        ({"backends": ()}, "at least one backend"),
    ],
)
def test_a_package_that_cannot_be_built_is_refused(
    provider_file: Path, overrides: dict[str, Any], message: str
) -> None:
    module = load_single_file_module(provider_file, module_name="triage_refused")
    with pytest.raises(SingleFileError, match=message):
        build(module, **overrides)


def test_the_summary_is_what_a_publisher_needs(provider_file: Path) -> None:
    package = build(load_single_file_module(provider_file, module_name="triage_summary"))
    summary = package.summary()
    assert summary["entry_points"] == package.entry_points
    assert summary["runtime_requirement"].startswith("cruxible-provider-runtime>=")
    assert summary["files"] == {
        name: "sha256:" + hashlib.sha256(data).hexdigest() for name, data in package.files().items()
    }
    assert summary["operations"] == json.loads(package.registration_bytes)["operations"]


# -- the built package, run ------------------------------------------------------------------


def _package_directory(tmp_path: Path, provider_file: Path) -> tuple[Path, SingleFilePackage]:
    """The layout a publisher writes: the module as a package, its metadata beside it."""

    module = load_single_file_module(provider_file, module_name="triage_build")
    package = build(module)
    site = tmp_path / "site"
    target = site / package.module_name
    target.mkdir(parents=True)
    shutil.copyfile(provider_file, target / "__init__.py")
    package.write(target)
    return site, package


def test_the_built_entrypoints_run_through_the_child_harness(
    tmp_path: Path, provider_file: Path
) -> None:
    site, package = _package_directory(tmp_path, provider_file)
    bundle = load_registration(site / package.module_name)
    advisory = wire(b'{"findings": ["a", "b", "c"]}')
    outputs: dict[str, Any] = {}
    for operation in bundle.operations:
        document = RunContext(
            protocol_version=PROTOCOL_VERSION.render(),
            run_id=f"run-{operation.name}",
            operation=operation.name,
            operation_signature_digest="sha256:" + "51" * 32,
            build_digest="sha256:" + "b1" * 32,
            implementation_digest="sha256:" + "1a" * 32,
            entrypoint=operation.entrypoint,
            input={"source": advisory},
            budgets=Budgets(wall_clock_seconds=30.0, output_bytes=1_000_000),
        ).model_dump(mode="json")
        completed = subprocess.run(
            [sys.executable, "-m", CHILD_MODULE, "--entrypoint", operation.entrypoint],
            input=json.dumps(document).encode("utf-8"),
            capture_output=True,
            env={**os.environ, "PYTHONPATH": os.pathsep.join((str(RUNTIME_SRC), str(site)))},
            check=True,
            timeout=60,
        )
        envelope = parse_result_envelope(completed.stdout)
        assert envelope.status == "ok", envelope
        outputs[operation.name] = envelope.output
    assert outputs["triage.count"] == {"bytes": len(b'{"findings": ["a", "b", "c"]}')}
    summary = CaptureView(outputs["triage.summarize"]["summary"])
    assert summary.json() == {"findings": ["a", "b"]}
    assert summary.observed_at == OBSERVED


# -- the command line --------------------------------------------------------------------------


def _cli(*argv: str, cwd: Path) -> tuple[int, dict[str, Any], str]:
    completed = subprocess.run(
        [sys.executable, "-m", "cruxible_provider_runtime.single_file", *argv],
        capture_output=True,
        cwd=cwd,
        env={**os.environ, "PYTHONPATH": str(RUNTIME_SRC)},
        timeout=60,
    )
    return completed.returncode, json.loads(completed.stdout), completed.stderr.decode()


def test_the_cli_describes_a_file(provider_file: Path, tmp_path: Path) -> None:
    status, document, stderr = _cli(
        "describe", str(provider_file), "--module-name", "triage_provider", cwd=tmp_path
    )
    assert status == 0, stderr
    assert "a provider that prints while it is imported" in stderr
    assert document["module_name"] == "triage_provider"
    assert [item["name"] for item in document["operations"]] == ["triage.count", "triage.summarize"]
    summarize = document["operations"][1]
    assert summarize["attribute"] == "summarize"
    assert summarize["entrypoint"] == "triage_provider:summarize"
    assert summarize["in"] == "triage.input@1"
    assert summarize["refusals"] == {
        "empty": {"contract": "triage.refusal.empty@1", "absorbable": False}
    }
    assert summarize["capture_fields"] == {"source": "one"}
    assert summarize["effect"] == "pure"


def test_the_cli_builds_the_package_files(provider_file: Path, tmp_path: Path) -> None:
    declares = tmp_path / "declares.json"
    declares.write_text(json.dumps({"contracts": CONTRACTS, "capture_kinds": [SUMMARY_KIND]}))
    out = tmp_path / "out"
    status, document, stderr = _cli(
        "build",
        str(provider_file),
        "--module-name",
        "triage_provider",
        "--distribution",
        "triage-provider",
        "--version",
        "0.1.0",
        "--declares",
        str(declares),
        "--out",
        str(out),
        cwd=tmp_path,
    )
    assert status == 0, stderr
    expected = build(load_single_file_module(provider_file, module_name="triage_cli"))
    assert (out / REGISTRATION_FILE).read_bytes() == expected.registration_bytes
    assert (out / MANIFEST_FILE).read_bytes() == expected.manifest_bytes
    assert document == {
        **expected.summary(),
        "written": {
            MANIFEST_FILE: str(out / MANIFEST_FILE),
            REGISTRATION_FILE: str(out / REGISTRATION_FILE),
        },
    }
    load_registration(out)


def test_the_cli_reports_a_failure_as_json(tmp_path: Path) -> None:
    empty = write(tmp_path / "nothing_here.py", "x = 1\n")
    status, document, _ = _cli("describe", str(empty), cwd=tmp_path)
    assert status == 1
    assert document["error"]["kind"] == "invalid_package"
    assert "defines no @derive operation" in document["error"]["message"]

    exploding = write(tmp_path / "exploding_cli.py", "raise RuntimeError('boom')\n")
    status, document, stderr = _cli("describe", str(exploding), cwd=tmp_path)
    assert status == 1
    assert document["error"] == {"kind": "import_failed", "message": "RuntimeError: boom"}
    assert "Traceback" in stderr


def test_run_cli_names_each_kind_of_failure(provider_file: Path, tmp_path: Path) -> None:
    bad = write(
        tmp_path / "bad_derive.py",
        HEADER + '\n@declare("Bad Name")\ndef f():\n    """Doc."""\n    return {}\n',
    )
    status, document = run_cli(["describe", str(bad)])
    assert status == 1
    assert document["error"]["kind"] == "import_failed"  # the decorator raises at import
    assert "DeriveDefinitionError" in document["error"]["message"]

    status, document = run_cli(["describe", str(provider_file), "--module-name", "json"])
    assert (status, document["error"]["kind"]) == (1, "invalid_package")

    declares = write(tmp_path / "declares.json", json.dumps({"shapes": []}))
    status, document = run_cli(
        [
            "build",
            str(provider_file),
            "--distribution",
            "triage-provider",
            "--version",
            "0.1.0",
            "--declares",
            str(declares),
            "--out",
            str(tmp_path / "out"),
        ]
    )
    assert (status, document["error"]["kind"]) == (1, "invalid_package")
    assert "a declares block" in document["error"]["message"]
    assert not (tmp_path / "out").exists()


# -- parity with core's models -----------------------------------------------------------------


def _core_python() -> str:
    """The interpreter with core's model; required in CI, optional on a workstation."""

    core_python = os.environ.get("CRUXIBLE_CORE_PYTHON")
    if core_python:
        return core_python
    message = "set CRUXIBLE_CORE_PYTHON to an interpreter with core's model (cruxible_client)"
    if os.environ.get("CI"):
        pytest.fail(f"{message}: CI runs the core-parity checks and must not skip them")
    pytest.skip(message)


_CORE_CHECK = """
import json, sys
from cruxible_client.contracts.captures import ProviderResultToExternalCapture
from cruxible_client.contracts.provider_registration import (
    PackageRegistration, evaluate_registration_law,
)
from cruxible_client.contracts.providers import ProviderRuntimeManifest
request = json.load(sys.stdin)
for item in request["wires"]:
    ProviderResultToExternalCapture.model_validate(item)
registration = PackageRegistration.model_validate_json(request["registration"])
ProviderRuntimeManifest.model_validate(json.loads(request["manifest"]))
json.dump({
    "problems": [f"{item.code}: {item.message}" for item in evaluate_registration_law(registration)],
    "operations": [item.name for item in registration.operations],
}, sys.stdout)
"""


@pytest.mark.core_parity
def test_the_wire_and_the_package_are_valid_under_cores_models(provider_file: Path) -> None:
    """Core's own models read what the authoring layer writes.

    Every derived Capture wire -- bytes, text, JSON, a view's ``derive`` -- is a
    valid ``ProviderResultToExternalCapture``; the built registration parses as
    core's ``PackageRegistration`` and passes ``evaluate_registration_law`` with
    no problem; and the manifest is core's ``ProviderRuntimeManifest``.
    """

    from cruxible_provider_runtime import derived_capture

    source = CaptureView(wire(b'{"findings": ["a"]}'))
    wires = [
        derived_capture(b"\x00\xff", observed_at=OBSERVED),
        derived_capture("text ✓", observed_at=OBSERVED),
        derived_capture({"b": [1, 2.5], "a": None}, observed_at=OBSERVED),
        source.derive({"findings": ["a"]}, coordinate={"derived_from": source.bytes_digest}),
        source.wire(),
    ]
    package = build(load_single_file_module(provider_file, module_name="triage_parity"))
    request = {
        "wires": wires,
        "registration": package.registration_bytes.decode("utf-8"),
        "manifest": package.manifest_bytes.decode("utf-8"),
    }
    result = subprocess.run(
        [_core_python(), "-c", _CORE_CHECK],
        input=json.dumps(request).encode("utf-8"),
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr.decode()
    report = json.loads(result.stdout)
    assert report == {"problems": [], "operations": ["triage.count", "triage.summarize"]}
