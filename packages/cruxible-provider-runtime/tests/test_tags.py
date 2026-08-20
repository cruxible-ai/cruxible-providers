"""Tag compatibility: a declared tag list is an ordering, not a vocabulary.

The two halves under test pull against each other and both matter. A wheel built
for an *older* platform floor installs on a newer host and must resolve — that
half is what makes a heavy-engine environment pinnable at all. A wheel built for
a *newer* floor does not install on an older host and must still refuse — that
half is what keeps the first one from being a fail-open reading of the same rule.
"""

from __future__ import annotations

import pytest
from cruxible_provider_runtime.errors import RefusalCode, RefusalError
from cruxible_provider_runtime.resolution import MarkerEnvironment, UvLock, resolve
from cruxible_provider_runtime.tags import compatible_tags

MACOS_ENV = MarkerEnvironment(
    id="macos-arm-cp312",
    markers={
        "implementation_name": "cpython",
        "os_name": "posix",
        "platform_machine": "arm64",
        "python_full_version": "3.12.6",
        "python_version": "3.12",
        "sys_platform": "darwin",
    },
    tags=(
        "cp312-cp312-macosx_11_0_arm64",
        "cp312-abi3-macosx_11_0_arm64",
        "py3-none-any",
    ),
)


def _lock_with(filename: str) -> UvLock:
    """A one-dependency lock whose only artifact is the named wheel."""

    return UvLock(
        lock_sha256="sha256:" + "0" * 64,
        requires_python=">=3.11",
        packages=(
            {
                "name": "root",
                "version": "1.0.0",
                "source": {"editable": "."},
                "dependencies": [{"name": "engine"}],
            },
            {
                "name": "engine",
                "version": "1.0.0",
                "source": {"registry": "https://pypi.example/simple"},
                "wheels": [
                    {
                        "url": f"https://pypi.example/files/{filename}",
                        "hash": "sha256:" + "1" * 64,
                    }
                ],
            },
        ),
    )


# Every one of these is a real shape a launch engine publishes, and none of them
# is among the three literal tags a launch environment declares.
INSTALLABLE_ON_LINUX_CP311 = [
    pytest.param("engine-1.0.0-py3-none-manylinux1_x86_64.whl", id="browser-driver"),
    pytest.param("engine-1.0.0-cp311-cp311-manylinux1_x86_64.whl", id="accelerator-runtime"),
    pytest.param("engine-1.0.0-cp311-cp311-manylinux_2_12_x86_64.whl", id="older-glibc-floor"),
    pytest.param("engine-1.0.0-cp311-cp311-manylinux2014_x86_64.whl", id="legacy-alias"),
    pytest.param("engine-1.0.0-cp39-abi3-manylinux_2_17_x86_64.whl", id="older-abi3"),
    pytest.param("engine-1.0.0-py311-none-manylinux_2_17_x86_64.whl", id="version-pinned-pure"),
    pytest.param("engine-1.0.0-py3-none-linux_x86_64.whl", id="untagged-linux"),
]


@pytest.mark.parametrize("filename", INSTALLABLE_ON_LINUX_CP311)
def test_a_wheel_an_installer_would_take_resolves(
    filename: str, linux_env: MarkerEnvironment
) -> None:
    resolved = resolve(_lock_with(filename), "root", linux_env)
    assert [d.filename for d in resolved.distributions] == [filename]


UNINSTALLABLE_ON_LINUX_CP311 = [
    pytest.param("engine-1.0.0-cp311-cp311-manylinux_2_28_x86_64.whl", id="newer-glibc-floor"),
    pytest.param("engine-1.0.0-cp311-cp311-manylinux_2_17_aarch64.whl", id="other-architecture"),
    pytest.param("engine-1.0.0-cp311-cp311-musllinux_1_2_x86_64.whl", id="other-libc"),
    pytest.param("engine-1.0.0-cp312-cp312-manylinux_2_17_x86_64.whl", id="other-interpreter"),
    pytest.param("engine-1.0.0-cp313-abi3-manylinux_2_17_x86_64.whl", id="newer-abi3"),
    pytest.param("engine-1.0.0-py4-none-any.whl", id="other-python-major"),
]


