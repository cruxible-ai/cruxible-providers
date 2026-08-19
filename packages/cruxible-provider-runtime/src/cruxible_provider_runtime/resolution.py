"""Lock resolution for an explicit marker environment.

The materialization digest hashes the *resolution*, never the lock file's bytes:
lock formats churn across resolver releases, and one lock resolves differently
per platform (CPU-vs-GPU wheels being the loud quantitative case). This module
turns a committed ``uv.lock`` plus an explicit ``(python version, platform tag)``
marker environment into a deterministic, sorted resolved set of
``(name, version, artifact sha256)`` triples.

Everything fails closed: a registry package without a recorded artifact hash, a
fork the marker environment cannot disambiguate, or a package with no artifact
compatible with the declared tags each raise a typed refusal.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any, Literal

from packaging.markers import InvalidMarker, Marker
from pydantic import BaseModel, ConfigDict, field_validator

from .canonical import SHA256_RE, normalize_sha256
from .errors import RefusalCode, refuse

__all__ = [
    "MarkerEnvironment",
    "ResolvedDistribution",
    "ResolvedSet",
    "UvLock",
    "load_uv_lock",
    "resolve",
]

_WHEEL_RE = re.compile(
    r"^(?P<name>.+?)-(?P<version>.+?)(-(?P<build>\d[^-]*))?"
    r"-(?P<py>[^-]+)-(?P<abi>[^-]+)-(?P<plat>[^-]+)\.whl$"
)


class MarkerEnvironment(BaseModel):
    """An explicit target environment: PEP 508 markers plus compatible tags.

    ``tags`` is the ordered list of acceptable wheel tags (``py-abi-plat``),
    most preferred first. Declaring the tag list explicitly — rather than
    deriving it from the running interpreter — is what makes a materialization
    digest reproducible off the machine that computed it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    markers: dict[str, str]
    tags: tuple[str, ...]

    @field_validator("markers")
    @classmethod
    def _required_markers(cls, value: dict[str, str]) -> dict[str, str]:
        required = {"python_version", "python_full_version", "sys_platform", "platform_machine"}
        missing = required - set(value)
        if missing:
            raise ValueError(f"marker environment is missing {sorted(missing)}")
        return value

    @field_validator("tags")
    @classmethod
    def _tags_non_empty(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("a marker environment must declare at least one compatible tag")
        for tag in value:
            if tag.count("-") != 2:
                raise ValueError(f"tag must be 'py-abi-plat', got {tag!r}")
        return value

    @property
    def python_version(self) -> str:
        return self.markers["python_version"]

    def digest_payload(self) -> dict[str, Any]:
        """The environment as it enters a materialization preimage.

        ``id`` is a human label and is deliberately excluded: renaming an
        environment must not re-key an environment pin.
        """

        return {"markers": dict(sorted(self.markers.items())), "tags": list(self.tags)}

    def evaluate(self, marker: str | None) -> bool:
        if marker is None:
            return True
        try:
            return bool(Marker(marker).evaluate(dict(self.markers)))
        except InvalidMarker as exc:
            raise refuse(
                RefusalCode.LOCK_MISMATCH,
                f"lock carries an unparseable marker: {marker!r}",
                marker=marker,
            ) from exc


class ResolvedDistribution(BaseModel):
    """One fetched artifact in a resolved set."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    version: str
    sha256: str
    kind: Literal["wheel", "sdist"]
    filename: str
    url: str

    @field_validator("sha256")
    @classmethod
    def _digest(cls, value: str) -> str:
        if not SHA256_RE.match(value):
            raise ValueError(f"expected sha256:<hex>, got {value!r}")
        return value

    @property
    def triple(self) -> tuple[str, str, str]:
        return (self.name, self.version, self.sha256)


class ResolvedSet(BaseModel):
    """The full resolution of one lock for one marker environment."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    root_name: str
    marker_environment: MarkerEnvironment
    distributions: tuple[ResolvedDistribution, ...]

    @field_validator("distributions")
    @classmethod
    def _sorted_unique(
        cls, value: tuple[ResolvedDistribution, ...]
    ) -> tuple[ResolvedDistribution, ...]:
        names = [d.name for d in value]
        if len(set(names)) != len(names):
            raise ValueError(f"resolved set contains duplicate names: {names}")
        return tuple(sorted(value, key=lambda d: d.triple))

    def triples(self) -> list[list[str]]:
        return [list(d.triple) for d in sorted(self.distributions, key=lambda d: d.triple)]


class UvLock(BaseModel):
    """A parsed ``uv.lock``: the packages, keyed by name, plus the raw bytes' digest."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    lock_sha256: str
    requires_python: str | None
    packages: tuple[dict[str, Any], ...]


def load_uv_lock(path: Path) -> UvLock:
    """Read and parse a ``uv.lock``."""

    raw = path.read_bytes()
    try:
        document = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise refuse(
            RefusalCode.LOCK_MISMATCH,
            f"lock file at {path} is not valid TOML",
            path=str(path),
        ) from exc
    packages = document.get("package", [])
    if not isinstance(packages, list) or not packages:
        raise refuse(
            RefusalCode.LOCK_MISMATCH,
            f"lock file at {path} declares no packages",
            path=str(path),
        )
    import hashlib

    return UvLock(
        lock_sha256=f"sha256:{hashlib.sha256(raw).hexdigest()}",
        requires_python=document.get("requires-python"),
        packages=tuple(packages),
    )


def _entries_by_name(lock: UvLock) -> dict[str, list[dict[str, Any]]]:
    entries: dict[str, list[dict[str, Any]]] = {}
    for package in lock.packages:
        entries.setdefault(str(package["name"]), []).append(package)
    return entries


def _select_entry(
    name: str, candidates: list[dict[str, Any]], env: MarkerEnvironment
) -> dict[str, Any]:
    if len(candidates) == 1:
        return candidates[0]
    matching = [
        candidate
        for candidate in candidates
        if any(env.evaluate(marker) for marker in candidate.get("resolution-markers", []))
    ]
    if len(matching) == 1:
        return matching[0]
    raise refuse(
        RefusalCode.LOCK_AMBIGUOUS_FORK,
        f"lock forks package {name!r} into {len(candidates)} entries and marker "
        f"environment {env.id!r} selects {len(matching)}",
        package=name,
        marker_environment=env.id,
        versions=[str(candidate.get("version")) for candidate in candidates],
    )


def _expand_wheel_tags(py: str, abi: str, plat: str) -> set[str]:
    return {
        f"{p}-{a}-{t}"
        for p in py.split(".")
        for a in abi.split(".")
        for t in plat.split(".")
    }


def _artifact_hash(entry: dict[str, Any], name: str) -> str:
    hash_value = entry.get("hash")
    if not isinstance(hash_value, str):
        raise refuse(
            RefusalCode.LOCK_MISSING_HASH,
            f"lock entry for {name!r} records no artifact hash",
            package=name,
        )
    try:
        return normalize_sha256(hash_value.removeprefix("sha256:"))
    except ValueError as exc:
        raise refuse(
            RefusalCode.LOCK_MISSING_HASH,
            f"lock entry for {name!r} records a non-sha256 hash",
            package=name,
            hash=hash_value,
        ) from exc


def _pick_artifact(
    package: dict[str, Any], env: MarkerEnvironment
) -> ResolvedDistribution | None:
    name = str(package["name"])
    version = str(package["version"])
    best: tuple[int, str, dict[str, Any]] | None = None
    for wheel in package.get("wheels", []) or []:
        url = str(wheel.get("url", ""))
        filename = url.rsplit("/", 1)[-1]
        match = _WHEEL_RE.match(filename)
        if match is None:
            continue
        wheel_tags = _expand_wheel_tags(match["py"], match["abi"], match["plat"])
        ranks = [env.tags.index(tag) for tag in wheel_tags if tag in env.tags]
        if not ranks:
            continue
        rank = min(ranks)
        if best is None or (rank, filename) < (best[0], best[1]):
            best = (rank, filename, wheel)
    if best is not None:
        wheel = best[2]
        url = str(wheel["url"])
        return ResolvedDistribution(
            name=name,
            version=version,
            sha256=_artifact_hash(wheel, name),
            kind="wheel",
            filename=best[1],
            url=url,
        )
    sdist = package.get("sdist")
    if isinstance(sdist, dict) and "url" in sdist:
        url = str(sdist["url"])
        return ResolvedDistribution(
            name=name,
            version=version,
            sha256=_artifact_hash(sdist, name),
            kind="sdist",
            filename=url.rsplit("/", 1)[-1],
            url=url,
        )
    return None


def _is_registry(package: dict[str, Any]) -> bool:
    source = package.get("source")
    return isinstance(source, dict) and "registry" in source


def resolve(lock: UvLock, root_name: str, env: MarkerEnvironment) -> ResolvedSet:
    """Resolve ``lock`` for ``env``, starting from ``root_name``.

    Only runtime dependencies are traversed; dev-dependency groups and optional
    extras of the root are excluded, because they never enter a materialized
    provider environment.
    """

    entries = _entries_by_name(lock)
    if root_name not in entries:
        raise refuse(
            RefusalCode.LOCK_MISMATCH,
            f"lock declares no package named {root_name!r}",
            root=root_name,
            known=sorted(entries),
        )

    resolved: dict[str, ResolvedDistribution] = {}
    seen: set[str] = set()
    queue: list[str] = [root_name]
    while queue:
        name = queue.pop()
        if name in seen:
            continue
        seen.add(name)
        if name not in entries:
            raise refuse(
                RefusalCode.LOCK_MISMATCH,
                f"lock references package {name!r} but declares no entry for it",
                package=name,
            )
        package = _select_entry(name, entries[name], env)
        for dependency in package.get("dependencies", []) or []:
            if env.evaluate(dependency.get("marker")):
                queue.append(str(dependency["name"]))
        if name == root_name or not _is_registry(package):
            # The provider distribution itself is pinned separately by the
            # accepted artifact; local/virtual sources are not fetched artifacts.
            continue
        distribution = _pick_artifact(package, env)
        if distribution is None:
            raise refuse(
                RefusalCode.NO_COMPATIBLE_ARTIFACT,
                f"no artifact for {name!r} is compatible with marker environment {env.id!r}",
                package=name,
                marker_environment=env.id,
                tags=list(env.tags),
            )
        resolved[name] = distribution

    return ResolvedSet(
        root_name=root_name,
        marker_environment=env,
        distributions=tuple(resolved.values()),
    )
