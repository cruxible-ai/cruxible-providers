"""What a declared marker environment's tag list actually stands for.

A marker environment declares a handful of tags — ``cp311-cp311-manylinux_2_17_x86_64``,
``py3-none-any`` — and the resolver has to decide which of a package's wheels
that environment can install. Reading the declared list as a *set* and matching
by exact string membership is wrong, and wrong in the direction that quietly
produces an unpinnable environment: the tag scheme is an **ordering**, not a
vocabulary of names.

* PEP 600: a ``manylinux_2_5`` wheel installs on a ``manylinux_2_28`` host, and
  ``manylinux1``/``manylinux2010``/``manylinux2014`` are spellings of
  ``manylinux_2_5``/``_2_12``/``_2_17``.
* PEP 425: a ``py3-none-any`` wheel installs anywhere; a ``py3-none-<platform>``
  wheel installs on any interpreter of that major version on that platform; an
  ``abi3`` wheel built against ``cp39`` installs on ``cp311``.

Three literal tags cannot enumerate the dozen platform tags a real binary
closure reaches for across its packages, which is why no engine environment was
pinnable before this module existed: a browser driver ships
``py3-none-manylinux1_x86_64`` and ``py3-none-macosx_11_0_universal2``, an OCR
runtime ships ``cp311-cp311-manylinux1_x86_64``, and a declared list of three
literal names matched none of them.

So the declared tags are read as what they are — the *most preferred* tag in
each family the environment supports — and expanded here into the full ordered
list an installer would compute, most preferred first. The expansion is derived
from the declared tags and the declared markers and from **nothing else**: never
from the running interpreter, because deriving it from whoever happened to run
would produce a materialization digest that cannot be reproduced anywhere else.

The declared list remains what a digest preimage carries. This expansion is a
function of it, and lives outside every preimage.

Read that for exactly what it says. It means an environment is not re-keyed *by*
the expansion getting wider; it does **not** mean existing pins survive one. A
wider expansion admits artifacts a narrower one refused, so a package can resolve
to a different wheel, and a materialization digest hashes the resolution. This
module's first widening moved four environment pins for that reason, and
``docs/packaging.md`` names them.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .errors import RefusalCode, refuse

__all__ = ["InterpreterProfile", "compatible_tags"]

_MANYLINUX_RE = re.compile(r"^manylinux_(?P<major>\d+)_(?P<minor>\d+)_(?P<arch>.+)$")
_MANYLINUX_LEGACY_RE = re.compile(
    r"^(?P<name>manylinux1|manylinux2010|manylinux2014)_(?P<arch>.+)$"
)
_MUSLLINUX_RE = re.compile(r"^musllinux_(?P<major>\d+)_(?P<minor>\d+)_(?P<arch>.+)$")
_MACOS_RE = re.compile(r"^macosx_(?P<major>\d+)_(?P<minor>\d+)_(?P<arch>.+)$")
_PYTHON_TAG_RE = re.compile(r"^(?P<implementation>[a-z]+)(?P<major>\d)(?P<minor>\d*)$")

_MANYLINUX_LEGACY_GLIBC = {
    "manylinux1": (2, 5),
    "manylinux2010": (2, 12),
    "manylinux2014": (2, 17),
}
_MANYLINUX_LEGACY_NAME = {glibc: name for name, glibc in _MANYLINUX_LEGACY_GLIBC.items()}

_MACOS_BINARY_FORMATS: dict[str, tuple[str, ...]] = {
    "arm64": ("arm64", "universal2"),
    "x86_64": ("x86_64", "intel", "fat64", "fat32", "universal2", "universal"),
}
"""The archives a macOS architecture can run, most specific first.

