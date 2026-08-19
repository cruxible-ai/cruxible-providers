"""Stub registry: registration-time checks and measured bucket admission."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

import pytest
from cruxible_provider_runtime.artifact import (
    DistributionPin,
    LocalEnvBackendPin,
    ProviderArtifactPayload,
)
from cruxible_provider_runtime.buckets import BucketClass, BucketDimension, BucketVocabulary
from cruxible_provider_runtime.errors import RefusalCode, RefusalError
from cruxible_provider_runtime.manifest import load_manifest_document, manifest_digest
from cruxible_provider_runtime.registry import InterfaceRegistration, StubRegistry

INTERFACE_DIGEST = "sha256:" + "aa" * 32
OTHER_DIGEST = "sha256:" + "bb" * 32
DISTRIBUTION_DIGEST = "sha256:" + "cc" * 32

VOCABULARY = BucketVocabulary(
    interface_id="sample.echo",
    dimensions=(
        BucketDimension(
            name="size",
            description="input size",
            classes=(
                BucketClass(id="small", description="at most 10 characters"),
                BucketClass(id="large", description="more than 10 characters"),
            ),
        ),
    ),
)


def classify(payload: Mapping[str, Any]) -> Mapping[str, str] | None:
    text = payload.get("text")
    if not isinstance(text, str):
        return None
    return {"size": "small" if len(text) <= 10 else "large"}


MANIFEST: dict[str, Any] = {
    "provider_id": "cruxible-provider-sample",
    "distribution": {"name": "cruxible-provider-sample", "version": "0.1.0"},
    "supported_protocol_majors": [1],
    "implementations": [
        {
            "interface_id": "sample.echo",
            "interface_digest": INTERFACE_DIGEST,
            "entrypoint": "sample_provider.impl:Echo",
            "backends": ["local_env"],
            "declared_input_buckets": ["size=small"],
            "bucket_conformance": {"size=small": "fixture-small"},
            "deterministic": True,
            "side_effects": False,
        }
    ],
}


def _registry() -> StubRegistry:
    registry = StubRegistry()
    registry.register_interface(
        InterfaceRegistration(
            interface_id="sample.echo",
            interface_digest=INTERFACE_DIGEST,
            bucket_vocabulary=VOCABULARY,
            classifier=classify,
        )
    )
    return registry


def _payload(manifest_document: dict[str, Any]) -> ProviderArtifactPayload:
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


def test_registration_accepts_a_well_formed_provider() -> None:
    registry = _registry()
    registry.register_provider(_payload(MANIFEST))
    assert registry.accepted_provider("cruxible-provider-sample").provider_id == (
        "cruxible-provider-sample"
    )


def test_unknown_interface_refuses() -> None:
    with pytest.raises(RefusalError) as exc:
        StubRegistry().interface("sample.absent")
    assert exc.value.code is RefusalCode.UNKNOWN_INTERFACE


def test_interface_digest_mismatch_refuses() -> None:
    with pytest.raises(RefusalError) as exc:
        _registry().interface("sample.echo", OTHER_DIGEST)
    assert exc.value.code is RefusalCode.INTERFACE_DIGEST_MISMATCH


def test_claimed_bucket_without_a_fixture_refuses_at_registration() -> None:
    document = copy.deepcopy(MANIFEST)
    document["implementations"][0]["bucket_conformance"] = {}
    with pytest.raises(RefusalError) as exc:
        _registry().register_provider(_payload(document))
    assert exc.value.code is RefusalCode.BUCKET_FIXTURE_MISSING


def test_fixture_for_an_unclaimed_bucket_refuses_at_registration() -> None:
    document = copy.deepcopy(MANIFEST)
    document["implementations"][0]["bucket_conformance"]["size=large"] = "fixture-large"
    with pytest.raises(RefusalError) as exc:
        _registry().register_provider(_payload(document))
    assert exc.value.code is RefusalCode.BUCKET_FIXTURE_MISSING


def test_unaccepted_provider_refuses() -> None:
    registry = _registry()
    payload = _payload(MANIFEST)
    registry.providers[payload.provider_id] = payload.model_copy(update={"status": "proposed"})
    with pytest.raises(RefusalError) as exc:
        registry.accepted_provider(payload.provider_id)
    assert exc.value.code is RefusalCode.UNACCEPTED_PROVIDER


def test_bucket_is_derived_from_the_input_not_the_manifest() -> None:
    registry = _registry()
    assert registry.classify("sample.echo", {"text": "short"}) == "size=small"
    assert registry.classify("sample.echo", {"text": "x" * 40}) == "size=large"


def test_unclaimed_bucket_refuses_at_admission() -> None:
    registry = _registry()
    with pytest.raises(RefusalError) as exc:
        registry.admit("sample.echo", ("size=small",), {"text": "x" * 40})
    assert exc.value.code is RefusalCode.UNCLAIMED_BUCKET
    assert exc.value.refusal.detail["bucket"] == "size=large"


def test_unclassifiable_input_refuses() -> None:
    registry = _registry()
    with pytest.raises(RefusalError) as exc:
        registry.admit("sample.echo", ("size=small",), {"not_text": 1})
    assert exc.value.code is RefusalCode.UNCLASSIFIED_INPUT


def test_vocabulary_must_belong_to_its_interface() -> None:
    with pytest.raises(ValueError, match="belongs to interface"):
        InterfaceRegistration(
            interface_id="other.slot",
            interface_digest=INTERFACE_DIGEST,
            bucket_vocabulary=VOCABULARY,
            classifier=classify,
        )
