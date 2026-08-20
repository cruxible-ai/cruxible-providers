"""Fixtures for the document plane's conformance suite.

The accepted Provider artifact is built here rather than committed, and built
from the package's own committed lock: a committed artifact would carry a
materialization digest that goes stale the moment a dependency moves, and a
stale pin nobody notices is what the bind-time recomputation exists to catch.

Two environments, one lock. ``doc.to_markdown`` declares the ``docling`` extra
and ``ocr.extract`` declares ``paddleocr``, so the artifact pins two
materializations — ``linux-cp311-engines+docling`` and
``linux-cp311-engines+paddleocr`` — and a bind for one implementation cannot pick
up the other's environment. Neither environment is materialized for real: the
injected builder stages the resolution it was asked for, which is what lets the
default lane bind an engine environment without installing an engine.

Nothing here needs a network, a tensor stack, or a container engine.
"""

from __future__ import annotations

from pathlib import Path

import cruxible_provider_docs
import pytest
from cruxible_provider_docs.interfaces import registrations
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
    ResolvedSet,
    environment_pin_key,
    load_uv_lock,
    resolve,
)
from cruxible_provider_runtime.testing import (
    ENGINE_MARKER_ENVIRONMENT,
    FakeContainerDriver,
    FakeIndexTransport,
    InjectedEnvironmentBuilder,
)

PACKAGE_DIR = Path(cruxible_provider_docs.__file__).resolve().parent.parent.parent
REPO_ROOT = PACKAGE_DIR.parent.parent
RUNTIME_SRC = REPO_ROOT / "packages" / "cruxible-provider-runtime" / "src"
DOCS_SRC = PACKAGE_DIR / "src"

PROVIDER_ID = "cruxible-provider-docs"
DISTRIBUTION_SHA256 = "sha256:" + "4d" * 32
IMAGE_DIGEST = "sha256:" + "d0" * 32
BASE_IMAGE_DIGEST = "sha256:" + "b0" * 32
BUILDER_IDENTITY = "ci/build-provider-images@runner-0"

# Both implementations here carry an engine, so both environments need the broad
# marker environment the runtime ships. See its docstring for why the launch
# environment list cannot pin an environment containing an engine.
MARKER_ENVIRONMENT = ENGINE_MARKER_ENVIRONMENT

# Extras per implementation, mirroring the manifest. Spelled out here rather than
# read from the manifest so that a manifest edit changing an implementation's
# engine fails these tests instead of being followed by them.
MARKDOWN_EXTRAS: tuple[str, ...] = ("docling",)
OCR_EXTRAS: tuple[str, ...] = ("paddleocr",)


@pytest.fixture(scope="session")
def manifest_path() -> Path:
    return cruxible_provider_docs.MANIFEST_PATH


@pytest.fixture(scope="session")
def lock_path() -> Path:
    return PACKAGE_DIR / "uv.lock"


@pytest.fixture(scope="session")
def manifest(manifest_path: Path) -> ProviderManifest:
    return load_manifest(manifest_path)


def _resolved(lock_path: Path, extras: tuple[str, ...]) -> ResolvedSet:
    # allow_editable_dev_sources: this package depends on the runtime by path,
    # because the runtime is not published yet. Production binds refuse a local
    # source outright, and an accepted artifact cannot be produced from one.
    return resolve(
        load_uv_lock(lock_path),
        PROVIDER_ID,
        MARKER_ENVIRONMENT,
        extras=extras,
        allow_editable_dev_sources=True,
    )


@pytest.fixture(scope="session")
def markdown_resolution(lock_path: Path) -> ResolvedSet:
    return _resolved(lock_path, MARKDOWN_EXTRAS)


@pytest.fixture(scope="session")
def ocr_resolution(lock_path: Path) -> ResolvedSet:
    return _resolved(lock_path, OCR_EXTRAS)


@pytest.fixture()
def accepted_artifact(
    manifest: ProviderManifest,
    markdown_resolution: ResolvedSet,
    ocr_resolution: ResolvedSet,
    lock_path: Path,
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
            filename=f"cruxible_provider_docs-{version}-py3-none-any.whl",
            sha256=DISTRIBUTION_SHA256,
            index_url="https://index.example/simple",
            url=(
                "https://index.example/simple/cruxible-provider-docs/"
                f"cruxible_provider_docs-{version}-py3-none-any.whl"
            ),
        ),
        local_env=LocalEnvBackendPin(
            lock_sha256=lock.lock_sha256,
            materialization_digests={
                environment_pin_key(MARKER_ENVIRONMENT.id, MARKDOWN_EXTRAS): (
                    materialization_digest(
                        markdown_resolution, distribution_sha256=DISTRIBUTION_SHA256
                    )
                ),
                environment_pin_key(MARKER_ENVIRONMENT.id, OCR_EXTRAS): materialization_digest(
                    ocr_resolution, distribution_sha256=DISTRIBUTION_SHA256
                ),
            },
        ),
        container=ContainerBackendPin(
            image_reference="registry.example/cruxible/provider-docs",
            image_digest=IMAGE_DIGEST,
            provenance=ImageProvenance(
                provider_artifact_digest="sha256:" + "00" * 32,
                materialization_digest=materialization_digest(
                    markdown_resolution, distribution_sha256=DISTRIBUTION_SHA256
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
    for registration in registrations():
        stub.register_interface(registration)
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
    return InjectedEnvironmentBuilder(python_path_roots=(RUNTIME_SRC, DOCS_SRC))


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
        python_path_roots=(RUNTIME_SRC, DOCS_SRC),
        known_digests=(IMAGE_DIGEST,),
    )


@pytest.fixture()
def container_backend(container_driver: FakeContainerDriver) -> ContainerBackend:
    return ContainerBackend(container_driver)


@pytest.fixture()
def egress_guard_root(tmp_path: Path) -> Path:
    """A directory carrying the child-process egress guard, for the conformance lane."""

    return write_child_guard(tmp_path / "egress-guard")
