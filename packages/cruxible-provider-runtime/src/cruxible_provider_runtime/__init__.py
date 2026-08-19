"""Support library for Cruxible provider packages.

A provider is one Python package with its own committed lock, a package-side
manifest, and one or more entrypoints. This library is what makes such a package
bindable: it owns the manifest and protocol schemas, both identity digests, the
fetch-on-bind resolver, the materialization cache, secret delivery, budget
enforcement, egress instrumentation, and the typed refusal taxonomy that keeps
every path fail-closed.

It does **not** own governance. The Provider artifact kind, interface
registration, and bucket-vocabulary registration live in core; this package ships
schemas and a conformance harness against a stub registry. See
``docs/core-integration-seam.md``.
"""

from __future__ import annotations

from .artifact import ProviderArtifactPayload, artifact_digest, load_provider_artifact
from .binding import Binding, BindRequest, bind
from .buckets import BucketDimension, BucketSelector, BucketVocabulary
from .budget import ProcessOutcome, enforce_cost_budget, run_with_budget
from .cache import MaterializationCache
from .canonical import canonical_json, domain_digest

# dependency_closure_digest and CLOSURE_DOMAIN_TAG are deliberately NOT
# re-exported here. They belong to the packaging-scope gate, not to identity: a
# core executor importing this package should see exactly two digest functions,
# because a third one sitting beside them invites someone to pin a track record
# to it. The gate imports them from cruxible_provider_runtime.digests directly.
from .digests import (
    IMPLEMENTATION_DOMAIN_TAG,
    MATERIALIZATION_DOMAIN_TAG,
    implementation_digest,
    materialization_digest,
)
from .egress import EgressRecorder, compare_egress, enforce_egress, write_child_guard
from .errors import ProviderErrorPayload, Refusal, RefusalCode, RefusalError
from .execute import InvocationOutcome, invoke
from .index import ArtifactFetcher, IndexConfig
from .manifest import ProviderManifest, load_manifest, manifest_digest
from .protocol import PROTOCOL_VERSION, Budgets, ResultEnvelope, RunContext
from .provider_api import Provider, ProviderResult, ProviderRunContext, ProviderStatus
from .registry import InterfaceRegistration, StubRegistry
from .resolution import MarkerEnvironment, ResolvedSet, load_uv_lock, resolve

__version__ = "0.1.0"

__all__ = [
    "IMPLEMENTATION_DOMAIN_TAG",
    "MATERIALIZATION_DOMAIN_TAG",
    "PROTOCOL_VERSION",
    "ArtifactFetcher",
    "BindRequest",
    "Binding",
    "BucketDimension",
    "BucketSelector",
    "BucketVocabulary",
    "Budgets",
    "EgressRecorder",
    "IndexConfig",
    "InterfaceRegistration",
    "InvocationOutcome",
    "MarkerEnvironment",
    "MaterializationCache",
    "MaterializationRequest",
    "ProcessOutcome",
    "Provider",
    "ProviderArtifactPayload",
    "ProviderErrorPayload",
    "ProviderManifest",
    "ProviderResult",
    "ProviderRunContext",
    "ProviderStatus",
    "Refusal",
    "RefusalCode",
    "RefusalError",
    "ResolvedSet",
    "ResultEnvelope",
    "RunContext",
    "StubRegistry",
    "UvSyncBuilder",
    "__version__",
    "artifact_digest",
    "bind",
    "canonical_json",
    "compare_egress",
    "domain_digest",
    "enforce_cost_budget",
    "enforce_egress",
    "implementation_digest",
    "invoke",
    "load_manifest",
    "load_provider_artifact",
    "load_uv_lock",
    "manifest_digest",
    "materialization_digest",
    "resolve",
    "run_with_budget",
    "verify_environment",
    "write_child_guard",
]
