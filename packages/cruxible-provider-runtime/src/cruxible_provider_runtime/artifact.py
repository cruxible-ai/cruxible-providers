"""The governed Provider artifact payload.

A provider is bindable only through an accepted ``providers/<provider-id>.yaml``
governed artifact whose payload is a canonical transcription of the package-side
manifest. Registration is an ordinary change-set proposal; at bind and invoke the
runtime recomputes the package manifest digest and refuses on any divergence.

This module owns only the *payload* schema and its digest. Acceptance,
proposals, and storage are core's business — RP-0 ships schemas and a
conformance harness against a stub registry and never touches the core repo.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .canonical import SHA256_RE, domain_digest
from .errors import RefusalCode, refuse
from .manifest import ProviderManifest, load_manifest_document, manifest_digest

__all__ = [
    "ARTIFACT_DOMAIN_TAG",
    "ContainerBackendPin",
    "DistributionPin",
    "ImageProvenance",
    "LocalEnvBackendPin",
    "ProviderArtifactPayload",
    "artifact_digest",
    "load_provider_artifact",
]

ARTIFACT_DOMAIN_TAG = "cruxible.provider.artifact.v1"


def _digest_field(value: str) -> str:
    if not SHA256_RE.match(value):
        raise ValueError(f"expected sha256:<hex>, got {value!r}")
    return value


def _plain_filename(value: str) -> str:
    """Reject a filename that is anything other than a plain name.

    The local builder writes the fetched artifact to ``<staging>/artifact/<filename>``
    and creates the parents, so a name carrying ``..`` or a leading separator
    picks the directory rather than the file inside it — a governed artifact
    deciding where the operator's process writes bytes it also supplied. That the
    payload is accepted before it can be bound is a governance control and not a
    parser: acceptance reviews what the artifact *says*, and this is the check
    that what it says is a filename.

    ``:`` goes with the separators. A Windows drive-relative path (``C:evil.whl``)
    carries no separator at all and still names somewhere else.
    """

    if not value or value in {".", ".."} or value.startswith("."):
        raise ValueError(f"filename must be a plain name, not a relative path, got {value!r}")
    if any(character in value for character in ("/", "\\", ":", "\x00")):
        raise ValueError(
            f"filename must carry no path separator, drive letter, or NUL, got {value!r}"
        )
    return value


class DistributionPin(BaseModel):
    """The exact provider distribution artifact the implementation digest covers."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    version: str
    filename: str
    sha256: str
    index_url: str
    url: str

    _validate_sha256 = field_validator("sha256")(_digest_field)
    _validate_filename = field_validator("filename")(_plain_filename)


class ImageProvenance(BaseModel):
    """The provenance an accepted container image must carry.

    Recorded both as image labels and as a build-provenance attestation. The
    build is **not** claimed bit-reproducible; the image digest is what is
    authoritative, and these fields are what the executor checks it against.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_artifact_digest: str
    materialization_digest: str
    base_image_digest: str
    builder_identity: str

    _validate_provider = field_validator("provider_artifact_digest")(_digest_field)
    _validate_materialization = field_validator("materialization_digest")(_digest_field)
    _validate_base = field_validator("base_image_digest")(_digest_field)


class ContainerBackendPin(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    image_reference: str
    image_digest: str
    provenance: ImageProvenance

    _validate_image = field_validator("image_digest")(_digest_field)


class LocalEnvBackendPin(BaseModel):
    """The local backend's environment pins, one per supported environment.

    ``lock_sha256`` is tamper detection over the committed lock bytes and is
    explicitly *not* an identity: identity is the per-environment
    materialization digest, recomputed from the lock at bind time.

    The key is an **environment pin key** — a marker environment id plus the
    extras selected for it (``linux-cp311``, ``linux-cp311+docling``). One lock
    produces one environment per extras set, because that is what a per-engine
    extra *is*, and two implementations in one package may need different ones.
    See ``resolution.environment_pin_key``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    lock_sha256: str
    materialization_digests: dict[str, str] = Field(
        description="environment pin key (marker environment + extras) -> materialization digest",
    )

    _validate_lock = field_validator("lock_sha256")(_digest_field)

    @field_validator("materialization_digests")
    @classmethod
    def _digests(cls, value: dict[str, str]) -> dict[str, str]:
        if not value:
            raise ValueError("a local_env pin must cover at least one marker environment")
        for pin_key, digest in value.items():
            if not SHA256_RE.match(digest):
                raise ValueError(f"materialization digest for {pin_key!r} must be sha256:<hex>")
        return value


