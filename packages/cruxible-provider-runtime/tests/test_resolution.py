"""Lock resolution for an explicit marker environment."""

from __future__ import annotations

import pytest

from cruxible_provider_runtime.errors import RefusalCode, RefusalError
from cruxible_provider_runtime.resolution import MarkerEnvironment, UvLock, resolve


def test_resolution_follows_markers_and_skips_dev_dependencies(
    golden_lock: UvLock, marker_environments: dict[str, MarkerEnvironment]
) -> None:
    resolved = resolve(golden_lock, "sample-provider", marker_environments["linux-cp311"])
    names = [d.name for d in resolved.distributions]

    assert "leaf-windows-only" not in names, "a win32-gated dependency must not resolve on linux"
    assert "leaf-dev-only" not in names, "dev groups never enter a materialized environment"
    assert "sample-provider" not in names, "the provider itself is pinned by the accepted artifact"
    assert names == ["leaf-native", "leaf-pure", "leaf-sdist-only", "leaf-transitive"]


def test_resolution_is_platform_specific(
    golden_lock: UvLock, marker_environments: dict[str, MarkerEnvironment]
) -> None:
    linux = resolve(golden_lock, "sample-provider", marker_environments["linux-cp311"])
    macos = resolve(golden_lock, "sample-provider", marker_environments["macos-arm-cp311"])

    linux_native = next(d for d in linux.distributions if d.name == "leaf-native")
    macos_native = next(d for d in macos.distributions if d.name == "leaf-native")

    assert linux_native.sha256 != macos_native.sha256
    assert "manylinux" in linux_native.filename
    assert "arm64" in macos_native.filename


def test_windows_environment_pulls_the_gated_dependency(
    golden_lock: UvLock, marker_environments: dict[str, MarkerEnvironment]
) -> None:
    resolved = resolve(golden_lock, "sample-provider", marker_environments["windows-cp312"])
    assert "leaf-windows-only" in [d.name for d in resolved.distributions]


def test_sdist_only_package_resolves_to_its_sdist(
    golden_lock: UvLock, linux_env: MarkerEnvironment
) -> None:
    resolved = resolve(golden_lock, "sample-provider", linux_env)
    sdist = next(d for d in resolved.distributions if d.name == "leaf-sdist-only")
    assert sdist.kind == "sdist"


def test_unknown_root_refuses(golden_lock: UvLock, linux_env: MarkerEnvironment) -> None:
    with pytest.raises(RefusalError) as exc:
        resolve(golden_lock, "not-in-the-lock", linux_env)
    assert exc.value.code is RefusalCode.LOCK_MISMATCH


def test_missing_artifact_hash_refuses(linux_env: MarkerEnvironment) -> None:
    lock = UvLock(
        lock_sha256="sha256:" + "0" * 64,
        requires_python=">=3.11",
        packages=(
            {
                "name": "root",
                "version": "1.0.0",
                "source": {"editable": "."},
                "dependencies": [{"name": "unhashed"}],
            },
            {
                "name": "unhashed",
                "version": "1.0.0",
                "source": {"registry": "https://pypi.example/simple"},
                "wheels": [{"url": "https://pypi.example/files/unhashed-1.0.0-py3-none-any.whl"}],
            },
        ),
    )
    with pytest.raises(RefusalError) as exc:
        resolve(lock, "root", linux_env)
    assert exc.value.code is RefusalCode.LOCK_MISSING_HASH


def test_no_compatible_artifact_refuses(linux_env: MarkerEnvironment) -> None:
    lock = UvLock(
        lock_sha256="sha256:" + "0" * 64,
        requires_python=">=3.11",
        packages=(
            {
                "name": "root",
                "version": "1.0.0",
                "source": {"editable": "."},
                "dependencies": [{"name": "wrong-platform"}],
            },
            {
                "name": "wrong-platform",
                "version": "1.0.0",
                "source": {"registry": "https://pypi.example/simple"},
                "wheels": [
                    {
                        "url": "https://pypi.example/files/wrong_platform-1.0.0-cp311-cp311-win_amd64.whl",
                        "hash": "sha256:" + "b" * 64,
                    }
                ],
            },
        ),
    )
    with pytest.raises(RefusalError) as exc:
        resolve(lock, "root", linux_env)
    assert exc.value.code is RefusalCode.NO_COMPATIBLE_ARTIFACT


def test_ambiguous_fork_refuses(linux_env: MarkerEnvironment) -> None:
    forked = {
        "name": "forked",
        "source": {"registry": "https://pypi.example/simple"},
        "wheels": [
            {
                "url": "https://pypi.example/files/forked-1.0.0-py3-none-any.whl",
                "hash": "sha256:" + "c" * 64,
            }
        ],
    }
    lock = UvLock(
        lock_sha256="sha256:" + "0" * 64,
        requires_python=">=3.11",
        packages=(
            {
                "name": "root",
                "version": "1.0.0",
                "source": {"editable": "."},
                "dependencies": [{"name": "forked"}],
            },
            {**forked, "version": "1.0.0"},
            {**forked, "version": "2.0.0"},
        ),
    )
    with pytest.raises(RefusalError) as exc:
        resolve(lock, "root", linux_env)
    assert exc.value.code is RefusalCode.LOCK_AMBIGUOUS_FORK


def test_resolution_markers_disambiguate_a_fork(
    marker_environments: dict[str, MarkerEnvironment]
) -> None:
    def entry(version: str, marker: str, digest_char: str) -> dict[str, object]:
        return {
            "name": "forked",
            "version": version,
            "source": {"registry": "https://pypi.example/simple"},
            "resolution-markers": [marker],
            "wheels": [
                {
                    "url": f"https://pypi.example/files/forked-{version}-py3-none-any.whl",
                    "hash": "sha256:" + digest_char * 64,
                }
            ],
        }

    lock = UvLock(
        lock_sha256="sha256:" + "0" * 64,
        requires_python=">=3.11",
        packages=(
            {
                "name": "root",
                "version": "1.0.0",
                "source": {"editable": "."},
                "dependencies": [{"name": "forked"}],
            },
            entry("1.0.0", "sys_platform == 'linux'", "d"),
            entry("2.0.0", "sys_platform == 'darwin'", "e"),
        ),
    )
    linux = resolve(lock, "root", marker_environments["linux-cp311"])
    macos = resolve(lock, "root", marker_environments["macos-arm-cp311"])
    assert linux.distributions[0].version == "1.0.0"
    assert macos.distributions[0].version == "2.0.0"


def test_marker_environment_requires_the_marker_variables_it_names() -> None:
    with pytest.raises(ValueError, match="missing"):
        MarkerEnvironment(id="broken", markers={"python_version": "3.11"}, tags=("py3-none-any",))
