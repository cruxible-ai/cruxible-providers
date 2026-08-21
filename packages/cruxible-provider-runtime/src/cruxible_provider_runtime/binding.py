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

from dataclasses import dataclass, field
from pathlib import Path

from .artifact import ProviderArtifactPayload, artifact_digest
from .backends import ContainerBackend, LocalEnvBackend
from .buckets import BucketSelector
from .cache import tree_digest
from .digests import container_materialization_digest, implementation_digest, materialization_digest
from .errors import RefusalCode, refuse
from .manifest import BackendKind, ImplementationManifest, load_manifest, manifest_digest
from .protocol import PROTOCOL_VERSION, ProtocolVersion
from .registry import StubRegistry
from .resolution import (
    MarkerEnvironment,
    ResolvedSet,
    environment_pin_key,
    load_uv_lock,
    resolve,
)

__all__ = ["BindRequest", "Binding", "bind"]

_VANISHED_TREE_FINGERPRINT = (-1, -1.0)


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


def _tree_fingerprint(root: Path) -> tuple[int, float]:
    """A stat-only reading of a tree: how many entries, and the newest mtime.

    Cheap because it opens nothing. It is a staleness signal and never an
    integrity one — :meth:`Binding.revalidate` uses it only to decide whether to
    recompute the digest that *is* the integrity check.

    An entry that disappears mid-walk returns a sentinel this binding never
    caches, which forces the full hash. Guessing at what the tree looked like a
    moment ago would be the fail-open reading.
    """

    count = 0
    newest = 0.0
    for path in root.rglob("*"):
        count += 1
        try:
            newest = max(newest, path.lstat().st_mtime)
        except OSError:
            return _VANISHED_TREE_FINGERPRINT
    return (count, newest)