A universal build is installable on either architecture, and the pre-Apple-silicon
fat formats are installable on Intel; anything else is only itself.
"""

_MACOS_OLDEST_MINOR = 4
"""``macosx_10_4`` is the oldest 10.x deployment target still tagged in the wild."""


@dataclass(frozen=True)
class InterpreterProfile:
    """The interpreter a marker environment describes.

    Read out of the declared markers rather than out of the declared tags,
    because the markers state it unambiguously and a tag list need not: an
    environment may legitimately declare only ``py3-none-any``.
    """

    implementation: str
    major: int
    minor: int

    @property
    def tag(self) -> str:
        return f"{self.implementation}{self.major}{self.minor}"


_IMPLEMENTATION_TAGS = {"cpython": "cp", "pypy": "pp", "ironpython": "ip", "jython": "jy"}


def _profile(markers: Mapping[str, str]) -> InterpreterProfile:
    name = markers.get("implementation_name", "cpython")
    version = markers.get("python_version", "")
    major, _, minor = version.partition(".")
    try:
        return InterpreterProfile(
            implementation=_IMPLEMENTATION_TAGS.get(name, name[:2]),
            major=int(major),
            minor=int(minor),
        )
    except ValueError as exc:
        raise refuse(
            RefusalCode.NO_COMPATIBLE_ARTIFACT,
            f"marker environment declares an unreadable python_version {version!r}",
            python_version=version,
        ) from exc


def _expand_linux(major: int, minor: int, arch: str, *, musl: bool) -> list[str]:
    """Every glibc/musl platform tag at or below the declared floor, newest first.

    ``linux_<arch>`` closes the list because an untagged Linux wheel is
    installable on Linux; it sorts last because it promises the least.
    """

    family = "musllinux" if musl else "manylinux"
    platforms: list[str] = []
    for candidate in range(minor, -1, -1):
        platforms.append(f"{family}_{major}_{candidate}_{arch}")
        if not musl:
            legacy = _MANYLINUX_LEGACY_NAME.get((major, candidate))
            if legacy is not None:
                platforms.append(f"{legacy}_{arch}")
    platforms.append(f"linux_{arch}")
    return platforms


def _expand_macos(major: int, minor: int, arch: str) -> list[str]:
    """Every macOS deployment target at or below the declared one, newest first.

    The 11.0 discontinuity is real rather than cosmetic: Big Sur runs 10.x wheels,
    so an ``arm64``/``x86_64`` host declared at ``11.0`` accepts both the 11+
    series and the whole 10.x series beneath it.
    """

    formats = _MACOS_BINARY_FORMATS.get(arch, (arch,))
    versions: list[tuple[int, int]] = []
    if major >= 11:
        versions += [(candidate, 0) for candidate in range(major, 10, -1)]
        versions += [(10, candidate) for candidate in range(16, _MACOS_OLDEST_MINOR - 1, -1)]
    else:
        versions += [(major, candidate) for candidate in range(minor, -1, -1)]
    return [
        f"macosx_{version[0]}_{version[1]}_{binary}" for version in versions for binary in formats
    ]


def _expand_platform(platform: str) -> list[str]:
    """The platform tags a declared platform tag stands for, most preferred first."""

    if platform == "any":
        return ["any"]
    match = _MANYLINUX_RE.match(platform)
    if match is not None:
        return _expand_linux(int(match["major"]), int(match["minor"]), match["arch"], musl=False)
    match = _MANYLINUX_LEGACY_RE.match(platform)
    if match is not None:
        major, minor = _MANYLINUX_LEGACY_GLIBC[match["name"]]
        return _expand_linux(major, minor, match["arch"], musl=False)
    match = _MUSLLINUX_RE.match(platform)
    if match is not None:
        return _expand_linux(int(match["major"]), int(match["minor"]), match["arch"], musl=True)
    match = _MACOS_RE.match(platform)
    if match is not None:
        return _expand_macos(int(match["major"]), int(match["minor"]), match["arch"])
    # Windows and anything this module has not been taught: itself and nothing
    # more. Inventing an ordering for a scheme nobody has described here would
    # be the fail-open reading.
    return [platform]


def _expand_interpreters(
    declared: Sequence[tuple[str, str]], profile: InterpreterProfile
) -> list[str]:
    """The ``<python>-<abi>`` prefixes this interpreter can install, best first.

    Order follows what an installer computes: this interpreter's own ABI, then
    its ``abi3`` and ABI-less spellings, then the older ``abi3`` versions it is
    forward-compatible with, then the generic ``py`` series.
    """

    prefixes: list[str] = []
    for python, abi in declared:
        if abi not in {"abi3", "none"} and f"{python}-{abi}" not in prefixes:
            prefixes.append(f"{python}-{abi}")
    if profile.implementation == "cp":
        prefixes.append(f"{profile.tag}-abi3")
    prefixes.append(f"{profile.tag}-none")
    if profile.implementation == "cp":
        for minor in range(profile.minor - 1, 1, -1):
            prefixes.append(f"cp{profile.major}{minor}-abi3")
    prefixes.append(f"py{profile.major}{profile.minor}-none")
    prefixes.append(f"py{profile.major}-none")
    for minor in range(profile.minor - 1, -1, -1):
        prefixes.append(f"py{profile.major}{minor}-none")
    return prefixes


def _split(tag: str) -> tuple[str, str, str]:
    python, _, rest = tag.partition("-")
    abi, _, platform = rest.partition("-")
    if not python or not abi or not platform:
        raise refuse(
            RefusalCode.NO_COMPATIBLE_ARTIFACT,
            f"tag {tag!r} is not 'python-abi-platform'",
            tag=tag,
        )
    return python, abi, platform


def compatible_tags(markers: Mapping[str, str], declared: Sequence[str]) -> tuple[str, ...]:
    """Expand a declared tag list into the ordered list it stands for.

    The result is the cross product of the interpreter prefixes the declared
    markers admit and the platforms the declared tags admit — which is what a
    real environment's tag list is, and why an interpreter prefix drawn from one
    declared tag legitimately pairs with a platform drawn from another. That
    pairing is the whole fix: ``py3-none`` comes from ``py3-none-any`` and
    ``manylinux1_x86_64`` from ``cp311-cp311-manylinux_2_17_x86_64``, and
    ``py3-none-manylinux1_x86_64`` — a real browser-driver wheel — is a tag the
    environment supports without ever having spelled it.
    """

    profile = _profile(markers)
    pairs: list[tuple[str, str]] = []
    platforms: list[str] = []
    for tag in declared:
        python, abi, platform = _split(tag)
        pairs.append((python, abi))
        for candidate in _expand_platform(platform):
            if candidate not in platforms:
                platforms.append(candidate)
    expanded: list[str] = []
    seen: set[str] = set()
    for prefix in _expand_interpreters(pairs, profile):
        for platform in platforms:
            candidate = f"{prefix}-{platform}"
            if candidate not in seen:
                seen.add(candidate)
                expanded.append(candidate)
    return tuple(expanded)
