"""Fixtures for the workspace built-in's conformance suite.

The accepted Provider artifact is built here rather than committed, and built
from the package's own committed lock: a committed artifact would carry a
materialization digest that goes stale the moment a dependency moves, and a
stale pin nobody notices is what the bind-time recomputation exists to catch.

The distribution sha256 is synthetic: the package is not built during the test
run. The real one -- the built wheel's hash -- is what ``scripts/seed_pins.py``
feeds the implementation digest core's seed bundle pins.
"""

from __future__ import annotations

from pathlib import Path

import cruxible_provider_workspace
import pytest
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
from cruxible_provider_workspace.interface import registration

PACKAGE_DIR = Path(cruxible_provider_workspace.__file__).resolve().parent.parent.parent
REPO_ROOT = PACKAGE_DIR.parent.parent
RUNTIME_SRC = REPO_ROOT / "packages" / "cruxible-provider-runtime" / "src"
WORKSPACE_SRC = PACKAGE_DIR / "src"

PROVIDER_ID = "cruxible-provider-workspace"
INTERFACE_ID = "workspace.file"
DISTRIBUTION_SHA256 = "sha256:" + "5e" * 32
IMAGE_DIGEST = "sha256:" + "e5" * 32
BASE_IMAGE_DIGEST = "sha256:" + "b0" * 32
BUILDER_IDENTITY = "ci/build-provider-images@runner-0"

# Mirrors the committed linux-cp311 launch environment and must move with
# ci/marker-environments.json so test pins cannot diverge silently.
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
        "cp311-cp311-manylinux_2_28_x86_64",
        "cp311-abi3-manylinux_2_28_x86_64",
        "py3-none-any",
    ),
)


@pytest.fixture(scope="session")
def manifest_path() -> Path:
    return cruxible_provider_workspace.MANIFEST_PATH


@pytest.fixture(scope="session")
def lock_path() -> Path:
    return PACKAGE_DIR / "uv.lock"


@pytest.fixture(scope="session")
def manifest(manifest_path: Path) -> ProviderManifest:
    return load_manifest(manifest_path)


@pytest.fixture(scope="session")
def resolved(lock_path: Path) -> ResolvedSet:
    # allow_editable_dev_sources: this package depends on the runtime by path,
    # because the runtime is not published yet. Production binds refuse a local
    # source outright, and an accepted artifact cannot be produced from one.
    return resolve(
        load_uv_lock(lock_path),
        PROVIDER_ID,
        MARKER_ENVIRONMENT,
        allow_editable_dev_sources=True,
    )


@pytest.fixture()
def accepted_artifact(
    manifest: ProviderManifest, resolved: ResolvedSet, lock_path: Path
) -> ProviderArtifactPayload:
    lock = load_uv_lock(lock_path)
    version = manifest.distribution.version
    payload = ProviderArtifactPayload(
        provider_id=PROVIDER_ID,
        status="accepted",
        manifest=manifest,
        manifest_digest=manifest_digest(manifest),
        distribution=DistributionPin(
            name=manifest.distribution.name,
            version=version,
            filename=f"cruxible_provider_workspace-{version}-py3-none-any.whl",
            sha256=DISTRIBUTION_SHA256,
            index_url="https://index.example/simple",
            url=(
                "https://index.example/simple/cruxible-provider-workspace/"
                f"cruxible_provider_workspace-{version}-py3-none-any.whl"
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
            image_reference="registry.example/cruxible/provider-workspace",
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
    return InjectedEnvironmentBuilder(python_path_roots=(RUNTIME_SRC, WORKSPACE_SRC))


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
        python_path_roots=(RUNTIME_SRC, WORKSPACE_SRC),
        known_digests=(IMAGE_DIGEST,),
    )


@pytest.fixture()
def container_backend(container_driver: FakeContainerDriver) -> ContainerBackend:
    return ContainerBackend(container_driver)


@pytest.fixture()
def egress_guard_root(tmp_path: Path) -> Path:
    """A directory carrying the child-process egress guard, for the conformance lane."""

    return write_child_guard(tmp_path / "egress-guard")