@dataclass(frozen=True)
class Binding:
    """A verified, materialized pin, ready to invoke.

    A binding is reusable, and everything bind checked lives somewhere mutable:
    the registry's accepted artifact can be replaced, the package-side manifest
    is a file on disk, and the materialized tree is a directory. So the binding
    keeps the three digests bind established — accepted artifact, package
    manifest, sealed tree — and :meth:`revalidate` recomputes each from its live
    source before every run. Retaining them here rather than re-binding per
    invocation keeps the expensive half (resolution, materialization) once per
    bind while making the governance half unskippable.
    """

    provider_id: str
    interface_id: str
    interface_digest: str
    implementation: ImplementationManifest
    implementation_digest: str
    materialization_digest: str
    protocol_version: ProtocolVersion
    backend_kind: BackendKind
    artifact: ProviderArtifactPayload
    manifest_path: Path
    accepted_artifact_digest: str
    manifest_digest: str
    sealed_tree_digest: str | None = None
    """The materialized tree's digest as bind sealed it; ``None`` for a container.

    The cache's own seal is a self-consistent pair, so a rewrite that updates
    both halves survives it. This copy is held outside the tree, which is what
    makes the invoke-time comparison mean something.
    """

    extras: tuple[str, ...] = ()
    """The extras this implementation's manifest requires, sorted.

    Part of the snapshot because an environment built with an engine and one
    built without it are different environments, and a reader looking at two
    materialization digests deserves to be told why they differ.
    """

    env_path: Path | None = None
    image_digest: str | None = None
    resolved: ResolvedSet | None = None
    dev_sources_permitted: bool = False

    tree_watch: dict[str, tuple[int, float]] = field(
        default_factory=dict, repr=False, compare=False
    )
    """The last tree fingerprint this binding verified the full hash against.

    Not identity and not part of the snapshot: a scratchpad for
    :meth:`revalidate`'s staleness gate, and the only mutable thing on an
    otherwise frozen record.
    """

    def snapshot(self) -> dict[str, str | bool | list[str]]:
        """The binding snapshot a LineDeployment records."""

        snapshot: dict[str, str | bool | list[str]] = {
            "provider_id": self.provider_id,
            "interface_id": self.interface_id,
            "interface_digest": self.interface_digest,
            "implementation_digest": self.implementation_digest,
            "materialization_digest": self.materialization_digest,
            "protocol_version": self.protocol_version.render(),
            "backend_kind": self.backend_kind,
            # Always present, empty list included: the environment a binding
            # names is the environment for these extras and no others.
            "extras": sorted(self.extras),
            # Always present, both ways. Emitting only the true case makes the
            # key absent-means-false, and a consumer that has never heard of it
            # then reads a dev-source pin as a production pin -- the exact
            # misreading the field exists to prevent.
            "dev_sources_permitted": self.dev_sources_permitted,
        }
        return snapshot

    def revalidate(self, registry: StubRegistry) -> None:
        """Re-check, from live sources, the acceptance this binding runs under.

        Called before every invocation, in bind's order. The contract says the
        manifest is re-digested at bind *and invoke*; the same reasoning applies
        to the other two mutable inputs, because a binding held across a
        withdrawal, a manifest edit, or a tampered cache entry would otherwise
        keep executing under an acceptance that no longer exists.

        **The tree hash is gated on a cheap fingerprint, and the gate is
        defeatable.** Hashing every file of a materialized environment is
        seconds, not milliseconds — measured at 3.7s over a 19k-entry
        environment, and a tensor stack is several times that — which is a cost
        per *invocation*, on a check whose answer is almost always the one it
        gave a moment ago. So the full hash stays the authority and runs whenever
        the environment's ``(entry count, newest mtime)`` differs from the
        reading taken when it last verified; an unchanged reading skips it.

        An adversary who edits a file and restores its mtime defeats that gate.
        That is a real hole and it is not a new one: it needs write access to a
        ``0700`` cache the operator owns, and a local provider environment is
        **dependency isolation, not a security boundary** — a caller who can
        write there can already run code as the operator without touching the
        cache at all. Containment is the cloud container backend's, and nothing
        here is offered in its place. See ``docs/packaging.md`` on local
        execution.

        A second residual, for the same reason and with the same answer: the tree
        is checked here and the child is spawned afterwards, so a write that
        lands in between runs unchecked. Closing that would mean holding the tree
        immutable across the call, which the local backend does not claim to do.
        """

        current = artifact_digest(registry.accepted_provider(self.provider_id))
        if current != self.accepted_artifact_digest:
            raise refuse(
                RefusalCode.ACCEPTANCE_DIVERGENCE,
                "the accepted Provider artifact changed after this binding was made",
                provider_id=self.provider_id,
                bound=self.accepted_artifact_digest,
                current=current,
            )
        recomputed = manifest_digest(load_manifest(self.manifest_path))
        if recomputed != self.manifest_digest:
            raise refuse(
                RefusalCode.MANIFEST_DIVERGENCE,
                "the package-side manifest changed after this binding was made",
                provider_id=self.provider_id,
                bound=self.manifest_digest,
                recomputed=recomputed,
            )
        if self.env_path is not None and self.sealed_tree_digest is not None:
            fingerprint = _tree_fingerprint(self.env_path)
            if self.tree_watch.get(str(self.env_path)) == fingerprint:
                return
            actual = tree_digest(self.env_path)
            if actual != self.sealed_tree_digest:
                raise refuse(
                    RefusalCode.CACHE_INTEGRITY,
                    "the materialized environment changed after this binding was made",
                    provider_id=self.provider_id,
                    env_path=str(self.env_path),
                    sealed=self.sealed_tree_digest,
                    actual=actual,
                )
            # Recorded only after the authority agreed. A fingerprint cached
            # before the hash ran would be a gate that skips a check nobody
            # passed.
            if fingerprint != _VANISHED_TREE_FINGERPRINT:
                self.tree_watch[str(self.env_path)] = fingerprint


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
    # The extras come from the manifest, never from the bind request. A request
    # that could name its own extras could materialize an environment the
    # accepted artifact never pinned, and the implementation would run against
    # engines nobody approved.
    extras = implementation.requires_extras
    pin_key = environment_pin_key(env.id, extras)
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
        extras=extras,
        allow_editable_dev_sources=request.allow_editable_dev_sources,
    )
    computed = materialization_digest(resolved, distribution_sha256=payload.distribution.sha256)
    pinned = payload.local_env.materialization_digests.get(pin_key)
    if pinned is None:
        raise refuse(
            RefusalCode.LOCK_MISMATCH,
            f"accepted artifact pins no materialization for environment {pin_key!r}",
            provider_id=request.provider_id,
            marker_environment=env.id,
            extras=list(extras),
            environment_pin_key=pin_key,
            pinned_environments=sorted(payload.local_env.materialization_digests),
        )
    if computed != pinned:
        raise refuse(
            RefusalCode.LOCK_MISMATCH,
            "lock resolves to a different materialization than the accepted artifact pins",
            provider_id=request.provider_id,
            marker_environment=env.id,
            environment_pin_key=pin_key,
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
        distribution=payload.distribution,
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
        manifest_path=request.manifest_path,
        accepted_artifact_digest=artifact_digest(payload),
        manifest_digest=payload.manifest_digest,
        sealed_tree_digest=tree_digest(env_path),
        extras=extras,
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
        manifest_path=request.manifest_path,
        accepted_artifact_digest=artifact_digest(payload),
        manifest_digest=payload.manifest_digest,
        # The image already contains whatever the extras pulled in; the field
        # records what the implementation asked for, so the two backends'
        # snapshots stay comparable.
        extras=implementation.requires_extras,
        image_digest=payload.container.image_digest,
    )
