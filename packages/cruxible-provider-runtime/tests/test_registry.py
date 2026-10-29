"""Stub registry: registration-time checks over a build's pinned operations."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from cruxible_provider_runtime.artifact import (
    DistributionPin,
    LocalEnvBackendPin,
    ProviderArtifactPayload,
)
from cruxible_provider_runtime.errors import HARNESS_REFUSAL_CODES, RefusalCode, RefusalError
from cruxible_provider_runtime.manifest import load_manifest_document, manifest_digest
from cruxible_provider_runtime.operations import ContractRef, ProviderOperation
from cruxible_provider_runtime.registry import StubRegistry, stub_contract_digest

OTHER_DIGEST = "sha256:" + "bb" * 32
DISTRIBUTION_DIGEST = "sha256:" + "cc" * 32
ENTRYPOINT = "sample_provider.impl:Echo"

MANIFEST: dict[str, Any] = {
    "provider_id": "cruxible-provider-sample",
    "distribution": {"name": "cruxible-provider-sample", "version": "0.1.0"},
    "supported_protocol_majors": [2],
    "implementations": [
        {"entrypoint": ENTRYPOINT, "backends": ["local_env"], "deterministic": True}
    ],
}

ECHO = ProviderOperation.model_validate(
    {
        "name": "sample.echo",
        "in": "sample.echo.request@1",
        "out": "sample.echo.result@1",
        "effect": "pure",
        "refusals": {"too_long": {"contract": "sample.echo.refusal.too_long@1"}},
        "entrypoint": ENTRYPOINT,
    }
)
SHOUT = ECHO.model_copy(update={"name": "sample.shout"})


def _payload(manifest_document: dict[str, Any] = MANIFEST) -> ProviderArtifactPayload:
    manifest = load_manifest_document(manifest_document)
    return ProviderArtifactPayload(
        provider_id=manifest.provider_id,
        status="accepted",
        manifest=manifest,
        manifest_digest=manifest_digest(manifest),
        distribution=DistributionPin(
            name=manifest.distribution.name,
            version=manifest.distribution.version,
            filename="sample-0.1.0-py3-none-any.whl",
            sha256=DISTRIBUTION_DIGEST,
            index_url="https://index.example/simple",
            url="https://index.example/simple/sample-0.1.0-py3-none-any.whl",
        ),
        local_env=LocalEnvBackendPin(
            lock_sha256=OTHER_DIGEST, materialization_digests={"linux-cp311": OTHER_DIGEST}
        ),
    )


def test_registration_accepts_a_build_and_pins_every_contract() -> None:
    registry = StubRegistry()
    build = registry.register_provider(_payload(), (ECHO,))
    assert registry.accepted_build("cruxible-provider-sample") is build
    (operation,) = build.operations
    assert operation.pinned
    assert operation.in_.digest == stub_contract_digest(ECHO.in_)
    assert operation.refusals["too_long"].contract.digest == stub_contract_digest(
        ECHO.refusals["too_long"].contract
    )


def test_one_entrypoint_may_serve_several_operations() -> None:
    build = StubRegistry().register_provider(_payload(), (ECHO, SHOUT))
    assert [item.name for item in build.operations] == ["sample.echo", "sample.shout"]
    assert {item.entrypoint for item in build.operations} == {ENTRYPOINT}


def test_an_undeclared_operation_refuses() -> None:
    build = StubRegistry().register_provider(_payload(), (ECHO,))
    with pytest.raises(RefusalError) as exc:
        build.operation("sample.absent")
    assert exc.value.code is RefusalCode.UNDECLARED_OPERATION
    assert exc.value.refusal.detail["declared"] == ["sample.echo"]


def test_an_operation_naming_an_undeclared_entrypoint_refuses() -> None:
    stray = ECHO.model_copy(update={"entrypoint": "sample_provider.impl:Other"})
    with pytest.raises(RefusalError) as exc:
        StubRegistry().register_provider(_payload(), (stray,))
    assert exc.value.code is RefusalCode.MANIFEST_DIVERGENCE


def test_an_implementation_that_serves_nothing_refuses() -> None:
    document = copy.deepcopy(MANIFEST)
    document["implementations"].append(
        {
            "entrypoint": "sample_provider.impl:Idle",
            "backends": ["local_env"],
            "deterministic": True,
        }
    )
    with pytest.raises(RefusalError) as exc:
        StubRegistry().register_provider(_payload(document), (ECHO,))
    assert exc.value.code is RefusalCode.MANIFEST_DIVERGENCE
    assert exc.value.refusal.detail["unserved"] == ["sample_provider.impl:Idle"]


def test_operations_must_be_sorted_and_named_once() -> None:
    with pytest.raises(ValueError, match="sorted"):
        StubRegistry().register_provider(_payload(), (SHOUT, ECHO))
    with pytest.raises(ValueError, match="sorted"):
        StubRegistry().register_provider(_payload(), (ECHO, ECHO))


def test_a_package_pinned_contract_must_agree_with_the_install_base() -> None:
    prepinned = ECHO.model_copy(
        update={"in_": ECHO.in_.model_copy(update={"digest": "sha256:" + "ee" * 32})}
    )
    with pytest.raises(ValueError, match=r"pins sample\.echo\.request@1"):
        StubRegistry().register_provider(_payload(), (prepinned,))


def test_the_build_digest_moves_with_an_operation() -> None:
    """The build a run names covers its operations, not only the runtime artifact."""

    one = StubRegistry().register_provider(_payload(), (ECHO,))
    other = StubRegistry().register_provider(
        _payload(), (ECHO.model_copy(update={"effect": "external_read"}),)
    )
    assert one.build_digest != other.build_digest


def test_the_build_digest_moves_with_a_contract_pin() -> None:
    one = StubRegistry().register_provider(_payload(), (ECHO,))

    def shifted(ref: ContractRef) -> str:
        return OTHER_DIGEST if ref.name == "sample.echo.result" else stub_contract_digest(ref)

    other = StubRegistry().register_provider(_payload(), (ECHO,), contract_digests=shifted)
    assert one.build_digest != other.build_digest
    assert one.operations[0].signature_digest() != other.operations[0].signature_digest()


def test_unknown_provider_refuses() -> None:
    with pytest.raises(RefusalError) as exc:
        StubRegistry().accepted_build("cruxible-provider-absent")
    assert exc.value.code is RefusalCode.UNACCEPTED_PROVIDER


def test_unaccepted_provider_refuses() -> None:
    registry = StubRegistry()
    registry.register_provider(_payload().model_copy(update={"status": "proposed"}), (ECHO,))
    with pytest.raises(RefusalError) as exc:
        registry.accepted_build("cruxible-provider-sample")
    assert exc.value.code is RefusalCode.UNACCEPTED_PROVIDER


def _envelope(code: str) -> object:
    from cruxible_provider_runtime.protocol import PROTOCOL_VERSION, ResultEnvelope

    return ResultEnvelope.model_validate(
        {
            "protocol_version": PROTOCOL_VERSION.render(),
            "run_id": "r",
            "status": "refused",
            "refusal": {"code": code, "message": "m"},
        }
    )


class _Bound:
    def __init__(self, operation: ProviderOperation) -> None:
        self.operation = operation


@pytest.mark.parametrize(
    "code",
    sorted(code.value for code in RefusalCode if code.value not in HARNESS_REFUSAL_CODES),
)
def test_an_envelope_reporting_an_executor_only_condition_is_refused(code: str) -> None:
    """A forged envelope cannot pass a budget breach or a cache failure off as observed."""

    from cruxible_provider_runtime.execute import check_declared_refusal

    with pytest.raises(RefusalError) as exc:
        check_declared_refusal(_Bound(ECHO), _envelope(code))  # type: ignore[arg-type]
    assert exc.value.code is RefusalCode.PROVIDER_PROTOCOL_VIOLATION
    assert exc.value.refusal.detail["refused_code"] == code


def test_harness_and_declared_refusals_pass_and_undeclared_ones_do_not() -> None:
    from cruxible_provider_runtime.execute import check_declared_refusal

    check_declared_refusal(_Bound(ECHO), _envelope("undeclared_operation"))  # type: ignore[arg-type]
    check_declared_refusal(_Bound(ECHO), _envelope("too_long"))  # type: ignore[arg-type]
    with pytest.raises(RefusalError) as exc:
        check_declared_refusal(_Bound(ECHO), _envelope("too_short"))  # type: ignore[arg-type]
    assert exc.value.code is RefusalCode.UNDECLARED_REFUSAL
