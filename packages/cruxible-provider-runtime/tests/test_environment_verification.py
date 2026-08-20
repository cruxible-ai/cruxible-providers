"""A materialized tree must be checked against its resolution before it seals.

The cache seals whatever the builder leaves behind. That makes the builder's
self-check the load-bearing step: without it, a sealed entry carries a verified
*digest* over an unverified *tree*, and every later bind re-verifies the tree
against a seal that was written over the wrong contents in the first place.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from cruxible_provider_runtime.artifact import DistributionPin
from cruxible_provider_runtime.backends import (
    MaterializationRequest,
    UvSyncBuilder,
    find_site_packages,
    installed_distributions,
    verify_environment,
)
from cruxible_provider_runtime.cache import MaterializationCache
from cruxible_provider_runtime.errors import RefusalCode, RefusalError
from cruxible_provider_runtime.index import ArtifactFetcher, IndexConfig
from cruxible_provider_runtime.resolution import (
    MarkerEnvironment,
    ResolvedDistribution,
    ResolvedSet,
    UvLock,
    resolve,
)
from cruxible_provider_runtime.testing import FakeIndexTransport, InjectedEnvironmentBuilder

INDEX = IndexConfig(index_urls=("https://index.example/simple",))

ROOT_PIN = DistributionPin(
    name="sample-provider",
    version="0.4.2",
    filename="sample_provider-0.4.2-py3-none-any.whl",
    sha256="sha256:" + "4d" * 32,
    index_url="https://index.example/simple",
    url="https://index.example/simple/sample_provider-0.4.2-py3-none-any.whl",
)


def _resolved(env: MarkerEnvironment) -> ResolvedSet:
    return ResolvedSet(
        root_name="sample-provider",
        marker_environment=env,
        distributions=(
            ResolvedDistribution(
                name="leaf-pure",
                version="1.3.0",
                artifact_id="sha256:" + "11" * 32,
                kind="wheel",
                filename="leaf_pure-1.3.0-py3-none-any.whl",
                url="https://index.example/simple/leaf_pure-1.3.0-py3-none-any.whl",
            ),
            ResolvedDistribution(
                name="sibling",
                version="0.1.0",
                artifact_id="editable:../sibling",
                kind="editable",
            ),
        ),
    )


def _stage(root: Path, installed: dict[str, str]) -> Path:
    site_packages = root / ".venv" / "lib" / "python3.11" / "site-packages"
    site_packages.mkdir(parents=True)
    for name, version in installed.items():
        (site_packages / f"{name}-{version}.dist-info").mkdir()
    return root


def test_a_matching_tree_verifies(tmp_path: Path, linux_env: MarkerEnvironment) -> None:
    root = _stage(tmp_path / "env", {"leaf_pure": "1.3.0"})
    verify_environment(root, _resolved(linux_env))


def test_a_wrong_version_refuses(tmp_path: Path, linux_env: MarkerEnvironment) -> None:
    root = _stage(tmp_path / "env", {"leaf_pure": "9.9.9"})
    with pytest.raises(RefusalError) as exc:
        verify_environment(root, _resolved(linux_env))
    assert exc.value.code is RefusalCode.ENVIRONMENT_DIVERGENCE
    assert exc.value.refusal.detail["mismatched"]["leaf-pure"] == {
        "expected": "1.3.0",
        "installed": "9.9.9",
    }


def test_a_missing_distribution_refuses(tmp_path: Path, linux_env: MarkerEnvironment) -> None:
    root = _stage(tmp_path / "env", {})
    with pytest.raises(RefusalError) as exc:
        verify_environment(root, _resolved(linux_env))
    assert exc.value.code is RefusalCode.ENVIRONMENT_DIVERGENCE
    assert exc.value.refusal.detail["missing"] == ["leaf-pure"]


def test_a_tree_without_the_root_distribution_refuses(
    tmp_path: Path, linux_env: MarkerEnvironment
) -> None:
    """A dependency-only tree is not a provider environment.

    The root is not a resolved entry -- the closure covers the dependencies and
    the accepted artifact pins the provider -- so a check that consults only the
    resolution passes on a tree the provider was never installed into.
    """

    root = _stage(tmp_path / "env", {"leaf_pure": "1.3.0"})
    with pytest.raises(RefusalError) as exc:
        verify_environment(root, _resolved(linux_env), root=ROOT_PIN)
    assert exc.value.code is RefusalCode.ENVIRONMENT_DIVERGENCE
    assert exc.value.refusal.detail["missing"] == ["sample-provider"]


def test_a_tree_carrying_the_root_distribution_verifies(
    tmp_path: Path, linux_env: MarkerEnvironment
) -> None:
    root = _stage(tmp_path / "env", {"leaf_pure": "1.3.0", "sample_provider": "0.4.2"})
    verify_environment(root, _resolved(linux_env), root=ROOT_PIN)


@pytest.mark.parametrize(
    ("pinned", "installed"),
    [
        pytest.param("1.0", "1.0.0", id="omitted-patch"),
        pytest.param("1.0.0", "1.0", id="omitted-patch-other-way"),
        pytest.param("1.0.0-alpha1", "1.0.0a1", id="normalised-prerelease"),
        pytest.param("1.0.0+local", "1.0.0+local", id="local-segment"),
    ],
)
def test_two_spellings_of_one_version_are_one_version(
    tmp_path: Path, linux_env: MarkerEnvironment, pinned: str, installed: str
) -> None:
    """A manifest is written by a person; a ``.dist-info`` is written by a backend.

    They normalise differently, and reporting divergence over the spelling is
    the expensive kind of false alarm: the answer to it is to stop believing the
    check.
    """

    root = _stage(tmp_path / "env", {"leaf_pure": "1.3.0", "sample_provider": installed})
    verify_environment(
        root, _resolved(linux_env), root=ROOT_PIN.model_copy(update={"version": pinned})
    )


def test_a_version_that_cannot_be_parsed_is_compared_exactly(
    tmp_path: Path, linux_env: MarkerEnvironment
) -> None:
    """Fail closed: a version nobody can parse is not one this can vouch for."""

    root = _stage(tmp_path / "env", {"leaf_pure": "1.3.0", "sample_provider": "nonsense"})
    with pytest.raises(RefusalError) as exc:
        verify_environment(
            root, _resolved(linux_env), root=ROOT_PIN.model_copy(update={"version": "0.4.2"})
        )
    assert exc.value.code is RefusalCode.ENVIRONMENT_DIVERGENCE

    matching = _stage(tmp_path / "same", {"leaf_pure": "1.3.0", "sample_provider": "nonsense"})
    verify_environment(
        matching,
        _resolved(linux_env),
        root=ROOT_PIN.model_copy(update={"version": "nonsense"}),
    )


def test_a_genuinely_different_version_still_refuses(
    tmp_path: Path, linux_env: MarkerEnvironment
) -> None:
    """The check must not have become one that accepts everything."""

    root = _stage(tmp_path / "env", {"leaf_pure": "1.3.0", "sample_provider": "0.4.3"})
    with pytest.raises(RefusalError) as exc:
        verify_environment(root, _resolved(linux_env), root=ROOT_PIN)
    assert exc.value.code is RefusalCode.ENVIRONMENT_DIVERGENCE


def test_local_sources_are_exempt(tmp_path: Path, linux_env: MarkerEnvironment) -> None:
    """A dev-only path source has no pinned version to compare against."""

    root = _stage(tmp_path / "env", {"leaf_pure": "1.3.0"})
    verify_environment(root, _resolved(linux_env))
    assert "sibling" not in installed_distributions(find_site_packages(root))


def test_a_tree_with_no_site_packages_refuses(tmp_path: Path, linux_env: MarkerEnvironment) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(RefusalError) as exc:
        verify_environment(empty, _resolved(linux_env))
    assert exc.value.code is RefusalCode.ENVIRONMENT_DIVERGENCE


def test_name_normalisation_survives_dist_info_spelling(
    tmp_path: Path, linux_env: MarkerEnvironment
) -> None:
    """``.dist-info`` uses the escaped name; the resolution uses the raw one."""

    root = _stage(tmp_path / "env", {"Leaf_Pure": "1.3.0"})
    verify_environment(root, _resolved(linux_env))


def test_a_divergent_builder_refuses_to_seal(
    tmp_path: Path, golden_lock: UvLock, linux_env: MarkerEnvironment
) -> None:
    """End to end through the cache: divergence means no cache entry at all."""

    resolved = resolve(golden_lock, "sample-provider", linux_env)
    cache = MaterializationCache(tmp_path / "cache")
    builder = InjectedEnvironmentBuilder(stage_divergent_tree=True)
    digest = "sha256:" + "c1" * 32

    def build(target: Path) -> None:
        builder.build(
            MaterializationRequest(
                target=target,
                resolved=resolved,
                fetcher=ArtifactFetcher(INDEX, FakeIndexTransport()),
            )
        )

    with pytest.raises(RefusalError) as exc:
        cache.get_or_materialize(digest, build)
    assert exc.value.code is RefusalCode.ENVIRONMENT_DIVERGENCE
    assert not cache.path_for(digest).exists()


def test_a_matching_builder_seals(
    tmp_path: Path, golden_lock: UvLock, linux_env: MarkerEnvironment
) -> None:
    resolved = resolve(golden_lock, "sample-provider", linux_env)
    cache = MaterializationCache(tmp_path / "cache")
    builder = InjectedEnvironmentBuilder()
    digest = "sha256:" + "c2" * 32

    def build(target: Path) -> None:
        builder.build(
            MaterializationRequest(
                target=target,
                resolved=resolved,
                fetcher=ArtifactFetcher(INDEX, FakeIndexTransport()),
            )
        )

    path = cache.get_or_materialize(digest, build)
    assert cache.verify(digest) == path


def test_uv_sync_builder_needs_the_project_from_the_bind(
    tmp_path: Path, golden_lock: UvLock, linux_env: MarkerEnvironment
) -> None:
    """No constructor-held project dir to drift away from the verified lock."""

    builder = UvSyncBuilder()
    with pytest.raises(RefusalError) as exc:
        builder.build(
            MaterializationRequest(
                target=tmp_path,
                resolved=resolve(golden_lock, "sample-provider", linux_env),
                fetcher=ArtifactFetcher(INDEX, FakeIndexTransport()),
            )
        )
    assert exc.value.code is RefusalCode.LOCK_MISMATCH


def test_uv_sync_builder_refuses_to_materialize_when_air_gapped(
    tmp_path: Path, golden_lock: UvLock, linux_env: MarkerEnvironment
) -> None:
    builder = UvSyncBuilder()
    air_gapped = ArtifactFetcher(
        IndexConfig(index_urls=("https://index.example/simple",), air_gapped=True),
        FakeIndexTransport(),
    )
    with pytest.raises(RefusalError) as exc:
        builder.build(
            MaterializationRequest(
                target=tmp_path,
                resolved=resolve(golden_lock, "sample-provider", linux_env),
                fetcher=air_gapped,
                project_dir=tmp_path,
                lock_path=tmp_path / "uv.lock",
            )
        )
    assert exc.value.code is RefusalCode.AIR_GAPPED_CACHE_MISS


def test_export_argv_asks_for_a_locked_hash_pinned_export() -> None:
    argv = UvSyncBuilder.export_argv("uv", Path("/p"), Path("/p/req.txt"))
    assert "--locked" in argv
    assert "--no-dev" in argv
    assert "--no-config" in argv
    assert "--extra" not in argv
    assert argv[argv.index("--format") + 1] == "requirements-txt"


def test_export_argv_names_every_selected_extra() -> None:
    """An environment built for an extra has to contain the extra's packages."""

    argv = UvSyncBuilder.export_argv("uv", Path("/p"), Path("/p/req.txt"), ("paddleocr", "docling"))
    named = [argv[index + 1] for index, part in enumerate(argv) if part == "--extra"]
    assert named == ["docling", "paddleocr"]


def test_install_root_argv_installs_the_artifact_and_reaches_no_index() -> None:
    """The bytes were hash-checked already; an index could only add unpinned ones."""

    argv = UvSyncBuilder.install_root_argv(
        "uv", Path("/env/bin/python"), Path("/env/artifact/provider-1.0-py3-none-any.whl")
    )
    assert argv[:3] == ["uv", "pip", "install"]
    assert "--no-deps" in argv
    assert "--no-index" in argv
    assert "--no-config" in argv
    assert argv[-1] == "/env/artifact/provider-1.0-py3-none-any.whl"


def test_sync_argv_requires_hashes_and_pins_every_index() -> None:
    """The gap this closes: --locked asserts a current lock, not per-entry hashes."""

    argv = UvSyncBuilder.sync_argv(
        "uv",
        Path("/env/bin/python"),
        Path("/p/req.txt"),
        ("https://a.example/simple", "https://b.example/simple"),
    )
    assert "--require-hashes" in argv
    assert "--no-config" in argv
    assert argv[argv.index("--index-url") + 1] == "https://a.example/simple"
    assert argv[argv.index("--extra-index-url") + 1] == "https://b.example/simple"
    assert argv[-1] == "/p/req.txt"
