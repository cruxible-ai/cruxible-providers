"""The two provider identity digests.

Three levels of identity, one track-record key:

``implementation_digest``
    Backend-invariant. Domain tag ``cruxible.provider.implementation.v2`` over
    the entrypoint object path and the provider distribution's own sha256 —
    never a bare ``version``, which is a claim rather than an identity. A
    backend switch never changes it and never splits earned track record. It
    names the code that ran; which operation it ran is the run's ``operation``
    and ``operation_signature_digest``, recorded beside it, because one
    entrypoint may serve several operations.

``materialization_digest``
    A per-backend environment pin. Domain tag
    ``cruxible.provider.materialization.v1`` over the **root distribution's
    identity**, the sorted ``(name, version, artifact id)`` triples of the
    resolved set for an explicit marker environment, and that marker
    environment. For the container backend the pin is the image digest verbatim.

    The root identity is load-bearing and was missing from the first cut of this
    contract. Without it, two packages with identical dependency closures — the
    ordinary case inside one monorepo — produce the same digest, and a cache
    keyed on that digest will serve one package's sealed tree when the other is
    bound. Two *versions* of one package with unchanged dependencies collide the
    same way, which is worse: the cache would run the old code under the new
    pin. Both are fixed by pinning the root's name and distribution sha256 into
    the preimage.

``protocol_version``
    The transport envelope version. It is recorded in receipts and the binding
    snapshot and appears in **neither** preimage: an executor upgrade must not
    split track records.
"""

from __future__ import annotations

from typing import Any

from .canonical import domain_digest, normalize_sha256
from .resolution import ResolvedSet

__all__ = [
    "IMPLEMENTATION_DOMAIN_TAG",
    "MATERIALIZATION_DOMAIN_TAG",
    "container_materialization_digest",
    "implementation_digest",
    "implementation_preimage",
    "materialization_digest",
    "materialization_preimage",
]

IMPLEMENTATION_DOMAIN_TAG = "cruxible.provider.implementation.v2"
MATERIALIZATION_DOMAIN_TAG = "cruxible.provider.materialization.v1"
CLOSURE_DOMAIN_TAG = "cruxible.provider.closure.v1"


def implementation_preimage(*, entrypoint: str, distribution_sha256: str) -> dict[str, Any]:
    """The canonical implementation preimage.

    Exactly two fields. Nothing about the backend, the environment, the
    protocol, or the operations the entrypoint serves appears here.
    """

    module, separator, member = entrypoint.partition(":")
    if not separator or not module or not member:
        raise ValueError(f"entrypoint must be 'module:object', got {entrypoint!r}")
    return {
        "entrypoint": entrypoint,
        "distribution_sha256": normalize_sha256(distribution_sha256),
    }


def implementation_digest(*, entrypoint: str, distribution_sha256: str) -> str:
    """Compute the backend-invariant implementation digest."""

    return domain_digest(
        IMPLEMENTATION_DOMAIN_TAG,
        implementation_preimage(entrypoint=entrypoint, distribution_sha256=distribution_sha256),
    )


def dependency_closure_preimage(resolved: ResolvedSet) -> dict[str, Any]:
    """The resolution and its environment, without the root's artifact identity.

    This is the part of a materialization pin that a *dependency* bump moves. The
    packaging-scope gate compares it across revisions, because the root
    distribution sha256 moves on every release by construction and would drown
    out the signal the gate is looking for.
    """

    return {
        "root_name": resolved.root_name,
        "marker_environment": resolved.marker_environment.digest_payload(),
        "resolved": resolved.triples(),
    }


def dependency_closure_digest(resolved: ResolvedSet) -> str:
    """Digest the dependency closure alone. Not an environment pin."""

    return domain_digest(CLOSURE_DOMAIN_TAG, dependency_closure_preimage(resolved))


def materialization_preimage(resolved: ResolvedSet, *, distribution_sha256: str) -> dict[str, Any]:
    """The canonical materialization preimage.

    Three parts: the root distribution's identity, the resolution, and the
    marker environment the resolution was computed for.
    """

    return {
        "root": {
            "name": resolved.root_name,
            "distribution_sha256": normalize_sha256(distribution_sha256),
        },
        "marker_environment": resolved.marker_environment.digest_payload(),
        "resolved": resolved.triples(),
    }


def materialization_digest(resolved: ResolvedSet, *, distribution_sha256: str) -> str:
    """Compute the local-backend materialization digest for a resolved set.

    ``distribution_sha256`` comes from the accepted Provider artifact's
    ``DistributionPin``. It is required rather than optional: an optional root
    identity is one that call sites forget, and forgetting it is exactly the
    collision this argument exists to prevent.
    """

    return domain_digest(
        MATERIALIZATION_DOMAIN_TAG,
        materialization_preimage(resolved, distribution_sha256=distribution_sha256),
    )


def container_materialization_digest(image_digest: str) -> str:
    """The container backend's materialization pin: the image digest itself.

    Not re-hashed. Re-hashing would produce a value that nothing in a registry
    can be compared against, and the contract makes the image digest
    authoritative for the cloud backend.
    """

    return normalize_sha256(image_digest)
