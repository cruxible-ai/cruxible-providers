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

Extras
------

A provider package keeps its heavy engines behind **per-engine extras**, so the
environment a bind materializes depends on which extras the implementation
requires. Extras are therefore part of the resolution input: ``resolve`` takes
the extras to select, walks the root's ``optional-dependencies`` for exactly
those, and follows dependency-level extras (``{name = "x", extra = ["y"]}``)
transitively. They reach the materialization digest through the resolved set —
selecting an extra pulls packages in, and those packages are triples in the
preimage. Nothing about extras is added to the preimage separately, because a
second, independent statement of the same fact is a second thing that can be
wrong.

The consequence for pins: one lock produces several environments, one per
selected extras set, and an accepted artifact pins each separately. See
:func:`environment_pin_key`.

Tag compatibility
-----------------

A declared tag list is read as an **ordering**, not as a set of literal names:
the tags a marker environment declares are the most preferred tag in each family
it supports, and :mod:`.tags` expands them into the full ordered list an
installer would compute. That is what makes a heavy-engine closure resolvable —
a browser driver's ``py3-none-manylinux1_x86_64`` is compatible with an
environment that declared ``cp311-cp311-manylinux_2_17_x86_64`` and
``py3-none-any``, and exact string membership said otherwise.

The declared list is still what a preimage carries; the expansion is a function
of it and enters no digest. That does **not** mean widening the expansion leaves
existing pins alone — it changes which artifact a package resolves to, and a
materialization digest hashes the resolution, so a pin computed under a narrower
expansion has to be recomputed. ``docs/packaging.md`` records which pins this
resolver's first widening moved.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from functools import cache
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, Self

from packaging.markers import InvalidMarker, Marker
from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from .canonical import SHA256_RE, normalize_sha256
from .errors import RefusalCode, refuse
from .tags import compatible_tags

__all__ = [
    "MarkerEnvironment",
    "ResolvedDistribution",
    "ResolvedSet",
    "UvLock",
    "environment_pin_key",
    "load_uv_lock",
    "resolve",
]

_WHEEL_RE = re.compile(
    r"^(?P<name>.+?)-(?P<version>.+?)(-(?P<build>\d[^-]*))?"
    r"-(?P<py>[^-]+)-(?P<abi>[^-]+)-(?P<plat>[^-]+)\.whl$"
)


