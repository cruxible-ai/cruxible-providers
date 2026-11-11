"""Read package-owned registration data without importing provider code.

A provider package ships one ``registration.json`` (schema 2)::

    {
      "schema_version": 2,
      "manifest": {"path": "manifest.yaml", "digest": "sha256:..."},
      "operations": [
        {"name": "web.fetch", "in": "web.fetch.request@1", "out": "web.fetch.result@1",
         "effect": "external_read",
         "refusals": {"invalid_parameter": {
             "contract": "web.fetch.refusal.invalid_parameter@1", "absorbable": true}},
         "entrypoint": "cruxible_provider_web.fetch:WebFetch"}
      ],
      "runtime_requirements": []
    }

This is a proposal source, not acceptance authority or an installer. The host
pins the distribution bytes before trusting executable entry points; reading
metadata never imports them. Core pins each Contract an operation names to the
version accepted at the install base and accepts the definitions under its own
laws. Only schema 2 is read: a schema-1 registration exported interfaces,
bucket vocabularies and classifiers, and is refused as such.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from importlib.metadata import Distribution
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from packaging.utils import canonicalize_name
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .canonical import SHA256_RE
from .manifest import ENTRYPOINT_GROUP, ProviderManifest, load_manifest
from .operations import ProviderOperation, validate_operations

__all__ = [
    "REGISTRATION_FILE",
    "PackageRegistration",
    "RegistrationBundle",
    "ResourceRef",
    "RuntimeRequirement",
    "callable_path",
    "load_registration",
    "registration_from_distribution",
]

REGISTRATION_FILE = "registration.json"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def callable_path(value: str) -> str:
    """``module:callable`` with identifier parts only, or ``ValueError``."""

    module, sep, member = value.partition(":")
    if not sep or not member.isidentifier() or not all(p.isidentifier() for p in module.split(".")):
        raise ValueError("entrypoint must be module:callable")
    return value


class ResourceRef(_Strict):
    path: str
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
        ):
            raise ValueError("resource path must be a normalized package-relative path")
        return value

    @field_validator("digest")
    @classmethod
    def sha256(cls, value: str) -> str:
        if not SHA256_RE.fullmatch(value):
            raise ValueError("resource digest must be sha256:<64 lowercase hex>")
        return value

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
    name: str
    description: str
    probe_entrypoint: str | None = None

    @model_validator(mode="after")
    def probe(self) -> RuntimeRequirement:
        if self.kind == "runtime_resource":
            if self.probe_entrypoint is None:
                raise ValueError("runtime resources require a package readiness probe")
            callable_path(self.probe_entrypoint)
        elif self.probe_entrypoint is not None:
            raise ValueError("Python extras do not use resource probes")
        return self


class PackageRegistration(_Strict):
    """``registration.json``, schema 2."""

    schema_version: Literal[2]
    manifest: ResourceRef
    operations: tuple[ProviderOperation, ...] = Field(min_length=1)
    runtime_requirements: tuple[RuntimeRequirement, ...] = ()

    @field_validator("operations")
    @classmethod
    def _sorted_unique(cls, value: tuple[ProviderOperation, ...]) -> tuple[ProviderOperation, ...]:
        return validate_operations(value)


@dataclass(frozen=True)
class RegistrationBundle:
    """Verified, self-contained data ready for host-side proposal lowering."""

    root: Path
    descriptor: PackageRegistration
    manifest: ProviderManifest

    @property
    def operations(self) -> tuple[ProviderOperation, ...]:
        return self.descriptor.operations

    def operation(self, name: str) -> ProviderOperation:
        for item in self.descriptor.operations:
            if item.name == name:
                return item
        raise KeyError(name)

    def entry_points(self) -> dict[str, str]:
        """The ``cruxible.providers`` entry points the distribution must declare.

        One per operation, named by the operation, valued by its entrypoint.
        """

        return {item.name: item.entrypoint for item in self.descriptor.operations}

    def export_document(self) -> dict[str, Any]:
        """Return verified data for host lowering, without paths or imported code.

        This document describes the package; it is not an accepted artifact.
        Installation must independently bind it to the exact distribution it
        inspected, and pin every Contract it names.
        """

        return {
            "schema_version": 2,
            "manifest": self.manifest.model_dump(mode="json"),
            "operations": [item.wire() for item in self.descriptor.operations],
            "runtime_requirements": [
                item.model_dump(mode="json") for item in self.descriptor.runtime_requirements
            ],
        }


def _descriptor(raw: bytes) -> PackageRegistration:
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("registration.json is not a JSON object") from exc
    if not isinstance(document, dict):
        raise ValueError("registration.json is not a JSON object")
    if document.get("schema_version") != 2:
        raise ValueError(
            f"registration.json schema {document.get('schema_version')!r} is not read; "
            "schema 2 declares the package's typed operations (schema 1 exported "
            "interfaces, vocabularies and classifiers, which no longer exist)"
        )
    return PackageRegistration.model_validate(document)


def load_registration(root: Path) -> RegistrationBundle:
    """Verify the descriptor, its manifest and their cross-links; execute nothing.

    Every operation's entrypoint is one manifest implementation's, and every
    manifest implementation serves at least one operation. A runtime
    requirement belongs to a declared implementation, and a Python extra it
    names is one that implementation requires.
    """

    descriptor = _descriptor((root / REGISTRATION_FILE).read_bytes())
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
