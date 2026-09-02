"""Bind-time refusal paths, each one a rule the RP-0 contract states.

The built-in inherits the whole conformance suite rather than trusting that the
reference provider exercised these paths.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from cruxible_provider_runtime.artifact import ProviderArtifactPayload
from cruxible_provider_runtime.backends import ContainerBackend, LocalEnvBackend
from cruxible_provider_runtime.binding import BindRequest, bind
from cruxible_provider_runtime.errors import RefusalCode, RefusalError
from cruxible_provider_runtime.manifest import BackendKind, load_manifest, manifest_digest
from cruxible_provider_runtime.registry import StubRegistry
from cruxible_provider_runtime.testing import FakeContainerDriver
from cruxible_provider_workspace.interface import registration

from .conftest import INTERFACE_ID, MARKER_ENVIRONMENT, PROVIDER_ID


def _request(
    manifest_path: Path,
    lock_path: Path | None,
    backend_kind: BackendKind = "local_env",
    **overrides: Any,
) -> BindRequest:
    fields: dict[str, Any] = {
        "provider_id": PROVIDER_ID,
        "interface_id": INTERFACE_ID,
        "backend_kind": backend_kind,
        "manifest_path": manifest_path,
        "lock_path": lock_path,
        "marker_environment": MARKER_ENVIRONMENT,
        "allow_editable_dev_sources": True,
    }
    fields.update(overrides)
    return BindRequest(**fields)


def _edited(manifest_path: Path, tmp_path: Path, edit: Any) -> Path:
    document = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    edit(document)
    target = tmp_path / "manifest.yaml"
    target.write_text(yaml.safe_dump(document), encoding="utf-8")
    return target


def _registry_for(payload: ProviderArtifactPayload) -> StubRegistry:
    stub = StubRegistry()
    stub.register_interface(registration())
    stub.register_provider(payload)
    return stub


def _reaccepted(
    accepted_artifact: ProviderArtifactPayload, manifest_path: Path
) -> ProviderArtifactPayload:
    edited = load_manifest(manifest_path)
    return accepted_artifact.model_copy(
        update={"manifest": edited, "manifest_digest": manifest_digest(edited)}
    )


def test_unaccepted_provider_refuses(
    manifest_path: Path, lock_path: Path, local_backend: LocalEnvBackend
) -> None:
    with pytest.raises(RefusalError) as exc:
        bind(StubRegistry(), _request(manifest_path, lock_path), local_backend=local_backend)
    assert exc.value.code is RefusalCode.UNACCEPTED_PROVIDER


def test_manifest_divergence_refuses(
    registry: StubRegistry,
    manifest_path: Path,
    lock_path: Path,
    tmp_path: Path,
    local_backend: LocalEnvBackend,
) -> None:
    """A package-side manifest edited after acceptance is not authority."""

    def edit(document: dict[str, Any]) -> None:
        document["implementations"][0]["side_effects"] = True

    edited = _edited(manifest_path, tmp_path, edit)
    with pytest.raises(RefusalError) as exc:
        bind(registry, _request(edited, lock_path), local_backend=local_backend)
    assert exc.value.code is RefusalCode.MANIFEST_DIVERGENCE


def test_unknown_manifest_field_refuses(
    registry: StubRegistry,
    manifest_path: Path,
    lock_path: Path,
    tmp_path: Path,
    local_backend: LocalEnvBackend,
) -> None:
    def edit(document: dict[str, Any]) -> None:
        document["implementations"][0]["effect_class"] = "pure"

    edited = _edited(manifest_path, tmp_path, edit)
    with pytest.raises(RefusalError) as exc:
        bind(registry, _request(edited, lock_path), local_backend=local_backend)
    assert exc.value.code is RefusalCode.UNKNOWN_MANIFEST_FIELD


def test_undeclared_interface_refuses(
    registry: StubRegistry, manifest_path: Path, lock_path: Path, local_backend: LocalEnvBackend
) -> None:
    with pytest.raises(RefusalError) as exc:
        bind(
            registry,
            _request(manifest_path, lock_path, interface_id="db.row_select"),
            local_backend=local_backend,
        )
    assert exc.value.code is RefusalCode.UNDECLARED_INTERFACE


def test_interface_digest_mismatch_refuses(
    accepted_artifact: ProviderArtifactPayload,
    manifest_path: Path,
    lock_path: Path,
    tmp_path: Path,
    local_backend: LocalEnvBackend,
) -> None:
    """A manifest pinning a different interface digest cannot bind to this slot."""

    def edit(document: dict[str, Any]) -> None:
        document["implementations"][0]["interface_digest"] = "sha256:" + "ab" * 32

    edited = _edited(manifest_path, tmp_path, edit)
    payload = _reaccepted(accepted_artifact, edited)
    stub = StubRegistry()
    stub.register_interface(registration())
    stub.providers[payload.provider_id] = payload
    with pytest.raises(RefusalError) as exc:
        bind(stub, _request(edited, lock_path), local_backend=local_backend)
    assert exc.value.code is RefusalCode.INTERFACE_DIGEST_MISMATCH


def test_unsupported_protocol_refuses(
    accepted_artifact: ProviderArtifactPayload,
    manifest_path: Path,
    lock_path: Path,
    tmp_path: Path,
    local_backend: LocalEnvBackend,
) -> None:
    def edit(document: dict[str, Any]) -> None:
        document["supported_protocol_majors"] = [2]

    edited = _edited(manifest_path, tmp_path, edit)
    registry = _registry_for(_reaccepted(accepted_artifact, edited))
    with pytest.raises(RefusalError) as exc:
        bind(registry, _request(edited, lock_path), local_backend=local_backend)
    assert exc.value.code is RefusalCode.UNSUPPORTED_PROTOCOL


def test_unsupported_backend_refuses(
    accepted_artifact: ProviderArtifactPayload,
    manifest_path: Path,
    lock_path: Path,
    tmp_path: Path,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> None:
    def edit(document: dict[str, Any]) -> None:
        document["implementations"][0]["backends"] = ["local_env"]

    edited = _edited(manifest_path, tmp_path, edit)
    payload = _reaccepted(accepted_artifact, edited).model_copy(update={"container": None})
    registry = _registry_for(payload)
    with pytest.raises(RefusalError) as exc:
        bind(
            registry,
            _request(edited, lock_path, backend_kind="container"),
            local_backend=local_backend,
            container_backend=container_backend,
        )
    assert exc.value.code is RefusalCode.UNSUPPORTED_BACKEND


def test_bucket_fixture_missing_refuses_at_registration(
    accepted_artifact: ProviderArtifactPayload, manifest_path: Path, tmp_path: Path
) -> None:
    def edit(document: dict[str, Any]) -> None:
        document["implementations"][0]["bucket_conformance"] = {}

    edited = _edited(manifest_path, tmp_path, edit)
    payload = _reaccepted(accepted_artifact, edited)
    stub = StubRegistry()
    stub.register_interface(registration())
    with pytest.raises(RefusalError) as exc:
        stub.register_provider(payload)
    assert exc.value.code is RefusalCode.BUCKET_FIXTURE_MISSING


def test_claiming_the_large_bucket_without_a_fixture_refuses_at_registration(
    accepted_artifact: ProviderArtifactPayload, manifest_path: Path, tmp_path: Path
) -> None:
    """The unclaimed class stays unclaimed until a fixture exists for it."""

    def edit(document: dict[str, Any]) -> None:
        document["implementations"][0]["declared_input_buckets"].append(
            "content_kind=text;byte_size=large"
        )

    edited = _edited(manifest_path, tmp_path, edit)
    stub = StubRegistry()
    stub.register_interface(registration())
    with pytest.raises(RefusalError) as exc:
        stub.register_provider(_reaccepted(accepted_artifact, edited))
    assert exc.value.code is RefusalCode.BUCKET_FIXTURE_MISSING
    assert exc.value.refusal.detail["bucket"] == "content_kind=text;byte_size=large"


def test_a_lock_that_is_not_the_pinned_one_refuses_on_its_bytes(
    registry: StubRegistry,
    manifest_path: Path,
    lock_path: Path,
    tmp_path: Path,
    local_backend: LocalEnvBackend,
) -> None:
    reformatted = tmp_path / "uv.lock"
    reformatted.write_text(
        "# a comment a future uv release might add\n" + lock_path.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    with pytest.raises(RefusalError) as exc:
        bind(registry, _request(manifest_path, reformatted), local_backend=local_backend)
    assert exc.value.code is RefusalCode.LOCK_BYTES_MISMATCH


def test_missing_lock_refuses_rather_than_resolving_freely(
    registry: StubRegistry, manifest_path: Path, local_backend: LocalEnvBackend
) -> None:
    with pytest.raises(RefusalError) as exc:
        bind(registry, _request(manifest_path, None), local_backend=local_backend)
    assert exc.value.code is RefusalCode.LOCK_MISMATCH


def test_unpinned_marker_environment_refuses(
    registry: StubRegistry,
    manifest_path: Path,
    lock_path: Path,
    local_backend: LocalEnvBackend,
) -> None:
    other = MARKER_ENVIRONMENT.model_copy(update={"id": "solaris-cp311"})
    with pytest.raises(RefusalError) as exc:
        bind(
            registry,
            _request(manifest_path, lock_path, marker_environment=other),
            local_backend=local_backend,
        )
    assert exc.value.code is RefusalCode.LOCK_MISMATCH


def test_image_provenance_mismatch_refuses(
    registry: StubRegistry,
    accepted_artifact: ProviderArtifactPayload,
    manifest_path: Path,
    lock_path: Path,
    local_backend: LocalEnvBackend,
) -> None:
    assert accepted_artifact.container is not None
    lying = accepted_artifact.container.provenance.model_copy(
        update={"base_image_digest": "sha256:" + "de" * 32}
    )
    with pytest.raises(RefusalError) as exc:
        bind(
            registry,
            _request(manifest_path, lock_path, backend_kind="container"),
            local_backend=local_backend,
            container_backend=ContainerBackend(FakeContainerDriver(provenance=lying)),
        )
    assert exc.value.code is RefusalCode.IMAGE_PROVENANCE_MISMATCH