@cache
def _tag_ranks(markers: tuple[tuple[str, str], ...], tags: tuple[str, ...]) -> Mapping[str, int]:
    expanded = compatible_tags(dict(markers), tags)
    return MappingProxyType({tag: rank for rank, tag in enumerate(expanded)})


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

    def tag_ranks(self) -> Mapping[str, int]:
        """Every tag this environment can install, mapped to its preference rank.

        Derived from the declared tags and markers, cached because a resolution
        asks for it once per candidate wheel and the answer depends on nothing
        that changes in between.
        """

        return _tag_ranks(tuple(sorted(self.markers.items())), self.tags)

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
    """One entry in a resolved set.

    ``artifact_id`` is the entry's content identity. For a registry artifact it
    is the ``sha256:<hex>`` the lock records. For a local source — only ever
    admitted under the dev-only escape hatch below — it is
    ``editable:<path>`` or ``directory:<path>``, using the path exactly as the
    lock spells it, because a local source has no content hash to pin.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    version: str
    artifact_id: str
    kind: Literal["wheel", "sdist", "editable", "directory"]
    filename: str = ""
    url: str = ""

    @model_validator(mode="after")
    def _identity_matches_kind(self) -> Self:
        if self.kind in {"wheel", "sdist"}:
            if not SHA256_RE.match(self.artifact_id):
                raise ValueError(
                    f"a {self.kind} entry must carry a sha256:<hex>, got {self.artifact_id!r}"
                )
        elif not self.artifact_id.startswith(f"{self.kind}:"):
            raise ValueError(
                f"a {self.kind} entry must carry a {self.kind}:<path> identity, "
                f"got {self.artifact_id!r}"
            )
        return self

    @property
    def is_local_source(self) -> bool:
        return self.kind in {"editable", "directory"}

    @property
    def triple(self) -> tuple[str, str, str]:
        return (self.name, self.version, self.artifact_id)


def environment_pin_key(marker_environment_id: str, extras: Iterable[str] = ()) -> str:
    """The key an accepted artifact pins one materialization under.

    A marker environment alone is no longer enough to name an environment: one
    lock resolves to a different closure per selected extras set, so
    ``linux-cp311`` with the document-conversion engine and ``linux-cp311``
    without it are two environments with two digests. The key renders as the
    environment id, then each selected extra, sorted::

        linux-cp311
        linux-cp311+docling
        linux-cp311+docling+paddleocr

    The no-extras spelling is the bare environment id rather than
    ``linux-cp311+``, so every pin written before extras existed still reads
    correctly. That is the one place this design accepts an asymmetry, and it
    is safe because the two spellings cannot collide: an extra name is never
    empty.
    """

    selected = sorted(set(extras))
    if not selected:
        return marker_environment_id
    return "+".join([marker_environment_id, *selected])


class ResolvedSet(BaseModel):
    """The full resolution of one lock for one marker environment."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    root_name: str
    marker_environment: MarkerEnvironment
    distributions: tuple[ResolvedDistribution, ...]
    extras: tuple[str, ...] = ()
    """The root extras this resolution selected, sorted.

    Recorded for legibility -- a resolution should be able to say what it is a
    resolution *of*. It is deliberately **not** hashed into any preimage: the
    extras' effect is the packages they pull in, and those are already triples.
    """

    @field_validator("extras")
    @classmethod
    def _sorted_extras(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(set(value)))

    def pin_key(self) -> str:
        """The key an accepted artifact pins this resolution's digest under."""

        return environment_pin_key(self.marker_environment.id, self.extras)

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
    return {f"{p}-{a}-{t}" for p in py.split(".") for a in abi.split(".") for t in plat.split(".")}


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


def _local_source(package: dict[str, Any]) -> ResolvedDistribution:
    """Represent a path/editable source, for the dev-only escape hatch.

    The path is taken verbatim from the lock, which spells it relative to the
    project, so the identity stays stable across machines. It is emphatically
    not a content hash: a local source can change under a fixed identity, which
    is the whole reason this is dev-only.
    """

    source = package["source"]
    kind: Literal["editable", "directory"] = "editable" if "editable" in source else "directory"
    return ResolvedDistribution(
        name=str(package["name"]),
        version=str(package.get("version", "")),
        artifact_id=f"{kind}:{source[kind]}",
        kind=kind,
    )


def _pick_artifact(package: dict[str, Any], env: MarkerEnvironment) -> ResolvedDistribution | None:
    name = str(package["name"])
    version = str(package["version"])
    ranked = env.tag_ranks()
    best: tuple[int, str, dict[str, Any]] | None = None
    for wheel in package.get("wheels", []) or []:
        url = str(wheel.get("url", ""))
        filename = url.rsplit("/", 1)[-1]
        match = _WHEEL_RE.match(filename)
        if match is None:
            continue
        wheel_tags = _expand_wheel_tags(match["py"], match["abi"], match["plat"])
        ranks = [ranked[tag] for tag in wheel_tags if tag in ranked]
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
            artifact_id=_artifact_hash(wheel, name),
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
            artifact_id=_artifact_hash(sdist, name),
            kind="sdist",
            filename=url.rsplit("/", 1)[-1],
            url=url,
        )
    return None


def _is_registry(package: dict[str, Any]) -> bool:
    source = package.get("source")
    return isinstance(source, dict) and "registry" in source


