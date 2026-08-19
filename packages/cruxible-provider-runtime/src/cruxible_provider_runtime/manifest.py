"""The package-side provider manifest.

The manifest ships inside the provider distribution and declares, per
implementation: slot interface + exact interface digest, entrypoint object path,
backend kinds, declared input buckets, declared external endpoints,
CaptureContract families, and the determinism/side-effect flags inherited from
the legacy in-process provider protocol.

**The package-side manifest is never authority.** It is a transcription source;
authority is the accepted ``providers/<provider-id>.yaml`` governed artifact.
At bind and invoke the runtime recomputes this manifest's digest and refuses on
any divergence from the accepted artifact.

Unknown fields fail closed: every model here forbids extras, and
:func:`load_manifest` converts the resulting validation error into a typed
``unknown_manifest_field`` refusal.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .canonical import SHA256_RE, domain_digest
from .errors import RefusalCode, refuse

__all__ = [
    "BackendKind",
    "ImplementationManifest",
    "ProviderManifest",
    "DistributionRef",
    "MANIFEST_DOMAIN_TAG",
    "ENTRYPOINT_GROUP",
    "manifest_digest",
    "load_manifest",
    "load_manifest_document",
]

MANIFEST_DOMAIN_TAG = "cruxible.provider.manifest.v1"
ENTRYPOINT_GROUP = "cruxible.providers"

BackendKind = Literal["local_env", "container"]


def _validate_digest(value: str) -> str:
    if not SHA256_RE.match(value):
        raise ValueError(f"expected a sha256:<hex> digest, got {value!r}")
    return value


class DistributionRef(BaseModel):
    """Names the distribution this manifest belongs to.

    The distribution's own sha256 is deliberately absent: a manifest shipped
    inside an artifact cannot contain that artifact's hash. The hash is carried
    by the accepted Provider artifact and is what the implementation digest
    consumes.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    version: str


class ImplementationManifest(BaseModel):
    """One (interface, entrypoint) implementation inside a provider package."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    interface_id: str
    interface_digest: str
    entrypoint: str = Field(description="module path and object, e.g. 'pkg.mod:Object'")
    backends: tuple[BackendKind, ...]
    declared_input_buckets: tuple[str, ...]
    bucket_conformance: dict[str, str] = Field(
        default_factory=dict,
        description="declared bucket selector -> conformance fixture id",
    )
    declared_endpoints: tuple[str, ...] = ()
    capture_contract_families: tuple[str, ...] = ()
    deterministic: bool
    side_effects: bool

    _validate_interface_digest = field_validator("interface_digest")(_validate_digest)

    @field_validator("entrypoint")
    @classmethod
    def _object_path(cls, value: str) -> str:
        module, sep, obj = value.partition(":")
        if not sep or not module or not obj:
            raise ValueError(f"entrypoint must be 'module:object', got {value!r}")
        return value

    @field_validator("backends")
    @classmethod
    def _backends_non_empty(cls, value: tuple[BackendKind, ...]) -> tuple[BackendKind, ...]:
        if not value:
            raise ValueError("an implementation must declare at least one backend kind")
        if len(set(value)) != len(value):
            raise ValueError(f"duplicate backend kinds: {value}")
        return value

    @field_validator("declared_input_buckets")
    @classmethod
    def _buckets_non_empty(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("an implementation must declare at least one input bucket")
        return value


class ProviderManifest(BaseModel):
    """The whole package-side manifest."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    provider_id: str
    distribution: DistributionRef
    entrypoint_group: Literal["cruxible.providers"] = "cruxible.providers"
    supported_protocol_majors: tuple[int, ...]
    implementations: tuple[ImplementationManifest, ...]

    @field_validator("supported_protocol_majors")
    @classmethod
    def _majors_non_empty(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if not value:
            raise ValueError("a manifest must declare at least one supported protocol major")
        return value

    @field_validator("implementations")
    @classmethod
    def _implementations_unique(
        cls, value: tuple[ImplementationManifest, ...]
    ) -> tuple[ImplementationManifest, ...]:
        if not value:
            raise ValueError("a manifest must declare at least one implementation")
        keys = [(impl.interface_id, impl.entrypoint) for impl in value]
        if len(set(keys)) != len(keys):
            raise ValueError(f"duplicate (interface, entrypoint) pairs: {keys}")
        return value

    def implementation(self, interface_id: str) -> ImplementationManifest:
        """Look up the implementation for ``interface_id`` or refuse."""

        matches = [impl for impl in self.implementations if impl.interface_id == interface_id]
        if not matches:
            raise refuse(
                RefusalCode.UNDECLARED_INTERFACE,
                f"provider {self.provider_id!r} declares no implementation of {interface_id!r}",
                provider_id=self.provider_id,
                declared=[impl.interface_id for impl in self.implementations],
            )
        if len(matches) > 1:
            raise refuse(
                RefusalCode.UNDECLARED_INTERFACE,
                f"provider {self.provider_id!r} declares {len(matches)} implementations "
                f"of {interface_id!r}; the accepted artifact must disambiguate by entrypoint",
                provider_id=self.provider_id,
                interface_id=interface_id,
            )
        return matches[0]

    def canonical_payload(self) -> dict[str, Any]:
        """The manifest as a plain JSON-able mapping, for digesting/transcription."""

        return json.loads(self.model_dump_json())


def manifest_digest(manifest: ProviderManifest) -> str:
    """Digest a manifest under ``cruxible.provider.manifest.v1``."""

    return domain_digest(MANIFEST_DOMAIN_TAG, manifest.canonical_payload())


def load_manifest_document(document: Any) -> ProviderManifest:
    """Validate a already-parsed manifest document, failing closed on extras."""

    try:
        return ProviderManifest.model_validate(document)
    except ValidationError as exc:
        extras = [
            ".".join(str(part) for part in error["loc"])
            for error in exc.errors()
            if error["type"] == "extra_forbidden"
        ]
        if extras:
            raise refuse(
                RefusalCode.UNKNOWN_MANIFEST_FIELD,
                f"manifest declares unknown fields: {extras}",
                fields=extras,
            ) from exc
        raise refuse(
            RefusalCode.UNKNOWN_MANIFEST_FIELD,
            "manifest failed schema validation",
            errors=json.loads(exc.json()),
        ) from exc


def load_manifest(path: Path) -> ProviderManifest:
    """Load and validate a manifest from a YAML or JSON file."""

    text = path.read_text(encoding="utf-8")
    document = yaml.safe_load(text)
    if not isinstance(document, dict):
        raise refuse(
            RefusalCode.UNKNOWN_MANIFEST_FIELD,
            f"manifest at {path} is not a mapping",
            path=str(path),
        )
    return load_manifest_document(document)
