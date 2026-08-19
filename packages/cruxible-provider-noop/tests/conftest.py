"""Fixtures for the reference provider's conformance suite.

The accepted Provider artifact is built here rather than committed, and built
from the package's own **committed lock**. A committed artifact would carry a
materialization digest that goes stale the moment a dependency moves, and a
stale pin that nobody notices is exactly what the bind-time recomputation exists
to catch.

The distribution sha256 is synthetic: the package is not built during the test
run, and the implementation digest's dependence on a real artifact hash is a
property of release, not of this suite.
"""

from __future__ import annotations

from pathlib import Path

import cruxible_provider_noop
import pytest
from cruxible_provider_noop.interface import registration
from cruxible_provider_runtime.artifact import (
    ContainerBackendPin,
    DistributionPin,
    ImageProvenance,
    LocalEnvBackendPin,
    ProviderArtifactPayload,
    artifact_digest,
)
from cruxible_provider_runtime.backends import ContainerBackend, LocalEnvBackend
from cruxible_provider_runtime.cache import MaterializationCache
from cruxible_provider_runtime.digests import materialization_digest
from cruxible_provider_runtime.egress import write_child_guard
from cruxible_provider_runtime.index import ArtifactFetcher, IndexConfig
from cruxible_provider_runtime.manifest import ProviderManifest, load_manifest, manifest_digest
from cruxible_provider_runtime.registry import StubRegistry
from cruxible_provider_runtime.resolution import (
    MarkerEnvironment,
    ResolvedSet,
    load_uv_lock,
    resolve,
)
from cruxible_provider_runtime.testing import (
    FakeContainerDriver,
    FakeIndexTransport,
    InjectedEnvironmentBuilder,
)

PACKAGE_DIR = Path(cruxible_provider_noop.__file__).resolve().parent.parent.parent
REPO_ROOT = PACKAGE_DIR.parent.parent
RUNTIME_SRC = REPO_ROOT / "packages" / "cruxible-provider-runtime" / "src"
NOOP_SRC = PACKAGE_DIR / "src"

DISTRIBUTION_SHA256 = "sha256:" + "7d" * 32
IMAGE_DIGEST = "sha256:" + "e1" * 32
BASE_IMAGE_DIGEST = "sha256:" + "b0" * 32
BUILDER_IDENTITY = "ci/build-provider-images@runner-0"

MARKER_ENVIRONMENT = MarkerEnvironment(
    id="linux-cp311",
    markers={
        "implementation_name": "cpython",
        "os_name": "posix",
        "platform_machine": "x86_64",
        "python_full_version": "3.11.9",
        "python_version": "3.11",
        "sys_platform": "linux",
    },
    tags=(
        "cp311-cp311-manylinux_2_17_x86_64",
        "cp311-abi3-manylinux_2_17_x86_64",
        "py3-none-any",
    ),
)


@pytest.fixture(scope="session")
def manifest_path() -> Path:
    return cruxible_provider_noop.MANIFEST_PATH


@pytest.fixture(scope="session")
def lock_path() -> Path:
    return PACKAGE_DIR / "uv.lock"


@pytest.fixture(scope="session")
def manifest(manifest_path: Path) -> ProviderManifest:
    return load_manifest(manifest_path)


@pytest.fixture(scope="session")
def resolved(lock_path: Path) -> ResolvedSet:
    # allow_editable_dev_sources is the dev-only escape hatch: this package
    # depends on the runtime by path, because the runtime is not published yet.
    # Production binds refuse a local source outright.
    return resolve(
        load_uv_lock(lock_path),
        "cruxible-provider-noop",
        MARKER_ENVIRONMENT,
        allow_editable_dev_sources=True,
    )


