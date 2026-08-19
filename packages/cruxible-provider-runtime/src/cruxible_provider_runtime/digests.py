"""The two provider identity digests.

Three levels of identity, one track-record key:

``implementation_digest``
    Backend-invariant, and **the** track-record key. Domain tag
    ``cruxible.provider.implementation.v1`` over interface id, interface digest,
    entrypoint object path, and the provider distribution's own sha256 — never a
    bare ``version``, which is a claim rather than an identity. A backend switch
    never changes it and never splits earned track record.

``materialization_digest``
    A per-backend environment pin. Domain tag
    ``cruxible.provider.materialization.v1`` over the sorted
    ``(name, version, artifact sha256)`` triples of the resolved set for an
    explicit marker environment, plus that marker environment. For the container
    backend the pin is the image digest verbatim.

``protocol_version``
    The transport envelope version. It is recorded in receipts and the binding
    snapshot and appears in **neither** preimage: an executor upgrade must not
    split track records.
"""

from __future__ import annotations

from typing import Any

from .canonical import SHA256_RE, domain_digest, normalize_sha256
from .resolution import MarkerEnvironment, ResolvedSet

__all__ = [
    "IMPLEMENTATION_DOMAIN_TAG",
    "MATERIALIZATION_DOMAIN_TAG",
    "container_materialization_digest",
    "implementation_digest",
    "implementation_preimage",
    "materialization_digest",
    "materialization_preimage",
]

IMPLEMENTATION_DOMAIN_TAG = "cruxible.provider.implementation.v1"
MATERIALIZATION_DOMAIN_TAG = "cruxible.provider.materialization.v1"


def implementation_preimage(
    *,
    interface_id: str,
    interface_digest: str,
    entrypoint: str,
    distribution_sha256: str,
) -> dict[str, Any]:
    """The canonical implementation preimage.

    Exactly four fields, in the order the contract names them. Nothing about the
    backend, the environment, or the protocol appears here.
    """

    if not SHA256_RE.match(interface_digest):
        raise ValueError(f"interface_digest must be sha256:<hex>, got {interface_digest!r}")
    return {
        "interface_id": interface_id,
        "interface_digest": interface_digest,
        "entrypoint": entrypoint,
        "distribution_sha256": normalize_sha256(distribution_sha256),
    }


def implementation_digest(
    *,
    interface_id: str,
    interface_digest: str,
    entrypoint: str,
    distribution_sha256: str,
) -> str:
    """Compute the backend-invariant implementation digest."""

    return domain_digest(
        IMPLEMENTATION_DOMAIN_TAG,
        implementation_preimage(
            interface_id=interface_id,
            interface_digest=interface_digest,
            entrypoint=entrypoint,
            distribution_sha256=distribution_sha256,
        ),
    )


def materialization_preimage(resolved: ResolvedSet) -> dict[str, Any]:
    """The canonical materialization preimage: the resolution and its environment."""

    return {
        "marker_environment": resolved.marker_environment.digest_payload(),
        "resolved": resolved.triples(),
    }


def materialization_digest(resolved: ResolvedSet) -> str:
    """Compute the local-backend materialization digest for a resolved set."""

    return domain_digest(MATERIALIZATION_DOMAIN_TAG, materialization_preimage(resolved))


def container_materialization_digest(image_digest: str) -> str:
    """The container backend's materialization pin: the image digest itself.

    Not re-hashed. Re-hashing would produce a value that nothing in a registry
    can be compared against, and the contract makes the image digest
    authoritative for the cloud backend.
    """

    return normalize_sha256(image_digest)


def marker_environment_key(env: MarkerEnvironment) -> str:
    """A stable key for indexing per-environment pins in an accepted artifact."""

    return env.id
