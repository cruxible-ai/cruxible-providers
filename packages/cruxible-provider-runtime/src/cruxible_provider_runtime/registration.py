"""Read package-owned registration data without importing provider code.

This is a proposal source, not acceptance authority or an installer. The host
pins the distribution bytes before trusting executable entry points. Inspecting
metadata never imports them. Core remains responsible for validating operation
contracts and accepting the definitions under its versioned laws.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from importlib.metadata import Distribution
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from packaging.utils import canonicalize_name
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .buckets import BucketSelector, BucketVocabulary, parse_bucket_id
from .canonical import SHA256_RE, domain_digest
from .manifest import ProviderManifest, load_manifest
from .registry import load_bucket_vocabulary

REGISTRATION_FILE = "registration.json"
INTERFACE_DOMAIN = "cruxible.interface.stub.v1"
"""Retained digest domain; replacing its name would invalidate existing pins."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


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


def read_interface_definition(root: Path, identity: str) -> dict[str, Any]:
    """Read the same bundled definition used by package registration."""
    if not identity or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789._-" for c in identity):
        raise ValueError("invalid interface identity")
    value = json.loads((root / "contracts" / f"{identity}.json").read_bytes())
    if not isinstance(value, dict) or value.get("interface_id") != identity:
        raise ValueError("interface identity differs from definition")
    return value


class ClassifierExport(_Strict):
    identity: str
    version: int = Field(ge=1)
    entrypoint: str
    source: ResourceRef

    @field_validator("entrypoint")
    @classmethod
    def callable_path(cls, value: str) -> str:
        module, sep, member = value.partition(":")
        if (
            not sep
            or not member.isidentifier()
            or not all(p.isidentifier() for p in module.split("."))
        ):
            raise ValueError("classifier entrypoint must be module:callable")
        return value


class InterfaceHistory(_Strict):
    definition: ResourceRef
    interface_digest: str


class InterfaceExport(_Strict):
    interface_id: str
    interface_digest: str
    definition: ResourceRef
    vocabulary: ResourceRef
    classifier: ClassifierExport
    fixtures: ResourceRef
    predecessors: tuple[InterfaceHistory, ...] = ()


class RuntimeRequirement(_Strict):
    interface_id: str
    kind: Literal["python_extra", "runtime_resource"]
    name: str
    description: str
    probe_entrypoint: str | None = None

    @model_validator(mode="after")
    def probe(self) -> RuntimeRequirement:
        if self.kind == "runtime_resource":
            if self.probe_entrypoint is None:
                raise ValueError("runtime resources require a package readiness probe")
            ClassifierExport.callable_path(self.probe_entrypoint)
        elif self.probe_entrypoint is not None:
            raise ValueError("Python extras do not use resource probes")
        return self


class PackageRegistration(_Strict):
    schema_version: Literal[1] = 1
    manifest: ResourceRef
    interfaces: tuple[InterfaceExport, ...] = Field(min_length=1)
    runtime_requirements: tuple[RuntimeRequirement, ...] = ()


class ClassificationFixture(_Strict):
    fixture_id: str
    canonical_input: dict[str, Any]
    measured_bucket_id: str


@dataclass(frozen=True)
class RegistrationBundle:
    """Verified, self-contained data ready for host-side proposal lowering."""

    root: Path
    descriptor: PackageRegistration
    manifest: ProviderManifest
    definitions: Mapping[str, dict[str, Any]]
    vocabularies: Mapping[str, BucketVocabulary]
    fixtures: Mapping[str, tuple[ClassificationFixture, ...]]

    def export_document(self) -> dict[str, Any]:
        """Return verified data for host lowering, without paths or imported code.

        This document describes the package; it is not an accepted artifact or
        evidence that classifiers have executed successfully. Installation must
        independently bind it to the exact distribution it inspected.
        """
        return {
            "schema_version": 1,
            "manifest": self.manifest.model_dump(mode="json"),
            "interfaces": [
                {
                    "interface_id": item.interface_id,
                    "interface_digest": item.interface_digest,
                    "definition": self.definitions[item.interface_id],
                    "vocabulary": self.vocabularies[item.interface_id].model_dump(mode="json"),
                    "classifier_identity": item.classifier.identity,
                    "classifier_version": item.classifier.version,
                    "classifier_code": {
                        "entrypoint": item.classifier.entrypoint,
                        "source_digest": item.classifier.source.digest,
                    },
                    "fixtures": [
                        row.model_dump(mode="json") for row in self.fixtures[item.interface_id]
                    ],
                }
                for item in sorted(self.descriptor.interfaces, key=lambda row: row.interface_id)
            ],
            "runtime_requirements": [
                item.model_dump(mode="json") for item in self.descriptor.runtime_requirements
            ],
        }


