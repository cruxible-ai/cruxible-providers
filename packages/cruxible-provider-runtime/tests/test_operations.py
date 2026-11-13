"""Typed operations (registration schema 3): wire shape, serves, source, refusals, signature."""

from __future__ import annotations

import pytest
from cruxible_provider_runtime.errors import RUNTIME_REFUSAL_CODES, RefusalCode
from cruxible_provider_runtime.operations import (
    OPERATION_SIGNATURE_DOMAIN,
    ContractRef,
    ProviderOperation,
    operation_signature_digest,
    pin_operation,
)
from cruxible_provider_runtime.registry import stub_contract_digest
from pydantic import ValidationError

IN_DIGEST = "sha256:" + "1a" * 32
OUT_DIGEST = "sha256:" + "2b" * 32

OPERATION = {
    "name": "sample.echo",
    "description": "Echoes its input back.",
    "source": {
        "path": "packages/sample/src/sample_provider/impl.py",
        "repository": "https://github.com/example/sample",
    },
    "serves": ["sample.echo"],
    "in": "sample.echo.request@1",
    "out": "sample.echo.result@2",
    "effect": "pure",
    "refusals": {
        "too_long": {"contract": "sample.echo.refusal.too_long@1", "absorbable": True},
        "empty": {"contract": "sample.echo.refusal.empty@1"},
    },
    "entrypoint": "sample_provider.impl:Echo",
}


def test_contracts_are_written_name_at_version() -> None:
    operation = ProviderOperation.model_validate(OPERATION)
    assert operation.in_ == ContractRef(name="sample.echo.request", version=1)
    assert operation.out.spelling == "sample.echo.result@2"
    assert operation.refusals["too_long"].absorbable is True
    assert operation.refusals["empty"].absorbable is False
    assert not operation.pinned


def test_the_wire_form_is_the_one_core_reads() -> None:
    """Core's field order, ``in`` by alias, refusals sorted by code, nothing empty emitted."""

    wire = ProviderOperation.model_validate(OPERATION).wire()
    assert list(wire) == [
        "name",
        "description",
        "source",
        "serves",
        "in",
        "out",
        "effect",
        "refusals",
        "entrypoint",
    ]
    assert list(wire["refusals"]) == ["empty", "too_long"]
    assert wire["in"] == {"name": "sample.echo.request", "version": 1}
    bare = ProviderOperation.model_validate({**OPERATION, "refusals": {}}).wire()
    assert "refusals" not in bare
    assert "may_truncate" not in bare
    assert ProviderOperation.model_validate(wire) == ProviderOperation.model_validate(OPERATION)
    truncating = ProviderOperation.model_validate({**OPERATION, "may_truncate": True}).wire()
    assert truncating["may_truncate"] is True


def test_enumerates_is_gone() -> None:
    """Schema 3 withdrew ``enumerates``: a derived kind's layout is the declared enumeration."""

    enumerates = {"capture": "document", "selector": "/rows/*", "fields": {"id": {"at": "id"}}}
    with pytest.raises(ValidationError, match="enumerates"):
        ProviderOperation.model_validate({**OPERATION, "enumerates": enumerates})


@pytest.mark.parametrize("missing", ["description", "source", "serves"])
def test_description_source_and_serves_are_required(missing: str) -> None:
    document = {key: value for key, value in OPERATION.items() if key != missing}
    with pytest.raises(ValidationError, match=missing):
        ProviderOperation.model_validate(document)


@pytest.mark.parametrize("description", ["", " Echoes.", "Echoes. ", "x" * 2049, "Cafe\u0301"])
def test_a_description_is_trimmed_nfc_text_of_bounded_length(description: str) -> None:
    with pytest.raises(ValidationError):
        ProviderOperation.model_validate({**OPERATION, "description": description})


@pytest.mark.parametrize(
    "serves",
    [
        [],
        ["web.search", "web.fetch"],
        ["web.fetch", "web.fetch"],
        ["Web.Fetch"],
        ["web.fetch@1"],
        [f"sample.c{index:02d}" for index in range(17)],
    ],
)
def test_serves_names_capabilities_sorted_once_each_at_most_sixteen(serves: list[str]) -> None:
    with pytest.raises(ValidationError):
        ProviderOperation.model_validate({**OPERATION, "serves": serves})


def test_serves_takes_names_not_versions() -> None:
    serves = [f"sample.c{index:02d}" for index in range(16)]
    operation = ProviderOperation.model_validate({**OPERATION, "serves": serves})
    assert operation.serves == tuple(serves)


@pytest.mark.parametrize("name", ["workspace.file", "workspace.files", "workspace.anything"])
def test_no_package_serves_a_workspace_capability(name: str) -> None:
    """Core's builtin serves ``workspace.`` capabilities; a package claiming one refuses."""

    with pytest.raises(ValidationError, match=f"`{name}` is served by core"):
        ProviderOperation.model_validate({**OPERATION, "serves": [name]})