@pytest.fixture()
def accepted_artifact(
    manifest: ProviderManifest, resolved: ResolvedSet, lock_path: Path
) -> ProviderArtifactPayload:
    lock = load_uv_lock(lock_path)
    payload = ProviderArtifactPayload(
        provider_id=manifest.provider_id,
        status="accepted",
        manifest=manifest,
        manifest_digest=manifest_digest(manifest),
        distribution=DistributionPin(
            name=manifest.distribution.name,
            version=manifest.distribution.version,
            filename=(f"cruxible_provider_noop-{manifest.distribution.version}-py3-none-any.whl"),
            sha256=DISTRIBUTION_SHA256,
            index_url="https://index.example/simple",
            url=(
                "https://index.example/simple/cruxible-provider-noop/"
                f"cruxible_provider_noop-{manifest.distribution.version}-py3-none-any.whl"
            ),
        ),
        local_env=LocalEnvBackendPin(
            lock_sha256=lock.lock_sha256,
            materialization_digests={
                MARKER_ENVIRONMENT.id: materialization_digest(
                    resolved, distribution_sha256=DISTRIBUTION_SHA256
                )
            },
        ),
        container=ContainerBackendPin(
            image_reference="registry.example/cruxible/provider-noop",
            image_digest=IMAGE_DIGEST,
            provenance=ImageProvenance(
                provider_artifact_digest="sha256:" + "00" * 32,
                materialization_digest=materialization_digest(
                    resolved, distribution_sha256=DISTRIBUTION_SHA256
                ),
                base_image_digest=BASE_IMAGE_DIGEST,
                builder_identity=BUILDER_IDENTITY,
            ),
        ),
    )
    # The image records the artifact digest, which is computed with that
    # self-reference excluded; fill it in once the rest of the payload is fixed.
    settled = artifact_digest(payload)
    return payload.model_copy(
        update={
            "container": payload.container.model_copy(  # type: ignore[union-attr]
                update={
                    "provenance": payload.container.provenance.model_copy(  # type: ignore[union-attr]
                        update={"provider_artifact_digest": settled}
                    )
                }
            )
        }
    )


@pytest.fixture()
def registry(accepted_artifact: ProviderArtifactPayload) -> StubRegistry:
    stub = StubRegistry()
    stub.register_interface(registration())
    stub.register_provider(accepted_artifact)
    return stub


@pytest.fixture()
def cache(tmp_path: Path) -> MaterializationCache:
    return MaterializationCache(tmp_path / "provider-cache")


@pytest.fixture()
def fetcher() -> ArtifactFetcher:
    return ArtifactFetcher(
        IndexConfig(index_urls=("https://index.example/simple",)), FakeIndexTransport()
    )


@pytest.fixture()
def builder() -> InjectedEnvironmentBuilder:
    return InjectedEnvironmentBuilder(python_path_roots=(RUNTIME_SRC, NOOP_SRC))


@pytest.fixture()
def local_backend(
    cache: MaterializationCache, fetcher: ArtifactFetcher, builder: InjectedEnvironmentBuilder
) -> LocalEnvBackend:
    return LocalEnvBackend(cache, fetcher, builder)


@pytest.fixture()
def container_driver(accepted_artifact: ProviderArtifactPayload) -> FakeContainerDriver:
    assert accepted_artifact.container is not None
    return FakeContainerDriver(
        provenance=accepted_artifact.container.provenance,
        python_path_roots=(RUNTIME_SRC, NOOP_SRC),
        known_digests=(IMAGE_DIGEST,),
    )


@pytest.fixture()
def container_backend(container_driver: FakeContainerDriver) -> ContainerBackend:
    return ContainerBackend(container_driver)


@pytest.fixture()
def tampered_lock(lock_path: Path, tmp_path: Path, resolved: ResolvedSet) -> Path:
    """A copy of the committed lock whose *resolution* differs by one artifact hash.

    Tampering with a hash rather than with formatting is deliberate: the
    materialization digest hashes the resolution, so a reformatted lock must
    still bind and a substituted artifact must not.
    """

    text = lock_path.read_text(encoding="utf-8")
    registry_entries = [d for d in resolved.distributions if not d.is_local_source]
    original = registry_entries[0].artifact_id.removeprefix("sha256:")
    assert original in text, "the tampered hash must be one the resolution actually selects"
    substituted = ("f" if original[0] != "f" else "0") + original[1:]
    target = tmp_path / "uv.lock"
    target.write_text(text.replace(original, substituted, 1), encoding="utf-8")
    return target


@pytest.fixture()
def reformatted_lock(lock_path: Path, tmp_path: Path) -> Path:
    """A copy of the committed lock with formatting-only churn."""

    text = lock_path.read_text(encoding="utf-8")
    target = tmp_path / "uv.lock"
    target.write_text(
        "# a comment a future uv release might add\n" + text.replace("\n\n", "\n\n\n"),
        encoding="utf-8",
    )
    return target


@pytest.fixture()
def egress_guard_root(tmp_path: Path) -> Path:
    """A directory carrying the child-process egress guard, for the conformance lane."""

    return write_child_guard(tmp_path / "egress-guard")
