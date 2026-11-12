"""Registration schema 3: the carried shapes, their closure, and the writer.

A package carries copies of every Contract and CaptureKind its operations
reach, and nothing else. These tests build small registrations from scratch;
``tests/test_package_registration.py`` at the repository root reads the real
packages and holds them to core's model.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from cruxible_provider_runtime.operations import ProviderOperation
from cruxible_provider_runtime.registration import (
    PackageRegistration,
    ProviderDeclares,
    declared_closure,
    declares_problems,
    load_registration,
    render_registration,
    write_registration,
)
from pydantic import ValidationError

DIGEST = "sha256:" + "ab" * 32
ENTRYPOINT = "sample_provider.impl:Echo"


def _contract(name: str, fields: dict[str, Any] | None = None, version: int = 1) -> dict[str, Any]:
    document: dict[str, Any] = {
        "artifact_format": "cruxible-contract-v1",
        "name": name,
        "version": version,
        "description": f"The {name} shape.",
        "fields": fields or {},
    }
    if version > 1:
        document["predecessor_digest"] = DIGEST
    return document


def _nested(name: str, digest: str | None = DIGEST) -> dict[str, Any]:
    pin: dict[str, Any] = {"name": name, "version": 1}
    if digest is not None:
        pin["digest"] = digest
    return {"kind": "contract", "contract": pin}


def _operation(**overrides: Any) -> ProviderOperation:
    document: dict[str, Any] = {
        "name": "sample.echo",
        "description": "Echoes its input back.",
        "source": {"path": "src/sample_provider/impl.py"},
        "serves": ["sample.echo"],
        "in": "sample.echo.request@1",
        "out": "sample.echo.result@1",
        "effect": "pure",
        "refusals": {"too_long": {"contract": "sample.echo.refusal.too_long@1"}},
        "entrypoint": ENTRYPOINT,
    }
    document.update(overrides)
    return ProviderOperation.model_validate(document)


KIND = {
    "name": "sample.rows",
    "decoding": {
        "format": "json",
        "encoding": "identity",
        "limits": {"decoded_bytes": 1024, "depth": 8, "records": 100},
    },
    "layout": {
        "header": {"source": {"at": "/source", "type": 'string{pattern: "^a@1$"}'}},
        "records": {
            "rows": {
                "at": "/rows/*",
                "key": {"id": {"at": "id", "type": "string{max_len: 64}"}},
                "row": {"name": "sample.row", "version": 1, "digest": DIGEST},
                "fields": {"note": {"at": "note", "type": "sample.note@1"}},
            }
        },
    },
}

POOL = [
    _contract("sample.echo.request", {"value": {"type": {"kind": "string"}}}),
    _contract(
        "sample.echo.result",
        {
            "value": {"type": _nested("sample.value")},
            "document": {"type": {"kind": "capture", "capture_kind": "sample.rows"}},
            "raw": {"type": {"kind": "capture", "capture_kind": "*"}},
        },
    ),
    _contract("sample.echo.refusal.too_long"),
    _contract("sample.value", {"text": {"type": {"kind": "string"}}}),
    _contract("sample.row", {"note": {"type": {"kind": "string"}, "optional": True}}),
    _contract("sample.note"),
    _contract("sample.unused"),
]


def test_the_closure_is_exactly_what_the_operations_reach() -> None:
    declares = declared_closure([_operation()], POOL, [KIND])
    assert [item.ref.spelling for item in declares.contracts] == [
        "sample.echo.refusal.too_long@1",
        "sample.echo.request@1",
        "sample.echo.result@1",
        "sample.note@1",
        "sample.row@1",
        "sample.value@1",
    ]
    assert [item.name for item in declares.capture_kinds] == ["sample.rows"]
    assert declares_problems([_operation()], declares) == []


def test_a_quoted_pattern_in_a_rule_type_names_nothing() -> None:
    """``string{pattern: "^a@1$"}`` is text, not a pin to Contract ``a``."""

    declares = declared_closure([_operation()], POOL, [KIND])
    assert all(item.name != "a" for item in declares.contracts)


def test_a_reached_contract_the_pool_lacks_refuses() -> None:
    pool = [item for item in POOL if item["name"] != "sample.value"]
    with pytest.raises(ValueError, match=r"names sample.value@1, which declares does not carry"):
        declared_closure([_operation()], pool, [KIND])


def test_a_nested_pin_without_its_digest_refuses() -> None:
    pool = [
        item
        if item["name"] != "sample.echo.result"
        else _contract("sample.echo.result", {"value": {"type": _nested("sample.value", None)}})
        for item in POOL
    ]
    with pytest.raises(ValueError, match="without its digest"):
        declared_closure([_operation()], pool)


def test_a_named_kind_must_be_carried_or_spelled_star() -> None:
    with pytest.raises(ValueError, match=r"capture<sample.rows>; declares carries no copy"):
        declared_closure([_operation()], POOL)


def test_two_different_copies_of_one_version_refuse() -> None:
    with pytest.raises(ValueError, match=r"two different copies of Contract sample\.note@1"):
        declared_closure([_operation()], [*POOL, _contract("sample.note", {"x": {"type": {}}})])


def test_unreached_shapes_are_problems_when_carried() -> None:
    declares = declared_closure([_operation()], POOL, [KIND])
    padded = ProviderDeclares.model_validate(
        {
            **declares.model_dump(mode="json"),
            "contracts": [
                *declares.model_dump(mode="json")["contracts"],
                _contract("sample.zz"),
            ],
            "capture_kinds": [
                KIND,
                {"name": "sample.zz-raw", "decoding": KIND["decoding"]},
            ],
        }
    )
    problems = declares_problems([_operation()], padded)
    assert any("CaptureKind `sample.zz-raw`, which no operation names" in item for item in problems)
    assert any("Contract sample.zz@1, which no operation" in item for item in problems)


def test_variant_arms_lists_and_field_results_are_followed() -> None:
    result = _contract(
        "sample.echo.result",
        {
            "either": {
                "type": {
                    "kind": "variant",
                    "arms": {"a": {"name": "sample.value", "version": 1, "digest": DIGEST}},
                }
            },
            "many": {"type": {"kind": "list", "item": _nested("sample.note"), "max_items": 3}},
            "maybe": {"type": {"kind": "field_result", "value": _nested("sample.row")}},
        },
    )
    pool = [item for item in POOL if item["name"] != "sample.echo.result"] + [result]
    declares = declared_closure([_operation()], pool)
    assert {item.name for item in declares.contracts} >= {
        "sample.value",
        "sample.note",
        "sample.row",
    }


def test_declares_is_sorted_and_lists_each_shape_once() -> None:
    declares = declared_closure([_operation()], POOL, [KIND]).model_dump(mode="json")
    with pytest.raises(ValidationError, match="sorted by name and version"):
        ProviderDeclares.model_validate({**declares, "contracts": declares["contracts"][::-1]})
    with pytest.raises(ValidationError, match="sorted by name and version"):
        ProviderDeclares.model_validate(
            {**declares, "contracts": declares["contracts"] + declares["contracts"][-1:]}
        )
    with pytest.raises(ValidationError):
        ProviderDeclares.model_validate({**declares, "contracts": []})


def test_built_against_is_a_typed_sorted_record() -> None:
    built_against = [
        {"kind": "CaptureKind", "name": "sample.rows", "digest": DIGEST},
        {"kind": "Capability", "name": "sample.echo", "version": 1, "digest": DIGEST},
    ]
    declares = declared_closure([_operation()], POOL, [KIND], built_against)
    assert [item.kind for item in declares.built_against] == ["Capability", "CaptureKind"]
    with pytest.raises(ValidationError):
        declared_closure(
            [_operation()], POOL, [KIND], [{"kind": "Provider", "name": "x", "digest": DIGEST}]
        )


def test_a_carried_contract_is_a_contract_document() -> None:
    declares = declared_closure([_operation()], POOL, [KIND]).model_dump(mode="json")
    first = declares["contracts"][0]
    for broken in (
        {**first, "artifact_format": "playbill-contract-v1"},
        {**first, "version": 2},
        {**first, "fields": {"Bad": {"type": {"kind": "string"}}}},
        {**first, "fields": {"value": {}}},
        {**first, "extra": True},
    ):
        with pytest.raises(ValidationError):
            ProviderDeclares.model_validate({**declares, "contracts": [broken]})


def test_a_layout_needs_a_json_or_csv_decoding() -> None:
    with pytest.raises(ValidationError, match="json or csv decoding"):
        declared_closure(
            [_operation()], POOL, [{**KIND, "decoding": {**KIND["decoding"], "format": "text"}}]
        )


# -- the package file ----------------------------------------------------------------------

MANIFEST = """\
schema_version: 2
provider_id: cruxible-provider-sample
distribution:
  name: cruxible-provider-sample
  version: 0.1.0