@pytest.mark.parametrize(
    "source",
    [
        {"path": "/abs/impl.py"},
        {"path": "a/../impl.py"},
        {"path": "./impl.py"},
        {"path": "a\\impl.py"},
        {"path": "impl.py", "repository": "http://github.com/example/sample"},
        {"path": "impl.py", "revision": "a" * 40},
        {"path": "impl.py", "repository": "https://github.com/x/y", "revision": "abc123"},
        {"path": "impl.py", "repository": "https://github.com/x/y", "revision": "A" * 40},
        {"path": "impl.py", "line": 3},
    ],
)
def test_a_source_is_a_relative_path_with_an_https_repository_and_full_revision(
    source: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        ProviderOperation.model_validate({**OPERATION, "source": source})


def test_a_local_build_names_only_its_path_and_a_revision_is_a_full_commit_id() -> None:
    local = ProviderOperation.model_validate({**OPERATION, "source": {"path": "impl.py"}})
    assert local.wire()["source"] == {"path": "impl.py"}
    for revision in ("a" * 40, "b" * 64):
        source = {"path": "impl.py", "repository": "https://github.com/x/y", "revision": revision}
        assert ProviderOperation.model_validate({**OPERATION, "source": source}).source.revision


@pytest.mark.parametrize("entrypoint", ["sample", "sample:", "sample.impl:Echo.x", "1sample:Echo"])
def test_an_entrypoint_is_module_colon_callable(entrypoint: str) -> None:
    with pytest.raises(ValidationError):
        ProviderOperation.model_validate({**OPERATION, "entrypoint": entrypoint})


@pytest.mark.parametrize("spelling", ["sample.echo.request", "sample@one", "Sample@1", "@1"])
def test_a_contract_pin_that_is_not_name_at_n_refuses(spelling: str) -> None:
    with pytest.raises(ValidationError):
        ProviderOperation.model_validate({**OPERATION, "in": spelling})


@pytest.mark.parametrize("code", sorted(RUNTIME_REFUSAL_CODES))
def test_an_operation_may_not_declare_a_runtime_code(code: str) -> None:
    refusals = {code: {"contract": "sample.echo.refusal.x@1"}}
    with pytest.raises(ValidationError, match="reserved by the runtime"):
        ProviderOperation.model_validate({**OPERATION, "refusals": refusals})


def test_there_is_no_shared_provider_decline_vocabulary() -> None:
    """The 1.x decline codes are operation vocabulary now, not the runtime's."""

    for code in ("provider_declined", "invalid_parameter", "mismatched_lengths", "unknown_method"):
        assert code not in RUNTIME_REFUSAL_CODES
    for removed in ("unclaimed_bucket", "unclassified_input", "undeclared_interface"):
        assert removed not in {item.value for item in RefusalCode}


@pytest.mark.parametrize("effect", ["read", "mutating", ""])
def test_the_effect_is_one_of_three(effect: str) -> None:
    with pytest.raises(ValidationError):
        ProviderOperation.model_validate({**OPERATION, "effect": effect})


def test_an_unpinned_operation_has_no_signature() -> None:
    with pytest.raises(ValueError, match="not pinned"):
        ProviderOperation.model_validate(OPERATION).signature_digest()


def test_pinning_fills_every_contract() -> None:
    pinned = pin_operation(ProviderOperation.model_validate(OPERATION), stub_contract_digest)
    assert pinned.pinned
    assert pinned.signature_digest() == operation_signature_digest(
        in_digest=stub_contract_digest(pinned.in_),
        out_digest=stub_contract_digest(pinned.out),
        effect="pure",
    )


def test_the_signature_digest_golden() -> None:
    """Core's ``operation_signature_digest`` over the same three values.

    The cross-check against core's own function runs in the core integration
    lane (tests/test_package_registration.py); this golden pins the spelling.
    """

    assert OPERATION_SIGNATURE_DOMAIN == "cruxible-operation-signature-v1"
    assert operation_signature_digest(
        in_digest=IN_DIGEST, out_digest=OUT_DIGEST, effect="external_read"
    ) == ("sha256:82688773bd6b4b4fbf681837c4884aeb37d56acc7a93b8f8134c38e49408dc19")


def test_the_signature_moves_with_each_of_its_three_parts() -> None:
    base = operation_signature_digest(in_digest=IN_DIGEST, out_digest=OUT_DIGEST, effect="pure")
    assert base != operation_signature_digest(
        in_digest=OUT_DIGEST, out_digest=OUT_DIGEST, effect="pure"
    )
    assert base != operation_signature_digest(
        in_digest=IN_DIGEST, out_digest=IN_DIGEST, effect="pure"
    )
    assert base != operation_signature_digest(
        in_digest=IN_DIGEST, out_digest=OUT_DIGEST, effect="external_read"
    )
