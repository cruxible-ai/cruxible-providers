"""Manifest validation and the manifest-is-never-authority rule."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml
from cruxible_provider_runtime.artifact import (
    DistributionPin,
    LocalEnvBackendPin,
    ProviderArtifactPayload,
    artifact_digest,
    load_provider_artifact,
)
from cruxible_provider_runtime.errors import RefusalCode, RefusalError
from cruxible_provider_runtime.manifest import (
    ProviderManifest,
    load_manifest,
    load_manifest_document,
    manifest_digest,
)

DIGEST_ONE = "sha256:" + "11" * 32
DIGEST_TWO = "sha256:" + "22" * 32

MANIFEST: dict[str, Any] = {
    "schema_version": 1,
    "provider_id": "cruxible-provider-sample",
    "distribution": {"name": "cruxible-provider-sample", "version": "0.1.0"},
    "entrypoint_group": "cruxible.providers",
    "supported_protocol_majors": [1],
    "implementations": [
        {
            "interface_id": "sample.echo",
            "interface_digest": DIGEST_ONE,
            "entrypoint": "sample_provider.impl:Echo",
            "backends": ["local_env"],
            "declared_input_buckets": ["size=small"],
            "bucket_conformance": {"size=small": "fixture-small"},
            "declared_endpoints": [],
            "capture_contract_families": ["sample.capture.v1"],
            "deterministic": True,
            "side_effects": False,
        }
    ],
}


def _write(tmp_path: Path, document: dict[str, Any], name: str = "manifest.yaml") -> Path:
    path = tmp_path / name
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return path


def test_manifest_loads(tmp_path: Path) -> None:
    manifest = load_manifest(_write(tmp_path, MANIFEST))
    assert manifest.provider_id == "cruxible-provider-sample"
    assert manifest.implementation("sample.echo").deterministic is True


def test_unknown_manifest_field_fails_closed(tmp_path: Path) -> None:
    document = copy.deepcopy(MANIFEST)
    document["experimental_flag"] = True
    with pytest.raises(RefusalError) as exc:
        load_manifest(_write(tmp_path, document))
    assert exc.value.code is RefusalCode.UNKNOWN_MANIFEST_FIELD
    assert exc.value.refusal.detail["fields"] == ["experimental_flag"]


def test_unknown_field_inside_an_implementation_fails_closed(tmp_path: Path) -> None:
    document = copy.deepcopy(MANIFEST)
    document["implementations"][0]["priority"] = 3
    with pytest.raises(RefusalError) as exc:
        load_manifest(_write(tmp_path, document))
    assert exc.value.code is RefusalCode.UNKNOWN_MANIFEST_FIELD


def test_undeclared_interface_refuses() -> None:
    manifest = load_manifest_document(MANIFEST)
    with pytest.raises(RefusalError) as exc:
        manifest.implementation("sample.absent")
    assert exc.value.code is RefusalCode.UNDECLARED_INTERFACE


def test_manifest_digest_is_stable_under_key_order() -> None:
    shuffled = {key: MANIFEST[key] for key in reversed(list(MANIFEST))}
    assert manifest_digest(load_manifest_document(MANIFEST)) == manifest_digest(
        load_manifest_document(shuffled)
    )


def test_manifest_digest_moves_when_an_entrypoint_moves() -> None:
    changed = copy.deepcopy(MANIFEST)
    changed["implementations"][0]["entrypoint"] = "sample_provider.impl:EchoFast"
    assert manifest_digest(load_manifest_document(MANIFEST)) != manifest_digest(
        load_manifest_document(changed)
    )


def _artifact(manifest: ProviderManifest, **overrides: Any) -> ProviderArtifactPayload:
    document: dict[str, Any] = {
        "provider_id": manifest.provider_id,
        "status": "accepted",
        "manifest": manifest,
        "manifest_digest": manifest_digest(manifest),
        "distribution": DistributionPin(
            name=manifest.distribution.name,
            version=manifest.distribution.version,
            filename="cruxible_provider_sample-0.1.0-py3-none-any.whl",
            sha256=DIGEST_TWO,
            index_url="https://index.example/simple",
            url="https://index.example/simple/x.whl",
        ),
        "local_env": LocalEnvBackendPin(
            lock_sha256=DIGEST_ONE, materialization_digests={"linux-cp311": DIGEST_TWO}
        ),
    }
    document.update(overrides)
    return ProviderArtifactPayload.model_validate(document)


def test_artifact_self_consistency_holds() -> None:
    payload = _artifact(load_manifest_document(MANIFEST))
    payload.check_self_consistency()


def test_artifact_with_a_stale_manifest_digest_refuses() -> None:
    manifest = load_manifest_document(MANIFEST)
    with pytest.raises(RefusalError) as exc:
        _artifact(manifest, manifest_digest=DIGEST_ONE).check_self_consistency()
    assert exc.value.code is RefusalCode.MANIFEST_DIVERGENCE


def test_artifact_missing_a_declared_backend_pin_refuses() -> None:
    manifest = load_manifest_document(MANIFEST)
    with pytest.raises(RefusalError) as exc:
        _artifact(manifest, local_env=None).check_self_consistency()
    assert exc.value.code is RefusalCode.MANIFEST_DIVERGENCE


def test_artifact_digest_ignores_acceptance_status() -> None:
    manifest = load_manifest_document(MANIFEST)
    accepted = _artifact(manifest, status="accepted")
    proposed = _artifact(manifest, status="proposed")
    assert artifact_digest(accepted) == artifact_digest(proposed)


def test_artifact_round_trips_through_yaml(tmp_path: Path) -> None:
    payload = _artifact(load_manifest_document(MANIFEST))
    path = tmp_path / "providers" / "cruxible-provider-sample.yaml"
    path.parent.mkdir()
    path.write_text(payload.model_dump_json(indent=2), encoding="utf-8")
    assert load_provider_artifact(path) == payload


def test_artifact_with_an_unknown_manifest_field_refuses_with_the_manifest_code(
    tmp_path: Path,
) -> None:
    payload = _artifact(load_manifest_document(MANIFEST))
    document = yaml.safe_load(payload.model_dump_json())
    document["manifest"]["experimental_flag"] = True
    path = tmp_path / "artifact.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    with pytest.raises(RefusalError) as exc:
        load_provider_artifact(path)
    assert exc.value.code is RefusalCode.UNKNOWN_MANIFEST_FIELD


def test_two_implementations_of_one_interface_is_terminal() -> None:
    """RP-0 has no tie-break, and inventing one would make ordering decide."""

    document = copy.deepcopy(MANIFEST)
    second = copy.deepcopy(document["implementations"][0])
    second["entrypoint"] = "sample_provider.impl:EchoFast"
    document["implementations"].append(second)
    manifest = load_manifest_document(document)
    with pytest.raises(RefusalError) as exc:
        manifest.implementation("sample.echo")
    assert exc.value.code is RefusalCode.AMBIGUOUS_IMPLEMENTATION
    assert "terminal" in exc.value.refusal.message
    assert exc.value.refusal.detail["entrypoints"] == [
        "sample_provider.impl:Echo",
        "sample_provider.impl:EchoFast",
    ]


def test_an_absent_interface_is_a_different_refusal_from_an_ambiguous_one() -> None:
    """Over-declaration and non-declaration are different faults."""

    manifest = load_manifest_document(MANIFEST)
    with pytest.raises(RefusalError) as exc:
        manifest.implementation("sample.absent")
    assert exc.value.code is RefusalCode.UNDECLARED_INTERFACE


def test_a_manifest_may_require_per_engine_extras() -> None:
    """Heavy engines live behind extras; the implementation names the ones it needs."""

    document = copy.deepcopy(MANIFEST)
    document["implementations"][0]["requires_extras"] = ["docling"]
    manifest = load_manifest_document(document)
    assert manifest.implementation("sample.echo").requires_extras == ("docling",)


def test_an_implementation_requires_no_extras_by_default() -> None:
    assert load_manifest_document(MANIFEST).implementation("sample.echo").requires_extras == ()


def test_a_repeated_extra_refuses() -> None:
    document = copy.deepcopy(MANIFEST)
    document["implementations"][0]["requires_extras"] = ["docling", "docling"]
    with pytest.raises(RefusalError) as exc:
        load_manifest_document(document)
    assert exc.value.code is RefusalCode.UNKNOWN_MANIFEST_FIELD


def test_the_dynamic_endpoint_form_is_a_legal_declaration() -> None:
    """EXPERIMENTAL, and deliberately the only one."""

    document = copy.deepcopy(MANIFEST)
    document["implementations"][0]["declared_endpoints"] = ["dynamic:target-from-run-input"]
    manifest = load_manifest_document(document)
    assert manifest.implementation("sample.echo").declared_endpoints == (
        "dynamic:target-from-run-input",
    )


def test_an_unknown_dynamic_endpoint_form_refuses() -> None:
    """A declaration nobody can interpret must not be read as a hostname.

    Without this, ``dynamic:whatever-comes-next`` normalises to a host called
    ``dynamic`` and becomes an allowlist entry that quietly matches nothing.
    """

    document = copy.deepcopy(MANIFEST)
    document["implementations"][0]["declared_endpoints"] = ["dynamic:whatever-comes-next"]
    with pytest.raises(RefusalError) as exc:
        load_manifest_document(document)
    assert exc.value.code is RefusalCode.UNKNOWN_MANIFEST_FIELD


def test_a_declared_endpoint_that_is_not_an_endpoint_refuses() -> None:
    document = copy.deepcopy(MANIFEST)
    document["implementations"][0]["declared_endpoints"] = ["https:///no-host"]
    with pytest.raises(RefusalError) as exc:
        load_manifest_document(document)
    assert exc.value.code is RefusalCode.UNKNOWN_MANIFEST_FIELD