entrypoint_group: cruxible.providers
supported_protocol_majors: [2]
implementations:
  - entrypoint: "sample_provider.impl:Echo"
    backends: [local_env]
    requires_extras: []
    declared_endpoints: []
    deterministic: true
"""


def _package(tmp_path: Path) -> Path:
    root = tmp_path / "sample_provider"
    root.mkdir()
    (root / "manifest.yaml").write_text(MANIFEST)
    document = {
        "schema_version": 3,
        "manifest": {"path": "manifest.yaml", "digest": "sha256:" + "0" * 64},
        "operations": [_operation().wire()],
        "declares": {"contracts": []},
        "runtime_requirements": [],
    }
    (root / "registration.json").write_text(json.dumps(document))
    return root


def test_the_writer_fills_declares_and_the_manifest_digest(tmp_path: Path) -> None:
    root = _package(tmp_path)
    assert write_registration(root, contracts=POOL, capture_kinds=[KIND]) is False
    assert write_registration(root, contracts=POOL, capture_kinds=[KIND], check=True) is True
    bundle = load_registration(root)
    expected = "sha256:" + hashlib.sha256(MANIFEST.encode()).hexdigest()
    assert bundle.descriptor.manifest.digest == expected
    assert [item.name for item in bundle.declares.capture_kinds] == ["sample.rows"]
    raw = (root / "registration.json").read_bytes()
    assert raw == render_registration(bundle.descriptor)
    assert raw.endswith(b"}\n")
    assert list(json.loads(raw)) == [
        "schema_version",
        "manifest",
        "operations",
        "declares",
        "runtime_requirements",
    ]
    exported = bundle.export_document()
    assert exported["declares"] == json.loads(raw)["declares"]


def test_check_mode_reports_drift_and_writes_nothing(tmp_path: Path) -> None:
    root = _package(tmp_path)
    write_registration(root, contracts=POOL, capture_kinds=[KIND])
    before = (root / "registration.json").read_bytes()
    pool = [
        item if item["name"] != "sample.note" else {**item, "description": "Reworded."}
        for item in POOL
    ]
    assert write_registration(root, contracts=pool, capture_kinds=[KIND], check=True) is False
    assert (root / "registration.json").read_bytes() == before


def test_loading_refuses_a_registration_whose_declares_is_not_the_closure(tmp_path: Path) -> None:
    root = _package(tmp_path)
    write_registration(root, contracts=POOL, capture_kinds=[KIND])
    document = json.loads((root / "registration.json").read_text())
    document["declares"]["contracts"] = [
        item for item in document["declares"]["contracts"] if item["name"] != "sample.note"
    ]
    (root / "registration.json").write_text(json.dumps(document))
    with pytest.raises(ValueError, match=r"names sample\.note@1, which declares does not carry"):
        load_registration(root)


@pytest.mark.parametrize("version", [1, 2, 4, None])
def test_only_schema_3_is_read(tmp_path: Path, version: int | None) -> None:
    root = _package(tmp_path)
    write_registration(root, contracts=POOL, capture_kinds=[KIND])
    document = json.loads((root / "registration.json").read_text())
    if version is None:
        del document["schema_version"]
    else:
        document["schema_version"] = version
    (root / "registration.json").write_text(json.dumps(document))
    with pytest.raises(ValueError, match="is not read; schema 3"):
        load_registration(root)


def test_the_model_refuses_unknown_top_level_fields() -> None:
    declares = declared_closure([_operation()], POOL, [KIND])
    document = {
        "schema_version": 3,
        "manifest": {"path": "manifest.yaml", "digest": DIGEST},
        "operations": [_operation().wire()],
        "declares": declares.model_dump(mode="json"),
        "interfaces": [],
    }
    with pytest.raises(ValidationError, match="interfaces"):
        PackageRegistration.model_validate(document)
