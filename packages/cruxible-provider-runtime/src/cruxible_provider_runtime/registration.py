"""Read and write package-owned registration data without importing provider code.

A provider package ships one ``registration.json`` (schema 3)::

    {
      "schema_version": 3,
      "manifest": {"path": "manifest.yaml", "digest": "sha256:..."},
      "operations": [
        {"name": "web.fetch",
         "description": "Retrieves one resource with one HTTP GET ...",
         "source": {"path": "packages/.../fetch.py",
                    "repository": "https://github.com/cruxible-ai/cruxible-providers"},
         "serves": ["web.fetch"],
         "in": "web.fetch.request@1", "out": "web.fetch.result@1",
         "effect": "external_read",
         "refusals": {"provider_declined": {
             "contract": "web.fetch.refusal.provider_declined@1", "absorbable": true}},
         "entrypoint": "cruxible_provider_web.fetch:WebFetch"}
      ],
      "declares": {
        "contracts": [<every Contract the operations reach, in full>],
        "capture_kinds": [<every CaptureKind they name: name, decoding, layout>],
        "built_against": [<the capability and kind versions the copies came from>]
      },
      "runtime_requirements": []
    }

The package carries its own copies of the shapes its operations accept and
return: ``declares`` holds exactly the closure reached from the operations
(their ``in``, ``out`` and refusal Contracts, every Contract those pin, every
``capture<K>`` kind they name, and each carried kind's layout rows and rule
types, transitively). The copies are never installed as instance state, and an
install needs no kit. ``built_against`` records which capability and kind
versions the copies were made from; nothing gates on it.

This is a proposal source, not acceptance authority or an installer. The host
pins the distribution bytes before trusting executable entry points; reading
metadata never imports them. Core judges the carried copies under its own laws
(digests, types, layouts, fit); this reader checks the document's shape and
that the closure is exact by name and version. Only schema 3 is read.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import deque
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from importlib.metadata import Distribution
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal, TypeAlias

from packaging.utils import canonicalize_name
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

from .canonical import SHA256_RE
from .manifest import ENTRYPOINT_GROUP, ProviderManifest, load_manifest
from .operations import (
    ContractRef,
    ProviderOperation,
    callable_path,
    validate_capability_name,
    validate_operations,
)

__all__ = [
    "MAX_BUILT_AGAINST",
    "MAX_DECLARED_CAPTURE_KINDS",
    "MAX_DECLARED_CONTRACTS",
    "MAX_OPERATIONS",
    "REGISTRATION_FILE",
    "REGISTRATION_SCHEMA_VERSION",
    "BuiltAgainst",
    "BuiltAgainstCapability",
    "BuiltAgainstCaptureKind",
    "CarriedCaptureKind",
    "CarriedContract",
    "PackageRegistration",
    "ProviderDeclares",
    "RegistrationBundle",
    "ResourceRef",
    "RuntimeRequirement",
    "check_declares",
    "declared_closure",
    "declares_problems",
    "load_registration",
    "registration_from_distribution",
    "render_registration",
    "write_registration",
]

REGISTRATION_FILE = "registration.json"
REGISTRATION_SCHEMA_VERSION = 3
MAX_OPERATIONS = 256
MAX_DECLARED_CONTRACTS = 1024
MAX_DECLARED_CAPTURE_KINDS = 256
MAX_BUILT_AGAINST = 512
MAX_CONTRACT_FIELDS = 256

_CONTRACT_NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_FIELD_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_KIND_NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,255}$")
#: Field types that pin one Contract under ``contract``.
_PINNING_TYPES = frozenset({"contract", "record", "result", "refusal"})
#: In a layout rule's type spelling: quoted text (skipped), ``name@N`` and ``capture<K>``.
_QUOTED_RE = re.compile(r'"(?:[^"\\]|\\.)*"')
_SPELLED_REF_RE = re.compile(r"(?<![A-Za-z0-9_.-])([a-z][a-z0-9_.-]{0,127})@([1-9][0-9]{0,8})")
_SPELLED_KIND_RE = re.compile(r"capture<\s*([a-z][a-z0-9_.-]{0,255})\s*>")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _utf8(value: str) -> bytes:
    return value.encode("utf-8")


def _empty(value: object) -> bool:
    return not value


def _not_none(value: object) -> bool:
    return value is None


def _sha256(value: str, *, label: str) -> str:
    if not SHA256_RE.fullmatch(value):
        raise ValueError(f"{label} must be sha256:<64 lowercase hex>")
    return value


class ResourceRef(_Strict):
    """A package file the registration names, by package-relative path and sha256."""

    path: str = Field(max_length=512)
    digest: str

    @field_validator("path")
    @classmethod
    def relative_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if (
            not value
            or path.is_absolute()
            or ".." in path.parts
            or "\\" in value
            or path.as_posix() != value
            or value == "."
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise ValueError("resource path must be a normalized package-relative path")
        return value

    @field_validator("digest")
    @classmethod
    def sha256(cls, value: str) -> str:
        return _sha256(value, label="resource digest")

    def read(self, root: Path) -> bytes:
        root = root.resolve(strict=True)
        path = root / self.path
        if path.is_symlink() or not path.resolve(strict=True).is_relative_to(root):
            raise ValueError(f"resource escapes package: {self.path}")
        data = path.read_bytes()
        if "sha256:" + hashlib.sha256(data).hexdigest() != self.digest:
            raise ValueError(f"resource digest mismatch: {self.path}")
        return data


class RuntimeRequirement(_Strict):
    """Something an implementation's environment needs beyond its lock.

    Keyed by the manifest implementation it belongs to: its ``entrypoint``.
    """

    entrypoint: str
    kind: Literal["python_extra", "runtime_resource"]
    name: str = Field(min_length=1, max_length=256)
    description: str = Field(min_length=1, max_length=2048)
    probe_entrypoint: str | None = None

    _entrypoint = field_validator("entrypoint")(callable_path)

    @model_validator(mode="after")
    def probe(self) -> RuntimeRequirement:
        if self.kind == "runtime_resource":
            if self.probe_entrypoint is None:
                raise ValueError("runtime resources require a package readiness probe")
            callable_path(self.probe_entrypoint)
        elif self.probe_entrypoint is not None:
            raise ValueError("Python extras do not use resource probes")
        return self


# -- the carried shapes ---------------------------------------------------------------------


class CarriedContract(_Strict):
    """One Contract the package carries, as a full ``cruxible-contract-v1`` document.

    The runtime reads its identity and the Contracts and kinds its field types
    name; core judges the rest (types, refinements, digests).
    """

    artifact_format: Literal["cruxible-contract-v1"] = "cruxible-contract-v1"
    name: str
    version: int = Field(ge=1, le=999_999_999)
    description: str | None = Field(default=None, exclude_if=_not_none)
    fields: dict[str, dict[str, Any]] = Field(max_length=MAX_CONTRACT_FIELDS)
    predecessor_digest: str | None = Field(default=None, exclude_if=_not_none)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if unicodedata.normalize("NFC", value) != value or not _CONTRACT_NAME_RE.fullmatch(value):
            raise ValueError(f"Contract name {value!r} is not a canonical identifier")
        return value

    @field_validator("fields")
    @classmethod
    def _fields(cls, value: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        for name, declared in value.items():
            if not _FIELD_NAME_RE.fullmatch(name):
                raise ValueError(f"Contract field name {name!r} is not a canonical identifier")
            if not isinstance(declared.get("type"), Mapping):
                raise ValueError(f"Contract field {name!r} declares no type")
        return dict(sorted(value.items(), key=lambda item: _utf8(item[0])))

    @field_validator("predecessor_digest")
    @classmethod
    def _predecessor(cls, value: str | None) -> str | None:
        return None if value is None else _sha256(value, label="a predecessor digest")

    @model_validator(mode="after")
    def _succession(self) -> CarriedContract:
        if (self.version == 1) != (self.predecessor_digest is None):
            raise ValueError(
                "version 1 of a Contract has no predecessor; every later version names "
                "its predecessor's digest"
            )
        return self

    @property
    def ref(self) -> ContractRef:
        return ContractRef(name=self.name, version=self.version)

    def named(self) -> Iterator[tuple[str, ContractRef | str]]:
        """``(field, ref)`` for every Contract and ``(field, kind)`` for every kind it names."""

        for field_name, declared in self.fields.items():
            for item in _type_names(declared["type"]):
                yield field_name, item


class CarriedCaptureKind(_Strict):
    """A CaptureKind the operations name: its name, decoding and layout, as core declares them.

    A raw kind may carry neither (the operation names it without promising a
    document shape). A layout needs a JSON or CSV decoding.
    """

    name: str
    decoding: dict[str, Any] | None = Field(default=None, exclude_if=_not_none)
    layout: dict[str, Any] | None = Field(default=None, exclude_if=_not_none)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if unicodedata.normalize("NFC", value) != value or not _KIND_NAME_RE.fullmatch(value):
            raise ValueError(f"CaptureKind name {value!r} is not a canonical identifier")
        return value

    @model_validator(mode="after")
    def _layout(self) -> CarriedCaptureKind:
        if self.layout is not None and (self.decoding or {}).get("format") not in ("json", "csv"):
            raise ValueError(f"CaptureKind {self.name!r}: a layout needs a json or csv decoding")
        return self

    def named(self) -> Iterator[tuple[str, ContractRef | str]]:
        """``(entry, ref)`` for each record set's row and each Contract a rule type names,
        and ``(entry, kind)`` for each kind a rule type names."""

        layout = self.layout or {}
        for set_name, record_set in sorted((layout.get("records") or {}).items()):
            row = record_set.get("row")
            if row is not None:
                yield f"records.{set_name}.row", _ref(row)
        for entry, rule in _layout_rules(layout):
            declared = rule.get("type")
            if isinstance(declared, str):
                yield from ((entry, item) for item in _spelled_names(declared))
            elif isinstance(declared, Mapping):
                yield from ((entry, item) for item in _type_names(declared))


class BuiltAgainstCapability(_Strict):
    """One capability version the package's copies were made from."""

    kind: Literal["Capability"] = "Capability"
    name: str
    version: int = Field(ge=1, le=999_999_999)
    digest: str

    _name = field_validator("name")(validate_capability_name)

    @field_validator("digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        return _sha256(value, label="a built_against digest")


class BuiltAgainstCaptureKind(_Strict):
    """One CaptureKind a carried copy was made from, by the digest of its declared copy."""

    kind: Literal["CaptureKind"] = "CaptureKind"
    name: str
    digest: str

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if not _KIND_NAME_RE.fullmatch(value):
            raise ValueError("a CaptureKind name is a canonical identifier")
        return value

    @field_validator("digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        return _sha256(value, label="a built_against digest")


BuiltAgainst: TypeAlias = Annotated[
    BuiltAgainstCapability | BuiltAgainstCaptureKind, Field(discriminator="kind")
]
_BUILT_AGAINST: TypeAdapter[BuiltAgainstCapability | BuiltAgainstCaptureKind] = TypeAdapter(
    BuiltAgainst
)


def _built_against_key(item: BuiltAgainstCapability | BuiltAgainstCaptureKind) -> tuple[Any, ...]:
    version = item.version if isinstance(item, BuiltAgainstCapability) else 0
    return (_utf8(item.kind), _utf8(item.name), version)


def _contract_key(item: CarriedContract) -> tuple[bytes, int]:
    return (_utf8(item.name), item.version)


class ProviderDeclares(_Strict):
    """The shapes a package carries for its operations; never installed as instance state.

    ``contracts`` are sorted by name then version, ``capture_kinds`` by name,
    ``built_against`` by kind, name, then version; each is listed once.
    """

    contracts: tuple[CarriedContract, ...] = Field(min_length=1, max_length=MAX_DECLARED_CONTRACTS)
    capture_kinds: tuple[CarriedCaptureKind, ...] = Field(
        default=(), max_length=MAX_DECLARED_CAPTURE_KINDS, exclude_if=_empty
    )
    built_against: tuple[BuiltAgainst, ...] = Field(
        default=(), max_length=MAX_BUILT_AGAINST, exclude_if=_empty
    )

    @field_validator("contracts")
    @classmethod
    def _contracts(cls, value: tuple[CarriedContract, ...]) -> tuple[CarriedContract, ...]:
        keys = [_contract_key(item) for item in value]
        if keys != sorted(set(keys)):
            raise ValueError("declared Contracts are sorted by name and version, once each")
        return value

    @field_validator("capture_kinds")
    @classmethod
    def _capture_kinds(
        cls, value: tuple[CarriedCaptureKind, ...]
    ) -> tuple[CarriedCaptureKind, ...]:
        names = [_utf8(item.name) for item in value]
        if names != sorted(set(names)):
            raise ValueError("declared CaptureKinds are sorted by name, once each")
        return value

    @field_validator("built_against")
    @classmethod
    def _built_against(
        cls, value: tuple[BuiltAgainstCapability | BuiltAgainstCaptureKind, ...]
    ) -> tuple[BuiltAgainstCapability | BuiltAgainstCaptureKind, ...]:
        keys = [_built_against_key(item) for item in value]
        if keys != sorted(set(keys)):
            raise ValueError("built_against is sorted by kind, name and version, once each")
        return value

    def contract(self, ref: ContractRef) -> CarriedContract | None:
        key = (ref.name, ref.version)
        return next((item for item in self.contracts if (item.name, item.version) == key), None)

    def capture_kind(self, name: str) -> CarriedCaptureKind | None:
        return next((item for item in self.capture_kinds if item.name == name), None)


class PackageRegistration(_Strict):
    """``registration.json``, schema 3, exactly as a package ships it.

    ``manifest`` names the package's runtime manifest by path and digest;
    ``operations`` are sorted by name, once each; ``declares`` carries every
    shape they reach and nothing else.
    """

    schema_version: Literal[3]
    manifest: ResourceRef
    operations: tuple[ProviderOperation, ...] = Field(min_length=1, max_length=MAX_OPERATIONS)
    declares: ProviderDeclares
    runtime_requirements: tuple[RuntimeRequirement, ...] = ()

    @field_validator("operations")
    @classmethod
    def _sorted_unique(cls, value: tuple[ProviderOperation, ...]) -> tuple[ProviderOperation, ...]:
        return validate_operations(value)


# -- the closure -----------------------------------------------------------------------------


def _ref(value: object) -> ContractRef:
    if isinstance(value, str):
        return ContractRef.parse(value)
    return ContractRef.model_validate(value)


def _type_names(declared: Mapping[str, Any]) -> Iterator[ContractRef | str]:
    """The Contracts one field type pins and the CaptureKinds it names, depth first."""

    kind = declared.get("kind")
    if kind in _PINNING_TYPES:
        yield _ref(declared["contract"])
    elif kind == "variant":
        for arm in (declared.get("arms") or {}).values():
            yield _ref(arm)
    elif kind == "list":
        yield from _type_names(declared["item"])
    elif kind == "field_result":
        yield from _type_names(declared["value"])
    elif kind == "capture":
        name = declared.get("capture_kind", "*")
        if name != "*":
            yield str(name)


def _spelled_names(spelling: str) -> Iterator[ContractRef | str]:
    """The ``name@N`` Contracts and ``capture<K>`` kinds a type spelling names."""

    bare = _QUOTED_RE.sub('""', spelling)
    for name, version in _SPELLED_REF_RE.findall(bare):
        yield ContractRef(name=name, version=int(version))
    yield from _SPELLED_KIND_RE.findall(bare)


def _layout_rules(layout: Mapping[str, Any]) -> Iterator[tuple[str, Mapping[str, Any]]]:
    for name, rule in sorted((layout.get("header") or {}).items()):
        yield f"header.{name}", rule
    for set_name, record_set in sorted((layout.get("records") or {}).items()):
        for part, rule in sorted((record_set.get("key") or {}).items()):
            yield f"records.{set_name}.key.{part}", rule
        for field_name, rule in sorted((record_set.get("fields") or {}).items()):
            yield f"records.{set_name}.fields.{field_name}", rule


@dataclass
class _Reach:
    contracts: dict[tuple[str, int], CarriedContract]
    kinds: dict[str, str]
    """Every kind named, with where it was first named."""
    problems: list[str]


def _reach(
    operations: Sequence[ProviderOperation],
    contracts: Mapping[tuple[str, int], CarriedContract],
    kinds: Mapping[str, CarriedCaptureKind],
) -> _Reach:
    """Walk from the operations through the given Contracts and kinds, recording the gaps."""

    found = _Reach(contracts={}, kinds={}, problems=[])
    pending: deque[tuple[ContractRef, str, bool]] = deque()

    def name_kind(name: str, where: str) -> None:
        if name in found.kinds:
            return
        found.kinds[name] = where
        carried = kinds.get(name)
        if carried is None:
            return
        for entry, item in carried.named():
            owner = f"CaptureKind `{name}` {entry}"
            if isinstance(item, str):
                name_kind(item, owner)
            else:
                pending.append((item, owner, False))

    for operation in operations:
        owner = f"operation `{operation.name}`"
        pending.extend((ref, owner, False) for ref in operation.contract_refs())
    while pending:
        ref, owner, nested = pending.popleft()
        key = (ref.name, ref.version)
        if nested and ref.digest is None:
            found.problems.append(
                f"{owner} names {ref.spelling} without its digest; a carried copy pins every "
                "nested Contract by digest"
            )
        carried = contracts.get(key)
        if carried is None:
            found.problems.append(f"{owner} names {ref.spelling}, which declares does not carry")
            continue
        if key in found.contracts:
            continue
        found.contracts[key] = carried
        for field_name, item in carried.named():
            where = f"carried Contract {carried.ref.spelling}.{field_name}"
            if isinstance(item, str):
                name_kind(item, where)
            else:
                pending.append((item, where, True))
    for name, where in sorted(found.kinds.items()):
        if name not in kinds:
            found.problems.append(
                f"{where} is capture<{name}>; declares carries no copy of CaptureKind `{name}` "
                "(name it capture<*> to promise no layout)"
            )
    return found


def declares_problems(
    operations: Sequence[ProviderOperation], declares: ProviderDeclares
) -> list[str]:
    """What keeps ``declares`` from being exactly the closure the operations reach.

    Every ``in``, ``out`` and refusal Contract is carried; every carried
    Contract pins its nested Contracts by digest and they are carried; every
    ``capture<K>`` reached is a carried kind, whose layout rows and rule types
    are reached in turn; and nothing is carried that nothing reaches. Digests,
    types and layouts themselves are core's to judge.
    """

    found = _reach(
        operations,
        {(item.name, item.version): item for item in declares.contracts},
        {item.name: item for item in declares.capture_kinds},
    )
    problems = list(dict.fromkeys(found.problems))
    for kind in declares.capture_kinds:
        if kind.name not in found.kinds:
            problems.append(f"declares carries CaptureKind `{kind.name}`, which no operation names")
    for contract in declares.contracts:
        if (contract.name, contract.version) not in found.contracts:
            problems.append(
                f"declares carries Contract {contract.ref.spelling}, which no operation, and no "
                "layout of a kind they name, reaches"
            )
    return problems


def check_declares(operations: Sequence[ProviderOperation], declares: ProviderDeclares) -> None:
    """``ValueError`` naming every problem :func:`declares_problems` finds."""

    problems = declares_problems(operations, declares)
    if problems:
        raise ValueError("registration declares: " + "; ".join(problems))


def declared_closure(
    operations: Sequence[ProviderOperation],
    contracts: Iterable[CarriedContract | Mapping[str, Any]],
    capture_kinds: Iterable[CarriedCaptureKind | Mapping[str, Any]] = (),
    built_against: Iterable[
        BuiltAgainstCapability | BuiltAgainstCaptureKind | Mapping[str, Any]
    ] = (),
) -> ProviderDeclares:
    """The ``declares`` block for ``operations``: exactly what they reach out of the pool.

    ``contracts`` and ``capture_kinds`` are every shape the package has (more
    is fine; only what the operations reach is carried). A reached Contract or
    kind the pool lacks, a nested pin without its digest, or two different
    copies of one Contract version or one kind, is a ``ValueError``.
    """

    pool: dict[tuple[str, int], CarriedContract] = {}
    for raw in contracts:
        contract = raw if isinstance(raw, CarriedContract) else CarriedContract.model_validate(raw)
        key = (contract.name, contract.version)
        if key in pool and pool[key] != contract:
            raise ValueError(f"two different copies of Contract {contract.ref.spelling}")
        pool[key] = contract
    kinds: dict[str, CarriedCaptureKind] = {}
    for raw_kind in capture_kinds:
        kind = (
            raw_kind
            if isinstance(raw_kind, CarriedCaptureKind)
            else CarriedCaptureKind.model_validate(raw_kind)
        )
        if kind.name in kinds and kinds[kind.name] != kind:
            raise ValueError(f"two different copies of CaptureKind {kind.name!r}")
        kinds[kind.name] = kind
    found = _reach(operations, pool, kinds)
    if found.problems:
        raise ValueError("cannot carry the closure: " + "; ".join(dict.fromkeys(found.problems)))
    records = {
        item
        if isinstance(item, BuiltAgainstCapability | BuiltAgainstCaptureKind)
        else _BUILT_AGAINST.validate_python(item)
        for item in built_against
    }
    return ProviderDeclares(
        contracts=tuple(sorted(found.contracts.values(), key=_contract_key)),
        capture_kinds=tuple(
            sorted((kinds[name] for name in found.kinds), key=lambda item: _utf8(item.name))
        ),
        built_against=tuple(sorted(records, key=_built_against_key)),
    )


# -- reading -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class RegistrationBundle:
    """Verified, self-contained data ready for host-side proposal lowering."""

    root: Path
    descriptor: PackageRegistration
    manifest: ProviderManifest

    @property
    def operations(self) -> tuple[ProviderOperation, ...]:
        return self.descriptor.operations

    @property
    def declares(self) -> ProviderDeclares:
        return self.descriptor.declares

    def operation(self, name: str) -> ProviderOperation:
        for item in self.descriptor.operations:
            if item.name == name:
                return item
        raise KeyError(name)

    def contract(self, ref: ContractRef) -> CarriedContract:
        """The carried copy of one Contract the operations reach."""

        found = self.descriptor.declares.contract(ref)
        if found is None:
            raise KeyError(ref.spelling)
        return found

    def entry_points(self) -> dict[str, str]:
        """The ``cruxible.providers`` entry points the distribution must declare.

        One per operation, named by the operation, valued by its entrypoint.
        """

        return {item.name: item.entrypoint for item in self.descriptor.operations}

    def export_document(self) -> dict[str, Any]:
        """Return verified data for host lowering, without paths or imported code.

        This document describes the package; it is not an accepted artifact.
        Installation must independently bind it to the exact distribution it
        inspected; the operations' Contracts resolve inside ``declares``.
        """

        return {
            "schema_version": REGISTRATION_SCHEMA_VERSION,
            "manifest": self.manifest.model_dump(mode="json"),
            "operations": [item.wire() for item in self.descriptor.operations],
            "declares": self.descriptor.declares.model_dump(mode="json"),
            "runtime_requirements": [
                item.model_dump(mode="json") for item in self.descriptor.runtime_requirements
            ],
        }


def _document(raw: bytes) -> dict[str, Any]:
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("registration.json is not a JSON object") from exc
    if not isinstance(document, dict):
        raise ValueError("registration.json is not a JSON object")
    if document.get("schema_version") != REGISTRATION_SCHEMA_VERSION:
        raise ValueError(
            f"registration.json schema {document.get('schema_version')!r} is not read; "
            "schema 3 declares the package's operations, the capabilities they serve and "
            "the shapes they carry"
        )
    return document


def load_registration(root: Path) -> RegistrationBundle:
    """Verify the descriptor, its manifest and their cross-links; execute nothing.

    ``declares`` is exactly the closure the operations reach
    (:func:`declares_problems`). Every operation's entrypoint is one manifest
    implementation's, and every manifest implementation serves at least one
    operation. A runtime requirement belongs to a declared implementation, and
    a Python extra it names is one that implementation requires.
    """

    descriptor = PackageRegistration.model_validate(
        _document((root / REGISTRATION_FILE).read_bytes())
    )
    check_declares(descriptor.operations, descriptor.declares)
    descriptor.manifest.read(root)
    manifest = load_manifest(root / descriptor.manifest.path)
    implementations = {item.entrypoint: item for item in manifest.implementations}
    served: set[str] = set()
    for operation in descriptor.operations:
        if operation.entrypoint not in implementations:
            raise ValueError(
                f"operation {operation.name!r} names entrypoint {operation.entrypoint!r}, "
                "which the manifest does not declare"
            )
        served.add(operation.entrypoint)
    if served != set(implementations):
        raise ValueError(
            "every manifest implementation serves at least one operation; unserved: "
            f"{sorted(set(implementations) - served)}"
        )
    for requirement in descriptor.runtime_requirements:
        implementation = implementations.get(requirement.entrypoint)
        if implementation is None:
            raise ValueError("runtime requirement names an undeclared implementation")
        if (
            requirement.kind == "python_extra"
            and requirement.name not in implementation.requires_extras
        ):
            raise ValueError("runtime requirement names an undeclared extra")
    return RegistrationBundle(root, descriptor, manifest)


def registration_from_distribution(distribution: Distribution) -> RegistrationBundle:
    """Locate the descriptor using installed wheel metadata, without imports."""

    files = distribution.files or ()
    matches = [path for path in files if path.name == REGISTRATION_FILE]
    if len(matches) != 1:
        raise ValueError("provider distribution must contain exactly one registration.json")
    bundle = load_registration(Path(str(distribution.locate_file(matches[0]))).parent)
    name = distribution.metadata["Name"]
    if canonicalize_name(name) != canonicalize_name(bundle.manifest.distribution.name):
        raise ValueError("distribution name differs from manifest")
    if distribution.version != bundle.manifest.distribution.version:
        raise ValueError("distribution version differs from manifest")
    entries = [ep for ep in distribution.entry_points if ep.group == ENTRYPOINT_GROUP]
    actual = {ep.name: ep.value for ep in entries}
    if len(actual) != len(entries) or actual != bundle.entry_points():
        raise ValueError("distribution entry points differ from the registered operations")
    extras = set(distribution.metadata.get_all("Provides-Extra") or ())
    for implementation in bundle.manifest.implementations:
        if not set(implementation.requires_extras) <= extras:
            raise ValueError("manifest requires an extra absent from distribution")
    return bundle


# -- writing -----------------------------------------------------------------------------------


def _spelled(ref: dict[str, Any]) -> dict[str, Any] | str:
    return ref if "digest" in ref else f"{ref['name']}@{ref['version']}"


def render_registration(registration: PackageRegistration) -> bytes:
    """The exact bytes a package ships: two-space JSON in model order, one final newline.

    An operation's unpinned Contracts are spelled ``name@N``, as authors write them.
    """

    document = registration.model_dump(mode="json", by_alias=True)
    for operation in document["operations"]:
        operation["in"] = _spelled(operation["in"])
        operation["out"] = _spelled(operation["out"])
        for refusal in operation.get("refusals", {}).values():
            refusal["contract"] = _spelled(refusal["contract"])
    return (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def write_registration(
    root: Path,
    *,
    contracts: Iterable[CarriedContract | Mapping[str, Any]],
    capture_kinds: Iterable[CarriedCaptureKind | Mapping[str, Any]] = (),
    built_against: Iterable[
        BuiltAgainstCapability | BuiltAgainstCaptureKind | Mapping[str, Any]
    ] = (),
    check: bool = False,
) -> bool:
    """Rewrite ``root/registration.json``'s ``declares`` and manifest digest from their sources.

    The operations, the manifest path and the runtime requirements are the
    package author's and are kept as written. ``declares`` becomes exactly the
    closure the operations reach out of ``contracts`` and ``capture_kinds``
    (:func:`declared_closure`), with ``built_against`` as given; the manifest
    digest becomes the manifest file's. With ``check``, nothing is written.
    Returns whether the file on disk already held these bytes.
    """

    path = root / REGISTRATION_FILE
    current = path.read_bytes()
    document = _document(current)
    operations = validate_operations(
        ProviderOperation.model_validate(item) for item in document.get("operations", ())
    )
    manifest = ResourceRef.model_validate(document["manifest"])
    manifest_bytes = (root / manifest.path).read_bytes()
    registration = PackageRegistration(
        schema_version=3,
        manifest=manifest.model_copy(
            update={"digest": "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()}
        ),
        operations=operations,
        declares=declared_closure(operations, contracts, capture_kinds, built_against),
        runtime_requirements=tuple(
            RuntimeRequirement.model_validate(item)
            for item in document.get("runtime_requirements", ())
        ),
    )
    rendered = render_registration(registration)
    if rendered == current:
        return True
    if not check:
        path.write_bytes(rendered)
    return False
