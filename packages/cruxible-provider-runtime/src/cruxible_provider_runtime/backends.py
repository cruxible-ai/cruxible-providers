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
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from .artifact import DistributionPin, ImageProvenance, ProviderArtifactPayload, artifact_digest
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
    "MaterializationRequest",
    "UvSyncBuilder",
    "find_site_packages",
    "installed_distributions",
    "verify_environment",
]

CHILD_MODULE = "cruxible_provider_runtime.child"


@dataclass(frozen=True)
class MaterializationRequest:
    """Everything a builder needs to populate one staging directory.

    ``project_dir`` and ``lock_path`` come from the bind, not from the builder's
    constructor. A builder holding its own idea of which project to sync can
    materialize a tree that has nothing to do with the lock the bind verified,
    and then seal it under that lock's digest.

    ``distribution`` is the accepted artifact's root pin. It travels with the
    request for the same reason: the sha256 that feeds the implementation digest
    is a fact the *bind* established, and a builder that fetched the root from
    anywhere else would be installing something the digest does not describe.
    """

    target: Path
    resolved: ResolvedSet
    fetcher: ArtifactFetcher
    project_dir: Path | None = None
    lock_path: Path | None = None
    distribution: DistributionPin | None = None


@runtime_checkable
class EnvironmentBuilder(Protocol):
    """Populates a staging directory with a materialized provider environment.

    A builder MUST verify its own output against ``request.resolved`` before
    returning. The cache seals whatever the builder leaves behind, so a builder
    that returns without checking has sealed an unverified tree under a verified
    digest — which is the one thing the whole pinning apparatus exists to
    prevent.
    """

    def build(self, request: MaterializationRequest) -> None: ...

    def interpreter(self, env_path: Path) -> Path: ...

    def child_env(self, env_path: Path) -> Mapping[str, str]: ...


def installed_distributions(site_packages: Path) -> dict[str, str]:
    """Read ``name -> version`` from the ``.dist-info`` directories in a tree."""

    installed: dict[str, str] = {}
    for entry in sorted(site_packages.glob("*.dist-info")):
        stem = entry.name.removesuffix(".dist-info")
        name, _, version = stem.rpartition("-")
        if not name:
            continue
        installed[canonicalize_name(name)] = version
    return installed


def find_site_packages(env_path: Path) -> Path:
    """Locate the ``site-packages`` of a materialized virtual environment."""

    candidates = sorted(env_path.glob(".venv/lib/python*/site-packages"))
    if not candidates:
        candidates = sorted(env_path.glob(".venv/Lib/site-packages"))
    if not candidates:
        raise refuse(
            RefusalCode.ENVIRONMENT_DIVERGENCE,
            f"materialized environment at {env_path} contains no site-packages",
            env_path=str(env_path),
        )
    return candidates[0]


def _same_version(installed: str, expected: str) -> bool:
    """Whether two version strings name the same release under PEP 440.

    The two sides come from different places and normalise differently: a
    manifest is written by a person and may say ``1.0``, while the ``.dist-info``
    a build backend produced says ``1.0.0``. Those are one version, and refusing
    the environment over the spelling would report divergence where there is
    none — which is the expensive kind of false alarm, because the answer to it
    is to stop believing the check.

    An unparseable version on either side falls back to an exact comparison
    rather than being waved through: a version nobody can parse is not a version
    this can vouch for.
    """

    if installed == expected:
        return True
    try:
        return Version(installed) == Version(expected)
    except InvalidVersion:
        return False


def verify_environment(
    env_path: Path, resolved: ResolvedSet, *, root: DistributionPin | None = None
) -> None:
    """Refuse unless the materialized tree matches the resolution it claims.

    Compares the installed ``(name, version)`` set against the resolution's
    registry entries, plus the root distribution when one was pinned. The root
    is checked separately because it is not a resolved entry: the resolution
    covers the closure, and the accepted artifact pins the root. A tree missing
    it is a dependency-only environment sealed under a digest that names a
    provider, which is precisely the failure this argument exists to catch.
    Local sources admitted under the dev-only escape hatch are exempt, because
    they have no pinned version to compare against.

    Known limit, stated rather than papered over: ``.dist-info`` records a name
    and a version, not the sha256 of the artifact the file came from. This check
    therefore catches "this tree is not that resolution" at name-and-version
    granularity. The artifact-hash guarantee comes from the installer being
    given hash-pinned requirements and from the root being hash-verified before
    it is installed — see :class:`UvSyncBuilder` — and this is the independent
    check that the installer did what it was told.
    """

    installed = installed_distributions(find_site_packages(env_path))
    expected = {
        canonicalize_name(entry.name): entry.version
        for entry in resolved.distributions
        if not entry.is_local_source
    }
    if root is not None:
        expected[canonicalize_name(root.name)] = root.version
    missing = sorted(name for name in expected if name not in installed)
    mismatched = sorted(
        name
        for name, version in expected.items()
        if name in installed and not _same_version(installed[name], version)
    )
    if missing or mismatched:
        raise refuse(
            RefusalCode.ENVIRONMENT_DIVERGENCE,
            "the materialized environment does not match the resolution it was built from",
            env_path=str(env_path),
            missing=missing,
            mismatched={
                name: {"expected": expected[name], "installed": installed[name]}
                for name in mismatched
            },
        )


