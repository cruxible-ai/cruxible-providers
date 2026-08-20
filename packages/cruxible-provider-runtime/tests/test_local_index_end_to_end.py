"""The production local builder, driven end to end against a local index.

This is the lane the RP-0 residual promised and did not have. Everything before
it covered ``UvSyncBuilder``'s argument construction and its post-build
verification, and nothing ran the builder: the class exported the project's
dependencies with ``--no-emit-project``, synced those, and stopped — sealing a
dependency-only tree under a materialization digest that names a provider
distribution whose bytes were never fetched. A later invocation could not import
the provider at all.

Nothing here reaches the network. The index is a PEP 503 tree under ``file://``
holding wheels this test builds with ``uv build`` from packages it writes itself;
the closure is those packages and nothing else, so the sync has a real, hashed
requirement to install and still needs no registry. The transport is a real
filesystem transport rather than a stand-in, because a ``file:`` index is a
posture the contract supports rather than a testing convenience.

One thing is scoped out and worth naming. The sealed environment cannot carry
``cruxible_provider_runtime`` itself: the runtime is unpublished, so a local
index cannot serve it without also serving its third-party closure, which no
offline lane can produce. So the invocation proof here runs the sealed
interpreter over the *pinned entrypoint path*, resolving it exactly as the child
harness does, rather than through the harness. That covers what F-001 broke —
the provider distribution is present and importable with nothing injected on the
path — and the harness itself is covered end to end by the reference provider's
suite.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest
from cruxible_provider_runtime.artifact import DistributionPin
from cruxible_provider_runtime.backends import (
    LocalEnvBackend,
    MaterializationRequest,
    UvSyncBuilder,
    find_site_packages,
    installed_distributions,
)
from cruxible_provider_runtime.cache import MaterializationCache
from cruxible_provider_runtime.errors import RefusalCode, RefusalError
from cruxible_provider_runtime.index import ArtifactFetcher, IndexConfig, TransportResponse
from cruxible_provider_runtime.resolution import (
    MarkerEnvironment,
    ResolvedDistribution,
    ResolvedSet,
)

pytestmark = pytest.mark.skipif(
    shutil.which("uv") is None, reason="the production local builder shells out to uv"
)

ROOT_NAME = "sample-provider"
DEP_NAME = "sample-dep"
ENGINE_NAME = "sample-engine"
VERSION = "0.1.0"
ENTRYPOINT = "sample_provider.provider:Echo"

# Resolves the pinned entrypoint the way cruxible_provider_runtime.child does --
# import the module, walk the object path, instantiate a class -- and calls it.
# Run by the sealed interpreter, with the builder's own child environment, so a
# provider that is not installed cannot be reached by any other route.
_INVOKE = """
import importlib, json, sys

module_name, _, object_path = sys.argv[1].partition(":")
target = importlib.import_module(module_name)
for part in object_path.split("."):
    target = getattr(target, part)
if isinstance(target, type):
    target = target()
print(json.dumps(target(sys.argv[2])))
"""


@dataclass(frozen=True)
class LocalIndex:
    """A file:// PEP 503 index plus the project whose lock resolves against it."""

    index_url: str
    project_dir: Path
    lock_path: Path
    root_wheel: Path
    hashes: dict[str, str]

    def pin(self) -> DistributionPin:
        return DistributionPin(
            name=ROOT_NAME,
            version=VERSION,
            filename=self.root_wheel.name,
            sha256=self.hashes[self.root_wheel.name],
            index_url=self.index_url,
            url=self.root_wheel.as_uri(),
        )


class FilesystemTransport:
    """Reads an artifact off the filesystem, and reports what it was asked for."""

    def __init__(self) -> None:
        self.requested: list[str] = []

    def get(self, url: str) -> TransportResponse:
        self.requested.append(url)
        path = Path(url.removeprefix("file://"))
        if not path.is_file():
            return TransportResponse(status=404, final_url=url, body=b"")
        return TransportResponse(status=200, final_url=url, body=path.read_bytes())


def _write_package(directory: Path, name: str, module: str, body: str, extra_table: str) -> Path:
    package = directory / name
    (package / "src" / module).mkdir(parents=True)
    (package / "pyproject.toml").write_text(
        f'[project]\nname = "{name}"\nversion = "{VERSION}"\n'
        f'requires-python = ">=3.11"\ndependencies = {extra_table}\n\n'
        '[build-system]\nrequires = ["hatchling"]\nbuild-backend = "hatchling.build"\n\n'
        f'[tool.hatch.build.targets.wheel]\npackages = ["src/{module}"]\n',
        encoding="utf-8",
    )
    (package / "src" / module / "__init__.py").write_text(body, encoding="utf-8")
    return package


