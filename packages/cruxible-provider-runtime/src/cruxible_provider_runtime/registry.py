"""A stub interface/provider registry.

**This is a stub.** The real Provider artifact kind, interface registration, and
bucket-vocabulary registration live in core. RP-0 ships schemas and a
conformance harness against this stub and never touches the core repo; the seam
is documented in ``docs/core-integration-seam.md``.

The stub is faithful about the *rules* core must enforce, because those rules are
what the conformance suite tests:

* an interface is known by ``(interface_id, interface_digest)`` — a digest
  mismatch refuses rather than resolving to "the current one";
* a bucket vocabulary is registered with the interface, and the bucket recorded
  on a run is derived by the interface's registered classifier from the actual
  input;
* a claimed bucket without a passing per-bucket conformance fixture refuses at
  registration.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from .artifact import ProviderArtifactPayload
from .buckets import BucketClassifier, BucketSelector, BucketVocabulary
from .canonical import SHA256_RE
from .errors import RefusalCode, refuse

__all__ = [
    "InterfaceRegistration",
    "StubRegistry",
    "load_bucket_vocabularies",
    "load_bucket_vocabulary",
]


@dataclass(frozen=True)
class InterfaceRegistration:
    """One registered slot interface."""

    interface_id: str
    interface_digest: str
    bucket_vocabulary: BucketVocabulary
    classifier: BucketClassifier
    description: str = ""

    def __post_init__(self) -> None:
        if not SHA256_RE.match(self.interface_digest):
            raise ValueError(
                f"interface_digest must be sha256:<hex>, got {self.interface_digest!r}"
            )
        if self.bucket_vocabulary.interface_id != self.interface_id:
            raise ValueError(
                "bucket vocabulary belongs to interface "
                f"{self.bucket_vocabulary.interface_id!r}, not {self.interface_id!r}"
            )


@dataclass
class StubRegistry:
    """In-memory stand-in for the core-side registry."""

    interfaces: dict[str, InterfaceRegistration] = field(default_factory=dict)
    providers: dict[str, ProviderArtifactPayload] = field(default_factory=dict)

    # -- interfaces --------------------------------------------------------

    def register_interface(self, registration: InterfaceRegistration) -> None:
        """Register an interface, refusing a silent re-registration at a new digest.

        Overwriting would let a second registration move an interface's digest
        under bindings that already pinned the first — the registry equivalent of
        a mutable tag.
        """

        existing = self.interfaces.get(registration.interface_id)
        if existing is not None and existing.interface_digest != registration.interface_digest:
            raise refuse(
                RefusalCode.INTERFACE_DIGEST_MISMATCH,
                f"interface {registration.interface_id!r} is already registered at a "
                "different digest; a re-registration is a new interface, not an update",
                interface_id=registration.interface_id,
                registered=existing.interface_digest,
                offered=registration.interface_digest,
            )
        self.interfaces[registration.interface_id] = registration

    def interface(
        self, interface_id: str, interface_digest: str | None = None
    ) -> InterfaceRegistration:
        try:
            registration = self.interfaces[interface_id]
        except KeyError:
            raise refuse(
                RefusalCode.UNKNOWN_INTERFACE,
                f"no interface {interface_id!r} is registered",
                interface_id=interface_id,
                known=sorted(self.interfaces),
            ) from None
        if interface_digest is not None and interface_digest != registration.interface_digest:
            raise refuse(
                RefusalCode.INTERFACE_DIGEST_MISMATCH,
                f"interface {interface_id!r} is registered at a different digest",
                interface_id=interface_id,
                registered=registration.interface_digest,
                requested=interface_digest,
            )
        return registration

    # -- providers ---------------------------------------------------------

    def register_provider(self, payload: ProviderArtifactPayload) -> None:
        """Accept a Provider artifact after the registration-time checks.

        Registration is an ordinary change-set proposal in core; here it is a
        method. The checks are the point, not the mechanism.
        """

        payload.check_self_consistency()
        for implementation in payload.manifest.implementations:
            registration = self.interface(
                implementation.interface_id, implementation.interface_digest
            )
            vocabulary = registration.bucket_vocabulary
            for selector_text in implementation.declared_input_buckets:
                BucketSelector.parse(selector_text, vocabulary)
                if selector_text not in implementation.bucket_conformance:
                    raise refuse(
                        RefusalCode.BUCKET_FIXTURE_MISSING,
                        f"implementation of {implementation.interface_id!r} claims bucket "
                        f"{selector_text!r} with no conformance fixture",
                        provider_id=payload.provider_id,
                        interface_id=implementation.interface_id,
                        bucket=selector_text,
                    )
            unclaimed = set(implementation.bucket_conformance) - set(
                implementation.declared_input_buckets
            )
            if unclaimed:
                raise refuse(
                    RefusalCode.BUCKET_FIXTURE_MISSING,
                    "conformance fixtures reference buckets the implementation does not claim",
                    provider_id=payload.provider_id,
                    interface_id=implementation.interface_id,
                    buckets=sorted(unclaimed),
                )
        self.providers[payload.provider_id] = payload

    def accepted_provider(self, provider_id: str) -> ProviderArtifactPayload:
        try:
            payload = self.providers[provider_id]
        except KeyError:
            raise refuse(
                RefusalCode.UNACCEPTED_PROVIDER,
                f"provider {provider_id!r} has no accepted artifact",
                provider_id=provider_id,
                known=sorted(self.providers),
            ) from None
        if payload.status != "accepted":
            raise refuse(
                RefusalCode.UNACCEPTED_PROVIDER,
                f"provider {provider_id!r} is registered but not accepted",
                provider_id=provider_id,
                status=payload.status,
            )
        return payload

    # -- classification ----------------------------------------------------

    def classify(self, interface_id: str, payload: Mapping[str, Any]) -> str:
        """Derive the bucket of an actual input; never read it from a manifest."""

        registration = self.interface(interface_id)
        assignment = registration.classifier(payload)
        if assignment is None:
            raise refuse(
                RefusalCode.UNCLASSIFIED_INPUT,
                f"interface {interface_id!r} could not classify the input into any bucket",
                interface_id=interface_id,
            )
        return registration.bucket_vocabulary.bucket_id(assignment)

    def admit(
        self,
        interface_id: str,
        declared_input_buckets: tuple[str, ...],
        payload: Mapping[str, Any],
    ) -> str:
        """Classify and admit, refusing an input in an unclaimed bucket."""

        registration = self.interface(interface_id)
        derived = self.classify(interface_id, payload)
        for selector_text in declared_input_buckets:
            selector = BucketSelector.parse(selector_text, registration.bucket_vocabulary)
            if selector.matches(derived):
                return derived
        raise refuse(
            RefusalCode.UNCLAIMED_BUCKET,
            f"input classifies into bucket {derived!r}, which this implementation does not claim",
            interface_id=interface_id,
            bucket=derived,
            declared=list(declared_input_buckets),
        )


def load_bucket_vocabulary(path: Path) -> BucketVocabulary:
    """Load one vocabulary data file, failing closed on unknown fields."""

    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    try:
        return BucketVocabulary.model_validate(document)
    except ValidationError as exc:
        raise refuse(
            RefusalCode.INVALID_BUCKET_VOCABULARY,
            f"bucket vocabulary at {path} failed schema validation",
            path=str(path),
            errors=exc.errors(include_url=False),
        ) from exc


def load_bucket_vocabularies(directory: Path) -> dict[str, BucketVocabulary]:
    """Load every ``*.yaml`` vocabulary in ``directory``, keyed by interface id."""

    vocabularies: dict[str, BucketVocabulary] = {}
    for path in sorted(directory.glob("*.yaml")):
        vocabulary = load_bucket_vocabulary(path)
        if vocabulary.interface_id in vocabularies:
            raise refuse(
                RefusalCode.INVALID_BUCKET_VOCABULARY,
                f"two vocabulary files declare interface {vocabulary.interface_id!r}",
                path=str(path),
            )
        vocabularies[vocabulary.interface_id] = vocabulary
    return vocabularies