@pytest.mark.parametrize("filename", UNINSTALLABLE_ON_LINUX_CP311)
def test_a_wheel_an_installer_would_refuse_still_refuses(
    filename: str, linux_env: MarkerEnvironment
) -> None:
    """Ordering cuts both ways; a newer floor is not a compatible floor."""

    with pytest.raises(RefusalError) as exc:
        resolve(_lock_with(filename), "root", linux_env)
    assert exc.value.code is RefusalCode.NO_COMPATIBLE_ARTIFACT


@pytest.mark.parametrize(
    "filename",
    [
        pytest.param("engine-1.0.0-cp312-cp312-macosx_10_13_universal2.whl", id="universal2"),
        pytest.param("engine-1.0.0-cp312-cp312-macosx_10_9_arm64.whl", id="older-target"),
        pytest.param("engine-1.0.0-cp39-abi3-macosx_11_0_arm64.whl", id="older-abi3"),
    ],
)
def test_macos_accepts_what_the_platform_actually_runs(filename: str) -> None:
    resolved = resolve(_lock_with(filename), "root", MACOS_ENV)
    assert [d.filename for d in resolved.distributions] == [filename]


def test_macos_refuses_a_newer_deployment_target() -> None:
    with pytest.raises(RefusalError) as exc:
        resolve(_lock_with("engine-1.0.0-cp312-cp312-macosx_14_0_arm64.whl"), "root", MACOS_ENV)
    assert exc.value.code is RefusalCode.NO_COMPATIBLE_ARTIFACT


def test_the_newest_compatible_wheel_wins(linux_env: MarkerEnvironment) -> None:
    """Compatibility decides what is admissible; the ordering decides which one."""

    lock = UvLock(
        lock_sha256="sha256:" + "0" * 64,
        requires_python=">=3.11",
        packages=(
            {
                "name": "root",
                "version": "1.0.0",
                "source": {"editable": "."},
                "dependencies": [{"name": "engine"}],
            },
            {
                "name": "engine",
                "version": "1.0.0",
                "source": {"registry": "https://pypi.example/simple"},
                "wheels": [
                    {
                        "url": "https://pypi.example/files/engine-1.0.0-py3-none-any.whl",
                        "hash": "sha256:" + "1" * 64,
                    },
                    {
                        "url": (
                            "https://pypi.example/files/"
                            "engine-1.0.0-cp311-cp311-manylinux1_x86_64.whl"
                        ),
                        "hash": "sha256:" + "2" * 64,
                    },
                    {
                        "url": (
                            "https://pypi.example/files/"
                            "engine-1.0.0-cp311-cp311-manylinux_2_17_x86_64.whl"
                        ),
                        "hash": "sha256:" + "3" * 64,
                    },
                ],
            },
        ),
    )
    resolved = resolve(lock, "root", linux_env)
    assert [d.filename for d in resolved.distributions] == [
        "engine-1.0.0-cp311-cp311-manylinux_2_17_x86_64.whl"
    ]


def test_the_expansion_pairs_interpreters_and_platforms_across_declared_tags(
    linux_env: MarkerEnvironment,
) -> None:
    """The cross product is the fix, not a side effect of it.

    ``py3-none`` is only ever declared alongside ``any``, and ``manylinux`` only
    alongside ``cp311-cp311``. A browser driver ships the pairing of the two,
    which the environment supports and never spelled.
    """

    expanded = compatible_tags(linux_env.markers, linux_env.tags)
    assert "py3-none-manylinux1_x86_64" in expanded
    assert set(linux_env.tags) <= set(expanded)


def test_the_expansion_is_ordered_most_preferred_first(linux_env: MarkerEnvironment) -> None:
    ranks = linux_env.tag_ranks()
    assert ranks["cp311-cp311-manylinux_2_17_x86_64"] < ranks["cp311-cp311-manylinux1_x86_64"]
    assert ranks["cp311-cp311-manylinux1_x86_64"] < ranks["cp311-cp311-linux_x86_64"]
    assert ranks["cp311-cp311-manylinux_2_17_x86_64"] < ranks["py3-none-any"]


def test_the_expansion_never_reaches_a_digest_preimage(linux_env: MarkerEnvironment) -> None:
    """The declared list is the identity; the expansion is a function of it.

    If the expansion entered the preimage, teaching this module one more platform
    family would re-key every environment pin ever computed.
    """

    payload = linux_env.digest_payload()
    assert payload["tags"] == list(linux_env.tags)
    assert len(compatible_tags(linux_env.markers, linux_env.tags)) > len(linux_env.tags)