def _requirements(
    package: dict[str, Any],
    extras: frozenset[str],
    env: MarkerEnvironment,
    *,
    package_name: str,
) -> list[tuple[str, frozenset[str]]]:
    """The dependency edges out of ``package`` with ``extras`` selected.

    Base dependencies always; each selected extra's ``optional-dependencies``
    list on top. A requested extra the lock entry does not declare refuses
    (``unknown_extra``) rather than resolving to the base set: silently
    materializing an environment without the engine an implementation declared
    it needs is precisely the failure this whole apparatus is built to make
    impossible.
    """

    optional = package.get("optional-dependencies") or {}
    entries: list[dict[str, Any]] = list(package.get("dependencies", []) or [])
    for extra in sorted(extras):
        if extra not in optional:
            raise refuse(
                RefusalCode.UNKNOWN_EXTRA,
                f"package {package_name!r} declares no extra {extra!r}",
                package=package_name,
                extra=extra,
                declared=sorted(optional),
            )
        entries.extend(optional[extra] or [])

    edges: list[tuple[str, frozenset[str]]] = []
    for dependency in entries:
        if not env.evaluate(dependency.get("marker")):
            continue
        edges.append((str(dependency["name"]), frozenset(dependency.get("extra", []) or [])))
    return edges


def resolve(
    lock: UvLock,
    root_name: str,
    env: MarkerEnvironment,
    *,
    extras: Sequence[str] = (),
    allow_editable_dev_sources: bool = False,
) -> ResolvedSet:
    """Resolve ``lock`` for ``env``, starting from ``root_name``.

    Dev-dependency groups are never traversed: they do not enter a materialized
    provider environment. Root extras are traversed only when ``extras`` names
    them, and dependency-level extras (``{name = "x", extra = ["y"]}``) are
    followed wherever they appear — the launch document-plane lock reaches its
    engine through three of them, and dropping those edges would produce an
    environment pin that does not cover the environment.

    Every non-root dependency must come from a registry. A path, git, editable,
    or direct-URL dependency has no artifact hash, so it cannot be pinned, and
    silently dropping it — the original behaviour — produced an environment pin
    that did not cover part of the environment. Such a source now refuses with
    ``unresolvable_source``.

    ``allow_editable_dev_sources`` is a **development-only** escape hatch, false
    by default. It admits local sources into the resolved set under a
    path-derived identity so that a monorepo can bind its own in-tree packages
    before they are published. A pin computed with it set is not a production
    pin: :meth:`Binding.snapshot` records that it was used, so a receipt shows
    it. Accepted Provider artifacts cannot be produced this way in any case —
    ``DistributionPin`` requires a distribution sha256 that a local source does
    not have.
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
    seen: set[tuple[str, frozenset[str]]] = set()
    queue: list[tuple[str, frozenset[str]]] = [(root_name, frozenset(extras))]
    while queue:
        name, selected = queue.pop()
        if (name, selected) in seen:
            continue
        seen.add((name, selected))
        if name not in entries:
            raise refuse(
                RefusalCode.LOCK_MISMATCH,
                f"lock references package {name!r} but declares no entry for it",
                package=name,
            )
        package = _select_entry(name, entries[name], env)
        queue.extend(_requirements(package, selected, env, package_name=name))
        if name in resolved:
            # Reached again under a different extras set. The artifact a package
            # resolves to does not depend on which of its extras were asked for,
            # so the entry is already correct; only its edges were new.
            continue
        if name == root_name:
            # The provider distribution itself is pinned separately by the
            # accepted artifact, and enters the materialization preimage as the
            # root identity rather than as a resolved entry.
            continue
        if not _is_registry(package):
            if not allow_editable_dev_sources:
                raise refuse(
                    RefusalCode.UNRESOLVABLE_SOURCE,
                    f"dependency {name!r} comes from a non-registry source and cannot "
                    "be pinned; a provider environment admits registry artifacts only",
                    package=name,
                    source=package.get("source"),
                )
            resolved[name] = _local_source(package)
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
        extras=tuple(extras),
    )
