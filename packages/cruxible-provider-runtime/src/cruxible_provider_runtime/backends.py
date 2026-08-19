"""The two backend kinds.

One provider artifact, two backend kinds:

``local_env``
    The uv-locked package entrypoint, materialized into an isolated environment
    at bind time. Isolation here is *dependency* isolation, not a security
    boundary.

``container``
    A digest-pinned image. The image digest is authoritative for this backend
    and the build is **not** claimed bit-reproducible; what makes the image
    trustworthy is that its recorded provenance — provider artifact digest,
    materialization digest, base image digest, builder identity — must match the
    accepted Provider artifact, and the executor refuses it otherwise.

Both drivers are injected. Nothing here shells out to a container engine, and no
test in this repo requires one: the container path is covered at protocol level
against a fake driver.
"""

from __future__ import annotations

import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol, runtime_checkable

from .artifact import ImageProvenance, ProviderArtifactPayload, artifact_digest
from .budget import ProcessOutcome, minimal_env, run_with_budget
from .cache import MaterializationCache
from .canonical import canonical_json
from .errors import RefusalCode, refuse
from .index import ArtifactFetcher
from .protocol import Budgets
from .resolution import ResolvedSet

__all__ = [
    "CHILD_MODULE",
    "ContainerBackend",
    "ContainerDriver",
    "EnvironmentBuilder",
    "LocalEnvBackend",
    "UvSyncBuilder",
]

CHILD_MODULE = "cruxible_provider_runtime.child"


@runtime_checkable
class EnvironmentBuilder(Protocol):
    """Populates a staging directory with a materialized provider environment."""

    def build(self, target: Path, resolved: ResolvedSet, fetcher: ArtifactFetcher) -> None: ...

    def interpreter(self, env_path: Path) -> Path: ...

    def child_env(self, env_path: Path) -> Mapping[str, str]: ...


class UvSyncBuilder:
    """The production local builder: ``uv sync --locked`` into an isolated env.

    Requires the network (or a pre-populated resolver cache) and a ``uv`` on the
    path, so it is never exercised by this repo's tests; the test suite injects a
    builder instead. ``--locked`` is the point: the lock is authority, and a
    resolver free to move is a resolver free to break a pin.

    Honest note on where fetching happens: on this path ``uv`` does the
    downloading, against the pinned indexes passed in the environment and
    verifying the lock's recorded artifact hashes. :class:`ArtifactFetcher` is
    the executor-side fetcher — used for the provider distribution itself and
    available to any builder that wants to fetch directly — and its
    index-pinning, redirect, and hash refusals are unit-tested rather than
    exercised through this builder.
    """

    def __init__(self, project_dir: Path, uv_executable: str = "uv") -> None:
        self._project_dir = project_dir
        self._uv = uv_executable

    def build(self, target: Path, resolved: ResolvedSet, fetcher: ArtifactFetcher) -> None:
        if fetcher.config.air_gapped:
            raise refuse(
                RefusalCode.AIR_GAPPED_CACHE_MISS,
                "air-gapped mode is cache-only; refusing to materialize a new environment",
                materialization=resolved.marker_environment.id,
            )
        executable = shutil.which(self._uv)
        if executable is None:
            raise refuse(
                RefusalCode.NETWORK_DISABLED,
                f"{self._uv!r} is not on the path; cannot materialize a local environment",
            )
        for name in ("pyproject.toml", "uv.lock"):
            shutil.copyfile(self._project_dir / name, target / name)
        (target / "resolution.json").write_bytes(canonical_json(resolved.triples()))
        # UV_NO_CONFIG stops any ambient uv configuration from adding an index
        # the accepted artifact never named. The pinned indexes are the only ones
        # the resolver is told about, and --locked keeps the lock authoritative.
        overrides = {
            "UV_PROJECT_ENVIRONMENT": str(target / ".venv"),
            "UV_NO_CONFIG": "1",
            "UV_INDEX_URL": fetcher.config.index_urls[0],
        }
        if len(fetcher.config.index_urls) > 1:
            overrides["UV_EXTRA_INDEX_URL"] = " ".join(fetcher.config.index_urls[1:])
        env = minimal_env(overrides)
        outcome = run_with_budget(
            [executable, "sync", "--locked", "--no-dev", "--directory", str(target)],
            stdin_bytes=b"",
            budgets=Budgets(wall_clock_seconds=900.0, output_bytes=4_000_000),
            env=env,
        )
        if outcome.returncode != 0:
            raise refuse(
                RefusalCode.LOCK_MISMATCH,
                "uv sync --locked refused the recorded lock",
                returncode=outcome.returncode,
                stderr=outcome.stderr.decode("utf-8", "replace")[-2000:],
            )

    def interpreter(self, env_path: Path) -> Path:
        return env_path / ".venv" / "bin" / "python"

    def child_env(self, env_path: Path) -> Mapping[str, str]:
        # The runtime and the provider are ordinary installed dependencies of a
        # synced environment, so nothing needs to be injected on the path.
        return minimal_env()


