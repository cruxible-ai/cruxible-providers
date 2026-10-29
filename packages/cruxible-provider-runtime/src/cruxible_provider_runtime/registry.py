"""A stub provider registry.

**This is a stub.** The real Provider build artifact, its operations and the
Contracts they pin live in core. This repository ships schemas and a conformance
harness against this stub; the seam is documented in
``docs/core-integration-seam.md``.

The stub is faithful about the *rules* core enforces, because those rules are
what the conformance suite tests:

* a provider is bindable only through an **accepted** artifact;
* an accepted build declares its operations, each **pinned** — every Contract
  it names carries an exact digest — so its signature digest is defined;
* every operation's entrypoint is one the build's manifest declares, and every
  manifest implementation serves at least one operation;
* a build is known by digest, and the run context carries that digest.

Core's build digest is its own (it covers the governed Provider artifact). The
stub mints one over the runtime artifact digest and the pinned operations, under
its own domain, so a stub binding has a value with the same meaning: the exact
build an operation was bound from.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from .artifact import ProviderArtifactPayload, artifact_digest
from .canonical import domain_digest
from .errors import RefusalCode, refuse
from .operations import ContractRef, ProviderOperation, pin_operation, validate_operations

__all__ = [
    "STUB_BUILD_DOMAIN",
    "STUB_CONTRACT_DOMAIN",
    "AcceptedBuild",
    "StubRegistry",
    "stub_contract_digest",
]

STUB_BUILD_DOMAIN = "cruxible.provider.build.stub.v1"
STUB_CONTRACT_DOMAIN = "cruxible.contract.stub.v1"


def stub_contract_digest(ref: ContractRef) -> str:
    """A stand-in for the digest core's install base would pin ``ref`` to.

    Deterministic over the Contract's name and version, so two stub registries
    pin the same package identically. Core's real digest covers the accepted
    Contract body, which this repository never holds.
    """

    return domain_digest(STUB_CONTRACT_DOMAIN, {"name": ref.name, "version": ref.version})


@dataclass(frozen=True)
class AcceptedBuild:
    """One accepted provider build: the runtime artifact and its pinned operations."""

    payload: ProviderArtifactPayload
    operations: tuple[ProviderOperation, ...]

    @property
    def build_digest(self) -> str:
        return domain_digest(
            STUB_BUILD_DOMAIN,
            {
                "artifact_digest": artifact_digest(self.payload),
                "operations": [item.wire() for item in self.operations],
            },
        )

    def operation(self, name: str) -> ProviderOperation:
        for item in self.operations:
            if item.name == name:
                return item
        raise refuse(
            RefusalCode.UNDECLARED_OPERATION,
            f"provider {self.payload.provider_id!r} serves no operation {name!r}",
            provider_id=self.payload.provider_id,
            operation=name,
            declared=[item.name for item in self.operations],
        )


@dataclass
class StubRegistry:
    """In-memory stand-in for the core-side registry."""

    builds: dict[str, AcceptedBuild] = field(default_factory=dict)

    def register_provider(
        self,
        payload: ProviderArtifactPayload,
        operations: Iterable[ProviderOperation],
        *,
        contract_digests: Callable[[ContractRef], str] = stub_contract_digest,
    ) -> AcceptedBuild:
        """Accept a provider build after the registration-time checks.

        Registration is an ordinary change-set proposal in core; here it is a
        method. The checks are the point, not the mechanism. ``operations`` are
        the package's, as its registration declares them; each is pinned here
        through ``contract_digests``, the way an install pins them at its base.
        """

        payload.check_self_consistency()
        pinned = validate_operations(pin_operation(item, contract_digests) for item in operations)
        entrypoints = {impl.entrypoint for impl in payload.manifest.implementations}
        served = {item.entrypoint for item in pinned}
        unknown = served - entrypoints
        if unknown:
            raise refuse(
                RefusalCode.MANIFEST_DIVERGENCE,
                "operations name entrypoints the manifest does not declare",
                provider_id=payload.provider_id,
                entrypoints=sorted(unknown),
            )
        if served != entrypoints:
            raise refuse(
                RefusalCode.MANIFEST_DIVERGENCE,
                "a manifest implementation serves no operation",
                provider_id=payload.provider_id,
                unserved=sorted(entrypoints - served),
            )
        build = AcceptedBuild(payload=payload, operations=pinned)
        self.builds[payload.provider_id] = build
        return build

    def accepted_build(self, provider_id: str) -> AcceptedBuild:
        try:
            build = self.builds[provider_id]
        except KeyError:
            raise refuse(
                RefusalCode.UNACCEPTED_PROVIDER,
                f"provider {provider_id!r} has no accepted artifact",
                provider_id=provider_id,
                known=sorted(self.builds),
            ) from None
        if build.payload.status != "accepted":
            raise refuse(
                RefusalCode.UNACCEPTED_PROVIDER,
                f"provider {provider_id!r} is registered but not accepted",
                provider_id=provider_id,
                status=build.payload.status,
            )
        return build

    def accepted_provider(self, provider_id: str) -> ProviderArtifactPayload:
        return self.accepted_build(provider_id).payload
