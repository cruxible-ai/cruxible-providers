"""Fixtures for the quantitative plane's conformance suite.

Same shape as the reference provider's: the accepted Provider artifact is built
here rather than committed, and built from the package's own **committed lock**,
because a committed artifact carries a materialization digest that goes stale the
moment a dependency moves — and a stale pin nobody notices is exactly what
bind-time recomputation exists to catch. The distribution sha256 is synthetic:
the package is not built during the test run, and the implementation digest's
dependence on a real artifact hash is a property of release.

What is different here is the registry. Seven real launch interfaces are seeded
instead of one stub, and each is seeded with the vocabulary committed under
``vocab/interfaces/`` — the repository's single copy of core's data. The provider
package ships the classifiers and the pinned stub digests; it does not ship a
second copy of the vocabularies, because it does not own them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import cruxible_provider_quant
import pytest
from cruxible_provider_quant.interfaces import INTERFACE_IDS, registration
from cruxible_provider_runtime.artifact import (
    ContainerBackendPin,
    DistributionPin,
    ImageProvenance,
    LocalEnvBackendPin,
    ProviderArtifactPayload,
    artifact_digest,
)
from cruxible_provider_runtime.backends import ContainerBackend, LocalEnvBackend
from cruxible_provider_runtime.binding import Binding, BindRequest, bind
from cruxible_provider_runtime.cache import MaterializationCache
from cruxible_provider_runtime.digests import materialization_digest
from cruxible_provider_runtime.egress import EgressRecorder, write_child_guard
from cruxible_provider_runtime.index import ArtifactFetcher, IndexConfig
from cruxible_provider_runtime.manifest import (
    BackendKind,
    ProviderManifest,
    load_manifest,
    manifest_digest,
)
from cruxible_provider_runtime.protocol import Budgets
from cruxible_provider_runtime.provider_api import ProviderResult, ProviderRunContext
from cruxible_provider_runtime.registry import StubRegistry, load_bucket_vocabulary
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

PACKAGE_DIR = Path(cruxible_provider_quant.__file__).resolve().parent.parent.parent
REPO_ROOT = PACKAGE_DIR.parent.parent
RUNTIME_SRC = REPO_ROOT / "packages" / "cruxible-provider-runtime" / "src"
QUANT_SRC = PACKAGE_DIR / "src"
VOCAB_DIR = REPO_ROOT / "vocab" / "interfaces"

PROVIDER_ID = "cruxible-provider-quant"
DISTRIBUTION_SHA256 = "sha256:" + "5c" * 32
IMAGE_DIGEST = "sha256:" + "e2" * 32
BASE_IMAGE_DIGEST = "sha256:" + "b0" * 32
BUILDER_IDENTITY = "ci/build-provider-images@runner-0"

BUDGETS = Budgets(wall_clock_seconds=120.0, output_bytes=16_000_000)
BACKENDS: tuple[BackendKind, ...] = ("local_env", "container")

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


# --------------------------------------------------------------------------
# package artifacts
# --------------------------------------------------------------------------


@pytest.fixture(scope="session")
def manifest_path() -> Path:
    return cruxible_provider_quant.MANIFEST_PATH


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
        PROVIDER_ID,
        MARKER_ENVIRONMENT,
        allow_editable_dev_sources=True,
    )


@pytest.fixture()
def accepted_artifact(
    manifest: ProviderManifest, resolved: ResolvedSet, lock_path: Path
) -> ProviderArtifactPayload:
    lock = load_uv_lock(lock_path)
    pin = materialization_digest(resolved, distribution_sha256=DISTRIBUTION_SHA256)
    payload = ProviderArtifactPayload(
        provider_id=manifest.provider_id,
        status="accepted",
        manifest=manifest,
        manifest_digest=manifest_digest(manifest),
        distribution=DistributionPin(
            name=manifest.distribution.name,
            version=manifest.distribution.version,
            filename=f"cruxible_provider_quant-{manifest.distribution.version}-py3-none-any.whl",
            sha256=DISTRIBUTION_SHA256,
            index_url="https://index.example/simple",
            url=(
                "https://index.example/simple/cruxible-provider-quant/"
                f"cruxible_provider_quant-{manifest.distribution.version}-py3-none-any.whl"
            ),
        ),
        local_env=LocalEnvBackendPin(
            lock_sha256=lock.lock_sha256,
            materialization_digests={MARKER_ENVIRONMENT.id: pin},
        ),
        container=ContainerBackendPin(
            image_reference="registry.example/cruxible/provider-quant",
            image_digest=IMAGE_DIGEST,
            provenance=ImageProvenance(
                provider_artifact_digest="sha256:" + "00" * 32,
                materialization_digest=pin,
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


# --------------------------------------------------------------------------
# registry
# --------------------------------------------------------------------------


def seed_interfaces(stub: StubRegistry) -> StubRegistry:
    """Register all seven launch interfaces from the committed vocabularies."""

    for interface_id in INTERFACE_IDS:
        vocabulary = load_bucket_vocabulary(VOCAB_DIR / f"{interface_id}.yaml")
        stub.register_interface(registration(interface_id, vocabulary))
    return stub


@pytest.fixture()
def registry(accepted_artifact: ProviderArtifactPayload) -> StubRegistry:
    stub = seed_interfaces(StubRegistry())
    stub.register_provider(accepted_artifact)
    return stub


# --------------------------------------------------------------------------
# backends
# --------------------------------------------------------------------------


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
    return InjectedEnvironmentBuilder(python_path_roots=(RUNTIME_SRC, QUANT_SRC))


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
        python_path_roots=(RUNTIME_SRC, QUANT_SRC),
        known_digests=(IMAGE_DIGEST,),
    )


@pytest.fixture()
def container_backend(container_driver: FakeContainerDriver) -> ContainerBackend:
    return ContainerBackend(container_driver)


@pytest.fixture()
def egress_guard_root(tmp_path: Path) -> Path:
    """A directory carrying the child-process egress guard, for the conformance lane."""

    return write_child_guard(tmp_path / "egress-guard")


def bind_interface(
    interface_id: str,
    backend_kind: BackendKind,
    registry: StubRegistry,
    manifest_path: Path,
    lock_path: Path,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend | None = None,
) -> Binding:
    return bind(
        registry,
        BindRequest(
            provider_id=PROVIDER_ID,
            interface_id=interface_id,
            backend_kind=backend_kind,
            manifest_path=manifest_path,
            lock_path=lock_path,
            marker_environment=MARKER_ENVIRONMENT,
            allow_editable_dev_sources=True,
        ),
        local_backend=local_backend,
        container_backend=container_backend,
    )


# --------------------------------------------------------------------------
# in-process invocation
# --------------------------------------------------------------------------


def entrypoint(interface_id: str) -> Any:
    """Resolve one implementation the way the child harness would."""

    import importlib

    from cruxible_provider_quant.interfaces import INTERFACE_PREIMAGES

    assert interface_id in INTERFACE_PREIMAGES
    path = _ENTRYPOINTS[interface_id]
    module_name, _, object_name = path.partition(":")
    return getattr(importlib.import_module(module_name), object_name)()


_ENTRYPOINTS = {
    "calc.calibrate": "cruxible_provider_quant.calibrate:Calibrate",
    "calc.reduce": "cruxible_provider_quant.reduce:Reduce",
    "match.record": "cruxible_provider_quant.linkage:RecordLinkage",
    "score.rank": "cruxible_provider_quant.rank:Rank",
    "stat.test": "cruxible_provider_quant.stat_test:StatTest",
    "ts.anomaly": "cruxible_provider_quant.anomaly:Anomaly",
    "ts.forecast": "cruxible_provider_quant.forecast:Forecast",
}


def run_in_process(
    interface_id: str,
    payload: dict[str, Any],
    *,
    registry: StubRegistry | None = None,
    secrets: dict[str, str] | None = None,
) -> ProviderResult:
    """Invoke one implementation directly, with admission still enforced.

    The full bind-and-invoke loop is exercised on both backend kinds in
    ``test_full_loop.py``. Everything else runs in process: spawning a child per
    assertion would buy nothing except the cost of importing an engine again,
    and the executor-side behaviour it would re-test is the runtime's, already
    covered by the runtime's own suite and by the reference provider's.

    Admission is still real. The bucket is derived by the registered classifier
    from the actual payload and checked against what the manifest claims, so an
    in-process run cannot slip past a bucket a bound run would refuse.
    """

    from cruxible_provider_quant import MANIFEST_PATH

    stub = registry if registry is not None else seed_interfaces(StubRegistry())
    manifest = load_manifest(MANIFEST_PATH)
    implementation = manifest.implementation(interface_id)
    bucket = stub.admit(interface_id, implementation.declared_input_buckets, payload)
    context = ProviderRunContext(
        run_id="run-in-process",
        interface_id=interface_id,
        interface_digest=implementation.interface_digest,
        implementation_digest="sha256:" + "11" * 32,
        input_bucket=bucket,
        input=payload,
        coordinates={},
        budgets=BUDGETS,
        declared_endpoints=implementation.declared_endpoints,
        capture_contract=implementation.capture_contract_families[0],
        secrets=dict(secrets or {}),
        egress=EgressRecorder(),
    )
    return entrypoint(interface_id)(context)