def _uv(*args: str, cwd: Path | None = None) -> None:
    result = subprocess.run(["uv", *args], cwd=cwd, capture_output=True, text=True, check=False)
    if result.returncode != 0:  # pragma: no cover - a broken fixture, not a finding
        raise AssertionError(f"uv {' '.join(args)} failed:\n{result.stdout}\n{result.stderr}")


@pytest.fixture(scope="session")
def local_index(tmp_path_factory: pytest.TempPathFactory) -> LocalIndex:
    """Three wheels, a PEP 503 tree under file://, and a lock resolved from it."""

    workspace = tmp_path_factory.mktemp("local-index")
    sources = workspace / "sources"
    sources.mkdir()

    _write_package(sources, DEP_NAME, "sample_dep", 'NAME = "dep"\n', "[]")
    _write_package(sources, ENGINE_NAME, "sample_engine", 'NAME = "engine"\n', "[]")
    root = _write_package(sources, ROOT_NAME, "sample_provider", "", f'["{DEP_NAME}"]')
    (root / "pyproject.toml").write_text(
        (root / "pyproject.toml").read_text(encoding="utf-8")
        + f'\n[project.optional-dependencies]\nengine = ["{ENGINE_NAME}"]\n',
        encoding="utf-8",
    )
    (root / "src" / "sample_provider" / "provider.py").write_text(
        "class Echo:\n"
        '    interface_id = "sample.echo"\n\n'
        "    def __call__(self, text: str) -> dict[str, str]:\n"
        '        return {"echo": text, "module": __name__}\n',
        encoding="utf-8",
    )

    simple = workspace / "index" / "simple"
    hashes: dict[str, str] = {}
    for name in (DEP_NAME, ENGINE_NAME, ROOT_NAME):
        target = simple / name
        target.mkdir(parents=True)
        _uv("build", "--wheel", "--no-config", "-o", str(target), str(sources / name))
        wheels = sorted(target.glob("*.whl"))
        links = []
        for wheel in wheels:
            digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
            hashes[wheel.name] = f"sha256:{digest}"
            links.append(f'    <a href="{wheel.name}#sha256={digest}">{wheel.name}</a><br/>')
        (target / "index.html").write_text(
            "<!DOCTYPE html>\n<html><body>\n" + "\n".join(links) + "\n</body></html>\n",
            encoding="utf-8",
        )
    (simple / "index.html").write_text(
        "<!DOCTYPE html>\n<html><body>\n"
        + "\n".join(f'<a href="{name}/">{name}</a><br/>' for name in sorted(hashes))
        + "\n</body></html>\n",
        encoding="utf-8",
    )

    index_url = simple.as_uri()
    project = workspace / "project"
    shutil.copytree(root, project)
    _uv("lock", "--no-config", "--default-index", index_url, cwd=project)

    return LocalIndex(
        index_url=index_url,
        project_dir=project,
        lock_path=project / "uv.lock",
        root_wheel=simple / ROOT_NAME / f"sample_provider-{VERSION}-py3-none-any.whl",
        hashes=hashes,
    )


def _resolved(index: LocalIndex, environment: MarkerEnvironment, *extras: str) -> ResolvedSet:
    """The closure as the bind would hand it to the builder.

    Built here rather than run through ``resolve()`` because this test's subject
    is the builder: the resolver has its own suite, and a local-index lock spells
    its artifacts as paths relative to the index rather than as URLs.
    """

    names = [DEP_NAME, *([ENGINE_NAME] if "engine" in extras else [])]
    return ResolvedSet(
        root_name=ROOT_NAME,
        marker_environment=environment,
        extras=extras,
        distributions=tuple(
            ResolvedDistribution(
                name=name,
                version=VERSION,
                artifact_id=index.hashes[f"{name.replace('-', '_')}-{VERSION}-py3-none-any.whl"],
                kind="wheel",
                filename=f"{name.replace('-', '_')}-{VERSION}-py3-none-any.whl",
                url=(
                    Path(index.index_url.removeprefix("file://"))
                    / name
                    / f"{name.replace('-', '_')}-{VERSION}-py3-none-any.whl"
                ).as_uri(),
            )
            for name in names
        ),
    )


def _build(
    index: LocalIndex, target: Path, environment: MarkerEnvironment, *extras: str
) -> FilesystemTransport:
    transport = FilesystemTransport()
    UvSyncBuilder().build(
        MaterializationRequest(
            target=target,
            resolved=_resolved(index, environment, *extras),
            fetcher=ArtifactFetcher(IndexConfig(index_urls=(index.index_url,)), transport),
            project_dir=index.project_dir,
            lock_path=index.lock_path,
            distribution=index.pin(),
        )
    )
    return transport