class ProviderArtifactPayload(BaseModel):
    """The payload of ``providers/<provider-id>.yaml``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    provider_id: str
    status: Literal["proposed", "accepted"] = "proposed"
    manifest: ProviderManifest
    manifest_digest: str
    distribution: DistributionPin
    local_env: LocalEnvBackendPin | None = None
    container: ContainerBackendPin | None = None

    _validate_manifest_digest = field_validator("manifest_digest")(_digest_field)

    @field_validator("provider_id")
    @classmethod
    def _provider_id(cls, value: str) -> str:
        if not value:
            raise ValueError("provider_id must be non-empty")
        return value

    def check_self_consistency(self) -> None:
        """Refuse a payload whose transcription does not match its own manifest."""

        recomputed = manifest_digest(self.manifest)
        if recomputed != self.manifest_digest:
            raise refuse(
                RefusalCode.MANIFEST_DIVERGENCE,
                "accepted artifact records a manifest digest that its own transcription "
                "does not reproduce",
                provider_id=self.provider_id,
                recorded=self.manifest_digest,
                recomputed=recomputed,
            )
        if self.manifest.provider_id != self.provider_id:
            raise refuse(
                RefusalCode.MANIFEST_DIVERGENCE,
                "accepted artifact and its transcribed manifest name different providers",
                artifact_provider_id=self.provider_id,
                manifest_provider_id=self.manifest.provider_id,
            )
        if self.distribution.name != self.manifest.distribution.name:
            raise refuse(
                RefusalCode.MANIFEST_DIVERGENCE,
                "pinned distribution name does not match the manifest",
                pinned=self.distribution.name,
                manifest=self.manifest.distribution.name,
            )
        if self.distribution.version != self.manifest.distribution.version:
            raise refuse(
                RefusalCode.MANIFEST_DIVERGENCE,
                "pinned distribution version does not match the manifest",
                pinned=self.distribution.version,
                manifest=self.manifest.distribution.version,
            )
        declared_backends = {
            backend for impl in self.manifest.implementations for backend in impl.backends
        }
        if "local_env" in declared_backends and self.local_env is None:
            raise refuse(
                RefusalCode.MANIFEST_DIVERGENCE,
                "manifest declares the local_env backend but the artifact carries no pin",
                provider_id=self.provider_id,
            )
        if "container" in declared_backends and self.container is None:
            raise refuse(
                RefusalCode.MANIFEST_DIVERGENCE,
                "manifest declares the container backend but the artifact carries no pin",
                provider_id=self.provider_id,
            )

    def canonical_payload(self) -> dict[str, Any]:
        document: dict[str, Any] = json.loads(self.model_dump_json())
        return document


def artifact_digest(payload: ProviderArtifactPayload) -> str:
    """Digest an artifact payload under ``cruxible.provider.artifact.v1``.

    Two fields are excluded from the preimage:

    ``status``
        Acceptance is a governance transition, not a change of what the artifact
        says. A proposal and its acceptance must digest identically, or the
        image built against a proposal would stop verifying the moment it was
        accepted.

    ``container.provenance.provider_artifact_digest``
        A self-reference by construction: the image records *this* digest, so
        including it would make the value depend on itself.
    """

    document = payload.canonical_payload()
    document.pop("status", None)
    container = document.get("container")
    if isinstance(container, dict):
        provenance = container.get("provenance")
        if isinstance(provenance, dict):
            provenance.pop("provider_artifact_digest", None)
    return domain_digest(ARTIFACT_DOMAIN_TAG, document)


def load_provider_artifact(path: Path) -> ProviderArtifactPayload:
    """Load ``providers/<provider-id>.yaml`` and validate it, failing closed."""

    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise refuse(
            RefusalCode.MANIFEST_DIVERGENCE,
            f"provider artifact at {path} is not a mapping",
            path=str(path),
        )
    # Route the nested manifest through the manifest loader so that an unknown
    # manifest field refuses with its own code rather than a generic one.
    manifest_document = document.get("manifest")
    if isinstance(manifest_document, dict):
        load_manifest_document(manifest_document)
    try:
        payload = ProviderArtifactPayload.model_validate(document)
    except ValidationError as exc:
        raise refuse(
            RefusalCode.MANIFEST_DIVERGENCE,
            f"provider artifact at {path} failed schema validation",
            path=str(path),
            errors=json.loads(exc.json()),
        ) from exc
    payload.check_self_consistency()
    return payload