class UvSyncBuilder:
    """The production local builder: dependencies by sync, the root by artifact.

    An environment is two things and they arrive by two paths, deliberately:

    * **the closure** comes from ``uv export`` into hash-pinned requirements and
      ``uv pip sync --require-hashes``, so **every entry's** artifact hash is
      asserted by the installer. ``uv sync --locked`` alone asserts that the lock
      is current, which is a different claim. The export selects exactly the
      extras the resolution selected — an environment built without the engine
      an implementation declared would still verify against a closure that never
      contained it;
    * **the root** is fetched through the :class:`~.index.ArtifactFetcher`, from
      the pinned index and at the sha256 the accepted artifact pins, and
      installed from those exact bytes. That sha256 is what the *implementation
      digest* covers, so installing the root any other way would seal a tree
      under a digest describing an artifact the tree never contained. The export
      passes ``--no-emit-project`` because of this, not despite it: the root is
      not the sync's business.

    Two smaller choices, each closing a hole the first cut of this class had:
    the project and lock come from the :class:`MaterializationRequest`, so the
    tree that gets sealed is built from the lock the bind actually verified; and
    indexes are pinned as explicit command-line flags rather than the legacy
    ``UV_INDEX_URL``/``UV_EXTRA_INDEX_URL`` environment variables, which a
    project's own ``[[tool.uv.index]]`` table overrides. ``--no-config`` keeps
    ambient configuration out.

    Nothing seals unchecked: the finished tree is verified against the resolution
    *and* against the root pin before the cache seals it.
    """

    def __init__(self, uv_executable: str = "uv") -> None:
        self._uv = uv_executable

    # -- argument construction (pure, and therefore testable) ---------------

    @staticmethod
    def export_argv(
        uv: str, project_dir: Path, requirements: Path, extras: Sequence[str] = ()
    ) -> list[str]:
        argv = [
            uv,
            "export",
            "--locked",
            "--no-dev",
            "--no-emit-project",
            "--no-config",
            "--directory",
            str(project_dir),
        ]
        for extra in sorted(set(extras)):
            argv += ["--extra", extra]
        argv += ["--format", "requirements-txt", "--output-file", str(requirements)]
        return argv

    @staticmethod
    def sync_argv(
        uv: str, interpreter: Path, requirements: Path, index_urls: Sequence[str]
    ) -> list[str]:
        argv = [
            uv,
            "pip",
            "sync",
            "--require-hashes",
            "--no-config",
            "--python",
            str(interpreter),
            "--index-url",
            index_urls[0],
        ]
        for extra in index_urls[1:]:
            argv += ["--extra-index-url", extra]
        argv.append(str(requirements))
        return argv

    @staticmethod
    def install_root_argv(uv: str, interpreter: Path, artifact: Path) -> list[str]:
        """Install the fetched root artifact and nothing else.

        ``--no-deps`` because the closure is already installed and pinned;
        ``--no-index`` because this install must not be able to reach a registry
        at all — the bytes on disk have already been hash-checked against the
        accepted pin, and anything an index could contribute here would be
        unpinned by construction.
        """

        return [
            uv,
            "pip",
            "install",
            "--no-deps",
            "--no-index",
            "--no-config",
            "--python",
            str(interpreter),
            str(artifact),
        ]

    # -- the build ---------------------------------------------------------

    def build(self, request: MaterializationRequest) -> None:
        if request.fetcher.config.air_gapped:
            raise refuse(
                RefusalCode.AIR_GAPPED_CACHE_MISS,
                "air-gapped mode is cache-only; refusing to materialize a new environment",
                materialization=request.resolved.marker_environment.id,
            )
        if request.project_dir is None or request.lock_path is None:
            raise refuse(
                RefusalCode.LOCK_MISMATCH,
                "a uv-synced environment needs the project and lock the bind verified",
            )
        if request.distribution is None:
            raise refuse(
                RefusalCode.UNRESOLVABLE_SOURCE,
                "a uv-synced environment needs the root distribution the accepted "
                "artifact pinned; there is no other way to install the provider itself",
                root=request.resolved.root_name,
            )
        executable = shutil.which(self._uv)
        if executable is None:
            raise refuse(
                RefusalCode.NETWORK_DISABLED,
                f"{self._uv!r} is not on the path; cannot materialize a local environment",
            )

        target = request.target
        pin = request.distribution
        shutil.copyfile(request.lock_path, target / "uv.lock")
        (target / "resolution.json").write_bytes(canonical_json(request.resolved.triples()))
        requirements = target / "requirements.txt"
        budgets = Budgets(wall_clock_seconds=900.0, output_bytes=4_000_000)
        env = minimal_env({"UV_NO_CONFIG": "1"})

        # Fetched before anything is built. The fetcher refuses an unpinned
        # index, a redirect, and a hash mismatch, so reaching the next line means
        # the bytes on disk are the ones the accepted artifact names — and the
        # sealed tree keeps them, so what was installed stays inspectable.
        artifact = target / "artifact" / pin.filename
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(request.fetcher.fetch_url(pin.url, pin.sha256, pin.name))

        exported = run_with_budget(
            self.export_argv(
                executable, request.project_dir, requirements, request.resolved.extras
            ),
            stdin_bytes=b"",
            budgets=budgets,
            env=env,
        )
        if exported.returncode != 0:
            raise refuse(
                RefusalCode.LOCK_MISMATCH,
                "uv export --locked refused the recorded lock",
                returncode=exported.returncode,
                stderr=exported.stderr.decode("utf-8", "replace")[-2000:],
            )

        created = run_with_budget(
            [executable, "venv", "--no-config", str(target / ".venv")],
            stdin_bytes=b"",
            budgets=budgets,
            env=env,
        )
        if created.returncode != 0:
            raise refuse(
                RefusalCode.ENVIRONMENT_DIVERGENCE,
                "could not create the isolated environment",
                returncode=created.returncode,
                stderr=created.stderr.decode("utf-8", "replace")[-2000:],
            )

        synced = run_with_budget(
            self.sync_argv(
                executable,
                self.interpreter(target),
                requirements,
                request.fetcher.config.index_urls,
            ),
            stdin_bytes=b"",
            budgets=budgets,
            env=env,
        )
        if synced.returncode != 0:
            raise refuse(
                RefusalCode.ARTIFACT_HASH_MISMATCH,
                "uv pip sync --require-hashes refused the exported requirements",
                returncode=synced.returncode,
                stderr=synced.stderr.decode("utf-8", "replace")[-2000:],
            )

        installed = run_with_budget(
            self.install_root_argv(executable, self.interpreter(target), artifact),
            stdin_bytes=b"",
            budgets=budgets,
            env=env,
        )
        if installed.returncode != 0:
            raise refuse(
                RefusalCode.ENVIRONMENT_DIVERGENCE,
                f"the pinned root distribution {pin.name!r} could not be installed",
                returncode=installed.returncode,
                artifact=pin.filename,
                stderr=installed.stderr.decode("utf-8", "replace")[-2000:],
            )

        # Nothing seals unchecked.
        verify_environment(target, request.resolved, root=pin)

    def interpreter(self, env_path: Path) -> Path:
        return env_path / ".venv" / "bin" / "python"

    def child_env(self, env_path: Path) -> Mapping[str, str]:
        # Nothing is injected on the path, and that is now a claim rather than an
        # oversight: the provider distribution is installed into this
        # environment's own site-packages from the pinned artifact, and the
        # runtime arrives as its ordinary dependency. An environment that needed
        # a source path injected here would be one where the provider was never
        # installed.
        del env_path
        return minimal_env()


@runtime_checkable
class ContainerDriver(Protocol):
    """The container-engine seam. No engine is invoked by this repo's tests.

    Contract for ``run``: ``argv`` **replaces** the image's entrypoint — it is
    the complete command line, not arguments appended to one. An image whose
    ``ENTRYPOINT`` also invokes the child harness would therefore run it twice,
    so provider images set an empty ``ENTRYPOINT`` and carry the invocation in
    ``CMD``, where a supplied ``argv`` displaces it.
    """

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

    def materialize(
        self,
        materialization_digest: str,
        resolved: ResolvedSet,
        *,
        project_dir: Path | None = None,
        lock_path: Path | None = None,
        distribution: DistributionPin | None = None,
    ) -> Path:
        def _build(target: Path) -> None:
            self._builder.build(
                MaterializationRequest(
                    target=target,
                    resolved=resolved,
                    fetcher=self._fetcher,
                    project_dir=project_dir,
                    lock_path=lock_path,
                    distribution=distribution,
                )
            )

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
