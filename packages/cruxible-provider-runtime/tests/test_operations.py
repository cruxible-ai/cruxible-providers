"""Typed operations: the registration's wire shape, refusal codes and signature digest."""

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
    """``in`` by alias, refusals sorted by code, nothing empty emitted."""

    wire = ProviderOperation.model_validate(OPERATION).wire()
    assert list(wire["refusals"]) == ["empty", "too_long"]
    assert wire["in"] == {"name": "sample.echo.request", "version": 1}
    assert "enumerates" not in wire
    bare = ProviderOperation.model_validate({**OPERATION, "refusals": {}}).wire()
    assert "refusals" not in bare
    assert ProviderOperation.model_validate(wire) == ProviderOperation.model_validate(OPERATION)


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
