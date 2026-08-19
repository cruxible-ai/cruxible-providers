"""Bind-time refusal paths, each one a rule the RP-0 contract states."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml
from cruxible_provider_runtime.artifact import ProviderArtifactPayload
from cruxible_provider_runtime.backends import ContainerBackend, LocalEnvBackend, UvSyncBuilder
from cruxible_provider_runtime.binding import BindRequest, bind
from cruxible_provider_runtime.cache import MaterializationCache
from cruxible_provider_runtime.errors import RefusalCode, RefusalError
from cruxible_provider_runtime.index import ArtifactFetcher, IndexConfig
from cruxible_provider_runtime.manifest import BackendKind
from cruxible_provider_runtime.registry import StubRegistry
from cruxible_provider_runtime.resolution import ResolvedSet
from cruxible_provider_runtime.testing import FakeContainerDriver, FakeIndexTransport

from .conftest import MARKER_ENVIRONMENT


def _request(
    manifest_path: Path,
    lock_path: Path | None,
    backend_kind: BackendKind = "local_env",
    **overrides: Any,
) -> BindRequest:
    fields: dict[str, Any] = {
        "provider_id": "cruxible-provider-noop",
        "interface_id": "noop.echo",
        "backend_kind": backend_kind,
        "manifest_path": manifest_path,
        "lock_path": lock_path,
        "marker_environment": MARKER_ENVIRONMENT,
        "allow_editable_dev_sources": True,
    }
    fields.update(overrides)
    return BindRequest(**fields)


def _edited_manifest(manifest_path: Path, tmp_path: Path, edit: Any) -> Path:
    document = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    edit(document)
    target = tmp_path / "manifest.yaml"
    target.write_text(yaml.safe_dump(document), encoding="utf-8")
    return target


def test_unaccepted_provider_refuses(
    manifest_path: Path, lock_path: Path, local_backend: LocalEnvBackend
) -> None:
    with pytest.raises(RefusalError) as exc:
        bind(StubRegistry(), _request(manifest_path, lock_path), local_backend=local_backend)
    assert exc.value.code is RefusalCode.UNACCEPTED_PROVIDER


def test_a_lock_that_is_not_the_pinned_one_refuses_on_its_bytes(
    registry: StubRegistry,
    manifest_path: Path,
    tampered_lock: Path,
    local_backend: LocalEnvBackend,
) -> None:
    """Cheap tamper-evidence over the exact file that was reviewed."""

    with pytest.raises(RefusalError) as exc:
        bind(registry, _request(manifest_path, tampered_lock), local_backend=local_backend)
    assert exc.value.code is RefusalCode.LOCK_BYTES_MISMATCH


def test_a_lock_that_resolves_away_from_the_pin_refuses_on_its_resolution(
    accepted_artifact: ProviderArtifactPayload,
    manifest_path: Path,
    tampered_lock: Path,
    local_backend: LocalEnvBackend,
) -> None:
    """The primary gate, isolated from the byte gate.

    The artifact is re-pinned to the tampered lock's *bytes* so that the byte
    check passes, leaving the resolution comparison as the only thing that can
    catch the substituted artifact hash. It does.
    """

    from cruxible_provider_runtime.resolution import load_uv_lock

    assert accepted_artifact.local_env is not None
    repinned = accepted_artifact.model_copy(
        update={
            "local_env": accepted_artifact.local_env.model_copy(
                update={"lock_sha256": load_uv_lock(tampered_lock).lock_sha256}
            )
        }
    )
    registry = StubRegistry()
    from cruxible_provider_noop.interface import registration

    registry.register_interface(registration())
    registry.register_provider(repinned)

    with pytest.raises(RefusalError) as exc:
        bind(registry, _request(manifest_path, tampered_lock), local_backend=local_backend)
    assert exc.value.code is RefusalCode.LOCK_MISMATCH
    assert exc.value.refusal.detail["computed"] != exc.value.refusal.detail["pinned"]


def test_formatting_churn_refuses_on_bytes_but_does_not_move_the_resolution(
    registry: StubRegistry,
    accepted_artifact: ProviderArtifactPayload,
    manifest_path: Path,
    reformatted_lock: Path,
    lock_path: Path,
    local_backend: LocalEnvBackend,
) -> None:
    """Both halves of the two-lock-checks argument, in one test.

    A reformatted lock refuses: the accepted artifact pinned a specific file and
    nobody approved a different one, so re-accepting is a governance step. But
    identity is still not keyed on lock bytes — the resolution, and therefore the
    materialization digest, is byte-for-byte unchanged by the reformatting.
    """

    from cruxible_provider_runtime.digests import materialization_digest
    from cruxible_provider_runtime.resolution import load_uv_lock, resolve

    from .conftest import DISTRIBUTION_SHA256

    with pytest.raises(RefusalError) as exc:
        bind(registry, _request(manifest_path, reformatted_lock), local_backend=local_backend)
    assert exc.value.code is RefusalCode.LOCK_BYTES_MISMATCH

    def pin(path: Path) -> str:
        resolution = resolve(
            load_uv_lock(path),
            "cruxible-provider-noop",
            MARKER_ENVIRONMENT,
            allow_editable_dev_sources=True,
        )
        return materialization_digest(resolution, distribution_sha256=DISTRIBUTION_SHA256)

    assert pin(reformatted_lock) == pin(lock_path)
    assert accepted_artifact.local_env is not None
    assert (
        pin(lock_path) == accepted_artifact.local_env.materialization_digests[MARKER_ENVIRONMENT.id]
    )


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


def test_manifest_divergence_refuses(
    registry: StubRegistry,
    manifest_path: Path,
    lock_path: Path,
    tmp_path: Path,
    local_backend: LocalEnvBackend,
) -> None:
    """A package-side manifest edited after acceptance is not authority."""

    def edit(document: dict[str, Any]) -> None:
        document["implementations"][0]["deterministic"] = False

    edited = _edited_manifest(manifest_path, tmp_path, edit)
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
        document["priority"] = 10

    edited = _edited_manifest(manifest_path, tmp_path, edit)
    with pytest.raises(RefusalError) as exc:
        bind(registry, _request(edited, lock_path), local_backend=local_backend)
    assert exc.value.code is RefusalCode.UNKNOWN_MANIFEST_FIELD


def test_undeclared_interface_refuses(
    registry: StubRegistry,
    manifest_path: Path,
    lock_path: Path,
    local_backend: LocalEnvBackend,
) -> None:
    with pytest.raises(RefusalError) as exc:
        bind(
            registry,
            _request(manifest_path, lock_path, interface_id="noop.absent"),
            local_backend=local_backend,
        )
    assert exc.value.code is RefusalCode.UNDECLARED_INTERFACE


def test_unsupported_protocol_refuses(
    accepted_artifact: ProviderArtifactPayload,
    manifest_path: Path,
    lock_path: Path,
    tmp_path: Path,
    local_backend: LocalEnvBackend,
) -> None:
    """A provider that speaks only a future major cannot bind to this executor."""

    document = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    document["supported_protocol_majors"] = [2]
    target = tmp_path / "manifest.yaml"
    target.write_text(yaml.safe_dump(document), encoding="utf-8")

    from cruxible_provider_runtime.manifest import load_manifest, manifest_digest

    future = load_manifest(target)
    registry = StubRegistry()
    from cruxible_provider_noop.interface import registration

    registry.register_interface(registration())
    registry.register_provider(
        accepted_artifact.model_copy(
            update={"manifest": future, "manifest_digest": manifest_digest(future)}
        )
    )
    with pytest.raises(RefusalError) as exc:
        bind(registry, _request(target, lock_path), local_backend=local_backend)
    assert exc.value.code is RefusalCode.UNSUPPORTED_PROTOCOL


def test_unsupported_backend_refuses(
    accepted_artifact: ProviderArtifactPayload,
    manifest_path: Path,
    lock_path: Path,
    tmp_path: Path,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> None:
    document = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    document["implementations"][0]["backends"] = ["local_env"]
    target = tmp_path / "manifest.yaml"
    target.write_text(yaml.safe_dump(document), encoding="utf-8")

    from cruxible_provider_runtime.manifest import load_manifest, manifest_digest

    local_only = load_manifest(target)
    registry = StubRegistry()
    from cruxible_provider_noop.interface import registration

    registry.register_interface(registration())
    registry.register_provider(
        accepted_artifact.model_copy(
            update={
                "manifest": local_only,
                "manifest_digest": manifest_digest(local_only),
                "container": None,
            }
        )
    )
    with pytest.raises(RefusalError) as exc:
        bind(
            registry,
            _request(target, lock_path, backend_kind="container"),
            local_backend=local_backend,
            container_backend=container_backend,
        )
    assert exc.value.code is RefusalCode.UNSUPPORTED_BACKEND


def test_image_provenance_mismatch_refuses(
    registry: StubRegistry,
    accepted_artifact: ProviderArtifactPayload,
    manifest_path: Path,
    lock_path: Path,
    local_backend: LocalEnvBackend,
) -> None:
    assert accepted_artifact.container is not None
    lying_image = accepted_artifact.container.provenance.model_copy(
        update={"base_image_digest": "sha256:" + "de" * 32}
    )
    backend = ContainerBackend(FakeContainerDriver(provenance=lying_image))
    with pytest.raises(RefusalError) as exc:
        bind(
            registry,
            _request(manifest_path, lock_path, backend_kind="container"),
            local_backend=local_backend,
            container_backend=backend,
        )
    assert exc.value.code is RefusalCode.IMAGE_PROVENANCE_MISMATCH
    assert "base_image_digest" in exc.value.refusal.detail["mismatches"]


def test_bucket_fixture_missing_refuses_at_registration(
    accepted_artifact: ProviderArtifactPayload, manifest_path: Path, tmp_path: Path
) -> None:
    document = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    document["implementations"][0]["bucket_conformance"] = {}
    target = tmp_path / "manifest.yaml"
    target.write_text(yaml.safe_dump(document), encoding="utf-8")

    from cruxible_provider_runtime.manifest import load_manifest, manifest_digest

    unfixtured = load_manifest(target)
    registry = StubRegistry()
    from cruxible_provider_noop.interface import registration

    registry.register_interface(registration())
    with pytest.raises(RefusalError) as exc:
        registry.register_provider(
            accepted_artifact.model_copy(
                update={"manifest": unfixtured, "manifest_digest": manifest_digest(unfixtured)}
            )
        )
    assert exc.value.code is RefusalCode.BUCKET_FIXTURE_MISSING


def test_air_gapped_bind_refuses_to_materialize_a_missing_environment(
    registry: StubRegistry,
    manifest_path: Path,
    lock_path: Path,
    tmp_path: Path,
    resolved: ResolvedSet,
) -> None:
    """Air-gapped mode is cache-only: a cold cache refuses rather than fetching."""

    cache = MaterializationCache(tmp_path / "cold-cache")
    fetcher = ArtifactFetcher(
        IndexConfig(index_urls=("https://index.example/simple",), air_gapped=True),
        FakeIndexTransport(),
    )
    backend = LocalEnvBackend(cache, fetcher, UvSyncBuilder(tmp_path))
    with pytest.raises(RefusalError) as exc:
        bind(registry, _request(manifest_path, lock_path), local_backend=backend)
    assert exc.value.code is RefusalCode.AIR_GAPPED_CACHE_MISS
    assert resolved.distributions


def test_missing_lock_refuses_rather_than_resolving_freely(
    registry: StubRegistry, manifest_path: Path, local_backend: LocalEnvBackend
) -> None:
    with pytest.raises(RefusalError) as exc:
        bind(registry, _request(manifest_path, None), local_backend=local_backend)
    assert exc.value.code is RefusalCode.LOCK_MISMATCH


def test_interface_digest_mismatch_refuses(
    registry: StubRegistry,
    manifest_path: Path,
    lock_path: Path,
    tmp_path: Path,
    local_backend: LocalEnvBackend,
    accepted_artifact: ProviderArtifactPayload,
) -> None:
    """A manifest pinning a different interface digest cannot bind to this slot."""

    document = copy.deepcopy(yaml.safe_load(manifest_path.read_text(encoding="utf-8")))
    document["implementations"][0]["interface_digest"] = "sha256:" + "ab" * 32
    target = tmp_path / "manifest.yaml"
    target.write_text(yaml.safe_dump(document), encoding="utf-8")

    from cruxible_provider_runtime.manifest import load_manifest, manifest_digest

    shifted = load_manifest(target)
    registry_with_shift = StubRegistry()
    from cruxible_provider_noop.interface import registration

    registry_with_shift.register_interface(registration())
    registry_with_shift.providers[accepted_artifact.provider_id] = accepted_artifact.model_copy(
        update={"manifest": shifted, "manifest_digest": manifest_digest(shifted)}
    )
    with pytest.raises(RefusalError) as exc:
        bind(registry_with_shift, _request(target, lock_path), local_backend=local_backend)
    assert exc.value.code is RefusalCode.INTERFACE_DIGEST_MISMATCH
