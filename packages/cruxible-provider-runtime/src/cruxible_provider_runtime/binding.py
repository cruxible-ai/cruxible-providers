"""Bind: from an accepted Provider artifact to a materialized, invocable pin.

Bind is where every registration-time law is re-checked at use time, in a fixed
order, each failure typed:

1. the provider has an **accepted** artifact (``unaccepted_provider``);
2. the package-side manifest re-digests to what the accepted artifact recorded
   (``manifest_divergence``) — the manifest is a transcription source, never
   authority;
3. the manifest declares the requested interface (``undeclared_interface``) at
   the registry's digest (``interface_digest_mismatch``);
4. the manifest supports this executor's protocol major
   (``unsupported_protocol``);
5. the implementation supports the requested backend kind
   (``unsupported_backend``);
6. every claimed bucket has a conformance fixture (``bucket_fixture_missing``);
7. the lock's bytes are the ones the accepted artifact pinned
   (``lock_bytes_mismatch``) and it resolves, for the target marker environment,
   to the materialization digest the accepted artifact pinned
   (``lock_mismatch``);
8. the environment materializes into a verified cache entry whose contents were
   checked against the resolution before it was sealed.

On the two lock checks: the byte comparison is cheap tamper-evidence over the
exact file that was reviewed, and the resolution comparison is the primary gate.
They are not redundant. Identity is still never *keyed* on lock bytes — the
materialization digest hashes the resolution — but an accepted artifact pinned a
specific lock file, and a different file, however innocently reformatted, is one
nobody approved. Re-accepting a rewritten lock is a governance step, not an
inconvenience to route around.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .artifact import ProviderArtifactPayload
from .backends import ContainerBackend, LocalEnvBackend
from .buckets import BucketSelector
from .digests import container_materialization_digest, implementation_digest, materialization_digest
from .errors import RefusalCode, refuse
from .manifest import BackendKind, ImplementationManifest, load_manifest, manifest_digest
from .protocol import PROTOCOL_VERSION, ProtocolVersion
from .registry import StubRegistry
from .resolution import MarkerEnvironment, ResolvedSet, load_uv_lock, resolve

__all__ = ["BindRequest", "Binding", "bind"]


@dataclass(frozen=True)
class BindRequest:
    provider_id: str
    interface_id: str
    backend_kind: BackendKind
    manifest_path: Path
    lock_path: Path | None = None
    marker_environment: MarkerEnvironment | None = None
    allow_editable_dev_sources: bool = False
    """Development-only. Admits in-tree path sources into the resolution.

    False in production and in anything that produces an accepted artifact. When
    set, the resulting binding records it, so a receipt shows that the pin was
    computed under the escape hatch rather than over registry artifacts alone.
    """


@dataclass(frozen=True)
class Binding:
    """A verified, materialized pin, ready to invoke."""

    provider_id: str
    interface_id: str
    interface_digest: str
    implementation: ImplementationManifest
    implementation_digest: str
    materialization_digest: str
    protocol_version: ProtocolVersion
    backend_kind: BackendKind
    artifact: ProviderArtifactPayload
    env_path: Path | None = None
    image_digest: str | None = None
    resolved: ResolvedSet | None = None
    dev_sources_permitted: bool = False

    def snapshot(self) -> dict[str, str | bool]:
        """The binding snapshot a LineDeployment records."""

        snapshot: dict[str, str | bool] = {
            "provider_id": self.provider_id,
            "interface_id": self.interface_id,
            "interface_digest": self.interface_digest,
            "implementation_digest": self.implementation_digest,
            "materialization_digest": self.materialization_digest,
            "protocol_version": self.protocol_version.render(),
            "backend_kind": self.backend_kind,
        }
        if self.dev_sources_permitted:
            # Never silently absent-means-false: a pin computed under the
            # dev-source escape hatch has to be visible wherever it is recorded.
            snapshot["dev_sources_permitted"] = True
        return snapshot


def bind(
    registry: StubRegistry,
    request: BindRequest,
    *,
    local_backend: LocalEnvBackend | None = None,
    container_backend: ContainerBackend | None = None,
) -> Binding:
    payload = registry.accepted_provider(request.provider_id)

    package_manifest = load_manifest(request.manifest_path)
    recomputed = manifest_digest(package_manifest)
    if recomputed != payload.manifest_digest:
        raise refuse(
            RefusalCode.MANIFEST_DIVERGENCE,
            "package-side manifest diverges from the accepted Provider artifact",
            provider_id=request.provider_id,
            accepted=payload.manifest_digest,
            recomputed=recomputed,
        )

    implementation = package_manifest.implementation(request.interface_id)
    registration = registry.interface(request.interface_id, implementation.interface_digest)

    if PROTOCOL_VERSION.major not in package_manifest.supported_protocol_majors:
        raise refuse(
            RefusalCode.UNSUPPORTED_PROTOCOL,
            "provider supports protocol majors "
            f"{list(package_manifest.supported_protocol_majors)}; this executor speaks "
            f"{PROTOCOL_VERSION.render()}",
            provider_id=request.provider_id,
            supported=list(package_manifest.supported_protocol_majors),
            executor=PROTOCOL_VERSION.render(),
        )

    if request.backend_kind not in implementation.backends:
        raise refuse(
            RefusalCode.UNSUPPORTED_BACKEND,
            f"implementation of {request.interface_id!r} does not support backend "
            f"{request.backend_kind!r}",
            provider_id=request.provider_id,
            interface_id=request.interface_id,
            supported=list(implementation.backends),
        )

    for selector_text in implementation.declared_input_buckets:
        BucketSelector.parse(selector_text, registration.bucket_vocabulary)
        if selector_text not in implementation.bucket_conformance:
            raise refuse(
                RefusalCode.BUCKET_FIXTURE_MISSING,
                f"claimed bucket {selector_text!r} has no conformance fixture",
                provider_id=request.provider_id,
                interface_id=request.interface_id,
                bucket=selector_text,
            )

    impl_digest = implementation_digest(
        interface_id=implementation.interface_id,
        interface_digest=implementation.interface_digest,
        entrypoint=implementation.entrypoint,
        distribution_sha256=payload.distribution.sha256,
    )

    if request.backend_kind == "local_env":
        return _bind_local(
            registry=registry,
            request=request,
            payload=payload,
            implementation=implementation,
            impl_digest=impl_digest,
            local_backend=local_backend,
        )
    return _bind_container(
        request=request,
        payload=payload,
        implementation=implementation,
        impl_digest=impl_digest,
        container_backend=container_backend,
    )


def _bind_local(
    *,
    registry: StubRegistry,
    request: BindRequest,
    payload: ProviderArtifactPayload,
    implementation: ImplementationManifest,
    impl_digest: str,
    local_backend: LocalEnvBackend | None,
) -> Binding:
    if payload.local_env is None:
        raise refuse(
            RefusalCode.UNSUPPORTED_BACKEND,
            f"accepted artifact for {request.provider_id!r} carries no local_env pin",
            provider_id=request.provider_id,
        )
    if request.lock_path is None or request.marker_environment is None:
        raise refuse(
            RefusalCode.LOCK_MISMATCH,
            "a local_env bind needs both a lock and an explicit marker environment",
            provider_id=request.provider_id,
        )
    env = request.marker_environment
    lock = load_uv_lock(request.lock_path)
    if lock.lock_sha256 != payload.local_env.lock_sha256:
        raise refuse(
            RefusalCode.LOCK_BYTES_MISMATCH,
            "the lock file is not the one the accepted artifact pinned",
            provider_id=request.provider_id,
            pinned=payload.local_env.lock_sha256,
            actual=lock.lock_sha256,
        )
    resolved = resolve(
        lock,
        payload.distribution.name,
        env,
        allow_editable_dev_sources=request.allow_editable_dev_sources,
    )
    computed = materialization_digest(resolved, distribution_sha256=payload.distribution.sha256)
    pinned = payload.local_env.materialization_digests.get(env.id)
    if pinned is None:
        raise refuse(
            RefusalCode.LOCK_MISMATCH,
            f"accepted artifact pins no materialization for marker environment {env.id!r}",
            provider_id=request.provider_id,
            marker_environment=env.id,
            pinned_environments=sorted(payload.local_env.materialization_digests),
        )
    if computed != pinned:
        raise refuse(
            RefusalCode.LOCK_MISMATCH,
            "lock resolves to a different materialization than the accepted artifact pins",
            provider_id=request.provider_id,
            marker_environment=env.id,
            pinned=pinned,
            computed=computed,
        )
    if local_backend is None:
        raise refuse(
            RefusalCode.UNSUPPORTED_BACKEND,
            "no local_env backend was supplied to bind",
            provider_id=request.provider_id,
        )
    env_path = local_backend.materialize(
        computed,
        resolved,
        project_dir=request.lock_path.parent,
        lock_path=request.lock_path,
    )
    del registry  # the registry's work is done before materialization
    return Binding(
        provider_id=request.provider_id,
        interface_id=request.interface_id,
        interface_digest=implementation.interface_digest,
        implementation=implementation,
        implementation_digest=impl_digest,
        materialization_digest=computed,
        protocol_version=PROTOCOL_VERSION,
        backend_kind="local_env",
        artifact=payload,
        env_path=env_path,
        resolved=resolved,
        dev_sources_permitted=request.allow_editable_dev_sources,
    )


def _bind_container(
    *,
    request: BindRequest,
    payload: ProviderArtifactPayload,
    implementation: ImplementationManifest,
    impl_digest: str,
    container_backend: ContainerBackend | None,
) -> Binding:
    if payload.container is None:
        raise refuse(
            RefusalCode.UNSUPPORTED_BACKEND,
            f"accepted artifact for {request.provider_id!r} carries no container pin",
            provider_id=request.provider_id,
        )
    if container_backend is None:
        raise refuse(
            RefusalCode.UNSUPPORTED_BACKEND,
            "no container backend was supplied to bind",
            provider_id=request.provider_id,
        )
    container_backend.verify_image(payload)
    return Binding(
        provider_id=request.provider_id,
        interface_id=request.interface_id,
        interface_digest=implementation.interface_digest,
        implementation=implementation,
        implementation_digest=impl_digest,
        materialization_digest=container_materialization_digest(payload.container.image_digest),
        protocol_version=PROTOCOL_VERSION,
        backend_kind="container",
        artifact=payload,
        image_digest=payload.container.image_digest,
    )