def load_registration(root: Path) -> RegistrationBundle:
    """Verify all references and cross-links; execute no package entry points.

    Exact wheel hashing, dependency resolution, classifier execution, operation
    schema checks and acceptance are the installer's responsibility. No result
    from this reader claims that an environment is installed or authorized.
    """
    descriptor = PackageRegistration.model_validate_json((root / REGISTRATION_FILE).read_bytes())
    descriptor.manifest.read(root)
    manifest = load_manifest(root / descriptor.manifest.path)
    exports = {item.interface_id: item for item in descriptor.interfaces}
    if len(exports) != len(descriptor.interfaces):
        raise ValueError("duplicate interface exports")
    if set(exports) != {item.interface_id for item in manifest.implementations}:
        raise ValueError("registration must export every and only implemented interface")
    definitions: dict[str, dict[str, Any]] = {}
    vocabularies: dict[str, BucketVocabulary] = {}
    fixtures: dict[str, tuple[ClassificationFixture, ...]] = {}
    for identity, item in exports.items():
        definition = json.loads(item.definition.read(root))
        if not isinstance(definition, dict) or definition.get("interface_id") != identity:
            raise ValueError("interface identity differs from export")
        if domain_digest(INTERFACE_DOMAIN, definition) != item.interface_digest:
            raise ValueError("interface digest differs from definition")
        implementation = manifest.implementation(identity)
        if implementation.interface_digest != item.interface_digest:
            raise ValueError("manifest pins a different interface digest")
        if definition.get("effect_class") not in {"pure", "external_read", "external_mutation"}:
            raise ValueError("interface must declare its effect class")
        if implementation.side_effects != (definition["effect_class"] == "external_mutation"):
            raise ValueError("manifest and interface effects disagree")
        contracts = definition.get("contracts")
        if not isinstance(contracts, dict) or set(contracts) != {"input", "output"}:
            raise ValueError("interface must export shared input/output contracts")
        revision = definition.get("version")
        if type(revision) is not int or revision < 1:
            raise ValueError("interface version must be a positive integer")
        seen_predecessors: set[str] = set()
        for previous in item.predecessors:
            old = json.loads(previous.definition.read(root))
            if (
                not isinstance(old, dict)
                or old.get("interface_id") != identity
                or type(old.get("version")) is not int
                or not 0 < old["version"] < revision
                or domain_digest(INTERFACE_DOMAIN, old) != previous.interface_digest
                or previous.interface_digest in seen_predecessors
            ):
                raise ValueError("invalid interface predecessor")
            seen_predecessors.add(previous.interface_digest)
        item.vocabulary.read(root)
        vocabulary = load_bucket_vocabulary(root / item.vocabulary.path)
        if vocabulary.interface_id != identity:
            raise ValueError("vocabulary belongs to another interface")
        item.classifier.source.read(root)
        rows = tuple(
            ClassificationFixture.model_validate(row)
            for row in json.loads(item.fixtures.read(root))
        )
        by_id = {row.fixture_id: row for row in rows}
        if len(by_id) != len(rows):
            raise ValueError("duplicate classification fixture")
        for row in rows:
            assignment = parse_bucket_id(vocabulary, row.measured_bucket_id)
            if vocabulary.bucket_id(assignment) != row.measured_bucket_id:
                raise ValueError("fixture bucket must use canonical dimension order")
        if set(implementation.bucket_conformance) != set(implementation.declared_input_buckets):
            raise ValueError("each declared input selector must name a fixture")
        for selector, fixture_id in implementation.bucket_conformance.items():
            fixture = by_id.get(fixture_id)
            if fixture is None or not BucketSelector.parse(selector, vocabulary).matches(
                fixture.measured_bucket_id
            ):
                raise ValueError("classification fixture does not cover declared selector")
        definitions[identity] = definition
        vocabularies[identity] = vocabulary
        fixtures[identity] = rows
    for requirement in descriptor.runtime_requirements:
        implementation = manifest.implementation(requirement.interface_id)
        if (
            requirement.kind == "python_extra"
            and requirement.name not in implementation.requires_extras
        ):
            raise ValueError("runtime requirement names an undeclared extra")
    return RegistrationBundle(root, descriptor, manifest, definitions, vocabularies, fixtures)


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
    expected = {item.interface_id: item.entrypoint for item in bundle.manifest.implementations}
    entries = [ep for ep in distribution.entry_points if ep.group == "cruxible.providers"]
    actual = {ep.name: ep.value for ep in entries}
    if len(actual) != len(entries) or actual != expected:
        raise ValueError("distribution entry points differ from manifest")
    extras = set(distribution.metadata.get_all("Provides-Extra") or ())
    for implementation in bundle.manifest.implementations:
        if not set(implementation.requires_extras) <= extras:
            raise ValueError("manifest requires an extra absent from distribution")
    return bundle