@runtime_checkable
class ContainerDriver(Protocol):
    """The container-engine seam. No engine is invoked by this repo's tests."""

    def inspect(self, image_digest: str) -> ImageProvenance: ...

    def run(
        self,
        image_digest: str,
        *,
        argv: Sequence[str],
        stdin_bytes: bytes,
        budgets: Budgets,
        pass_fds: Sequence[int],
    ) -> ProcessOutcome: ...


class LocalEnvBackend:
    """Materialize-then-invoke against a locally isolated environment."""

    kind = "local_env"

    def __init__(
        self,
        cache: MaterializationCache,
        fetcher: ArtifactFetcher,
        builder: EnvironmentBuilder,
    ) -> None:
        self._cache = cache
        self._fetcher = fetcher
        self._builder = builder

    def materialize(self, materialization_digest: str, resolved: ResolvedSet) -> Path:
        def _build(target: Path) -> None:
            self._builder.build(target, resolved, self._fetcher)

        return self._cache.get_or_materialize(materialization_digest, _build)

    def invoke(
        self,
        env_path: Path,
        *,
        entrypoint: str,
        stdin_bytes: bytes,
        budgets: Budgets,
        pass_fds: Sequence[int] = (),
    ) -> ProcessOutcome:
        interpreter = self._builder.interpreter(env_path)
        if not interpreter.exists():
            raise refuse(
                RefusalCode.CACHE_INTEGRITY,
                f"materialized environment at {env_path} has no interpreter",
                env_path=str(env_path),
                interpreter=str(interpreter),
            )
        return run_with_budget(
            [str(interpreter), "-m", CHILD_MODULE, "--entrypoint", entrypoint],
            stdin_bytes=stdin_bytes,
            budgets=budgets,
            pass_fds=pass_fds,
            env=self._builder.child_env(env_path),
        )


class ContainerBackend:
    """Invoke a digest-pinned image after checking its recorded provenance."""

    kind = "container"

    def __init__(self, driver: ContainerDriver) -> None:
        self._driver = driver

    def verify_image(self, payload: ProviderArtifactPayload) -> ImageProvenance:
        if payload.container is None:
            raise refuse(
                RefusalCode.UNSUPPORTED_BACKEND,
                f"provider {payload.provider_id!r} carries no container pin",
                provider_id=payload.provider_id,
            )
        recorded = self._driver.inspect(payload.container.image_digest)
        expected_artifact = artifact_digest(payload)
        mismatches: dict[str, dict[str, str]] = {}
        if recorded.provider_artifact_digest != expected_artifact:
            mismatches["provider_artifact_digest"] = {
                "image": recorded.provider_artifact_digest,
                "accepted": expected_artifact,
            }
        expected = payload.container.provenance
        for name in ("materialization_digest", "base_image_digest", "builder_identity"):
            if getattr(recorded, name) != getattr(expected, name):
                mismatches[name] = {
                    "image": str(getattr(recorded, name)),
                    "accepted": str(getattr(expected, name)),
                }
        if mismatches:
            raise refuse(
                RefusalCode.IMAGE_PROVENANCE_MISMATCH,
                "container image provenance does not match the accepted Provider artifact",
                provider_id=payload.provider_id,
                image_digest=payload.container.image_digest,
                mismatches=mismatches,
            )
        return recorded

    def invoke(
        self,
        image_digest: str,
        *,
        entrypoint: str,
        stdin_bytes: bytes,
        budgets: Budgets,
        pass_fds: Sequence[int] = (),
    ) -> ProcessOutcome:
        return self._driver.run(
            image_digest,
            argv=["python", "-m", CHILD_MODULE, "--entrypoint", entrypoint],
            stdin_bytes=stdin_bytes,
            budgets=budgets,
            pass_fds=pass_fds,
        )