def test_the_pinned_root_is_fetched_hash_verified_and_installed(
    local_index: LocalIndex, linux_env: MarkerEnvironment, tmp_path: Path
) -> None:
    transport = _build(local_index, tmp_path, linux_env)

    assert transport.requested == [local_index.pin().url]
    kept = tmp_path / "artifact" / local_index.root_wheel.name
    assert kept.read_bytes() == local_index.root_wheel.read_bytes()

    installed = installed_distributions(find_site_packages(tmp_path))
    assert installed["sample-provider"] == VERSION
    assert installed["sample-dep"] == VERSION


def test_the_sealed_interpreter_imports_and_invokes_the_pinned_entrypoint(
    local_index: LocalIndex, linux_env: MarkerEnvironment, tmp_path: Path
) -> None:
    """The property F-001 broke, asserted through a process rather than a mock."""

    builder = UvSyncBuilder()
    _build(local_index, tmp_path, linux_env)

    result = subprocess.run(
        [str(builder.interpreter(tmp_path)), "-c", _INVOKE, ENTRYPOINT, "hello"],
        env=dict(builder.child_env(tmp_path)),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "echo": "hello",
        "module": "sample_provider.provider",
    }


def test_the_selected_extra_reaches_the_environment(
    local_index: LocalIndex, linux_env: MarkerEnvironment, tmp_path: Path
) -> None:
    """An environment built for an extra must contain the extra's packages.

    The export received no extras before this fix, so an implementation that
    declared an engine bound an environment without one -- and every digest over
    that environment still verified, because the closure it was checked against
    never mentioned the engine either.
    """

    base = tmp_path / "base"
    base.mkdir()
    _build(local_index, base, linux_env)
    assert "sample-engine" not in installed_distributions(find_site_packages(base))

    with_engine = tmp_path / "with-engine"
    with_engine.mkdir()
    _build(local_index, with_engine, linux_env, "engine")
    assert installed_distributions(find_site_packages(with_engine))["sample-engine"] == VERSION


def test_a_corrupted_index_entry_refuses_before_anything_is_built(
    local_index: LocalIndex, linux_env: MarkerEnvironment, tmp_path: Path
) -> None:
    """The fetch is a hash check, not a download."""

    wrong = local_index.pin().model_copy(update={"sha256": "sha256:" + "ab" * 32})
    with pytest.raises(RefusalError) as exc:
        UvSyncBuilder().build(
            MaterializationRequest(
                target=tmp_path,
                resolved=_resolved(local_index, linux_env),
                fetcher=ArtifactFetcher(
                    IndexConfig(index_urls=(local_index.index_url,)), FilesystemTransport()
                ),
                project_dir=local_index.project_dir,
                lock_path=local_index.lock_path,
                distribution=wrong,
            )
        )
    assert exc.value.code is RefusalCode.ARTIFACT_HASH_MISMATCH
    assert not (tmp_path / ".venv").exists()


def test_a_bind_with_a_cold_cache_seals_a_verified_tree(
    local_index: LocalIndex, linux_env: MarkerEnvironment, tmp_path: Path
) -> None:
    """The whole path: fresh cache, real build, seal, re-verify."""

    cache = MaterializationCache(tmp_path / "cache")
    backend = LocalEnvBackend(
        cache,
        ArtifactFetcher(IndexConfig(index_urls=(local_index.index_url,)), FilesystemTransport()),
        UvSyncBuilder(),
    )
    digest = "sha256:" + "c0" * 32
    env_path = backend.materialize(
        digest,
        _resolved(local_index, linux_env),
        project_dir=local_index.project_dir,
        lock_path=local_index.lock_path,
        distribution=local_index.pin(),
    )
    assert cache.verify(digest) == env_path
    assert installed_distributions(find_site_packages(env_path))["sample-provider"] == VERSION


def test_a_missing_root_pin_refuses_rather_than_sealing_a_dependency_only_tree(
    local_index: LocalIndex, linux_env: MarkerEnvironment, tmp_path: Path
) -> None:
    with pytest.raises(RefusalError) as exc:
        UvSyncBuilder().build(
            MaterializationRequest(
                target=tmp_path,
                resolved=_resolved(local_index, linux_env),
                fetcher=ArtifactFetcher(
                    IndexConfig(index_urls=(local_index.index_url,)), FilesystemTransport()
                ),
                project_dir=local_index.project_dir,
                lock_path=local_index.lock_path,
            )
        )
    assert exc.value.code is RefusalCode.UNRESOLVABLE_SOURCE
