# The core-integration seam

**Nothing in this repository touches core.** The core-side registry does not
exist yet; every integration point here runs against
`cruxible_provider_runtime.registry.StubRegistry`, and this document is the
specification of what has to replace it.

The seam is deliberately narrow. Three things must come from core, and nothing
else.

---

## 1. The Provider artifact kind

Core reserves the governed artifact kind `providers/<provider-id>.yaml`. What it
must provide:

**Payload schema.** `ProviderArtifactPayload` in
`cruxible_provider_runtime.artifact` is the RP-0 spelling: `provider_id`,
`status`, the transcribed `manifest`, its `manifest_digest`, the
`distribution` pin, and one pin per declared backend kind (`local_env`,
`container`). The laws are firm; the serialised spelling stays experimental
until this batch's files are reviewed.

**Acceptance as an ordinary change-set proposal.** Registration is not a special
path. A provider becomes bindable when its artifact is accepted, and until then
`accepted_provider()` refuses with `unaccepted_provider`.

**Registration-time checks.** Core must run, at acceptance, the checks
`StubRegistry.register_provider` runs here:

- the payload's transcribed manifest re-digests to the recorded
  `manifest_digest`;
- the declared distribution matches the manifest's;
- every declared backend kind has a pin;
- every claimed input bucket parses against the interface's registered
  vocabulary;
- every claimed bucket has a conformance fixture, and no fixture references an
  unclaimed bucket.

**What core must NOT do.** Core must never treat the package-side manifest as
authority, and must never resolve an interface without checking its digest. Both
are the difference between a governed artifact and a configuration file.

---

## 2. Interface and bucket registration

Core owns the interface registry. What it must provide:

**Interface identity.** `(interface_id, interface_digest)`, where the digest
covers the slot's input/output/refusal schema. A lookup at a digest core does
not hold refuses (`interface_digest_mismatch`); it must never fall back to "the
current one".

**Bucket vocabularies.** The format is `BucketVocabulary` in
`cruxible_provider_runtime.buckets`, published as JSON Schema at
`vocab/bucket-vocabulary.schema.json`. The launch vocabularies ship in this
repository as **draft data** under `vocab/interfaces/`, one file per interface,
for core to register. They are marked `status: draft` and stay that way until
core accepts them; accepting them is core's act, not this repository's.

**A registered classifier per interface.** Buckets are *measured, not claimed*:
the bucket recorded on a run is derived by the interface's registered classifier
from the actual input, never read from a manifest. An input the classifier
cannot place refuses (`unclassified_input`); an input placing into a bucket the
implementation does not claim refuses at admission (`unclaimed_bucket`).

Bucket ids and selectors use the spelling this repository defines:
`dimension=class;dimension=class` in the vocabulary's declared dimension order,
with `*` permitted in a selector.

---

## 3. Executor invocation of the runtime library

The core executor is what actually calls this library. The sequence:

```python
binding = bind(registry, BindRequest(...), local_backend=..., container_backend=...)
outcome = invoke(binding, registry=registry, payload=..., budgets=..., secrets=...)
```

**What core supplies.**

| Input | Notes |
|---|---|
| `IndexConfig` | Pinned index URLs from configuration, and the air-gapped flag. Nothing else may name an index. |
| A `Transport` | This library ships no HTTP client. The executor supplies one that does not follow redirects. |
| An `EnvironmentBuilder` | `UvSyncBuilder` is the production spelling; the seam is injectable so that conformance suites need no network. |
| A `ContainerDriver` | The container-engine seam, for the cloud backend. |
| Resolved secret material | Core resolves secret-refs; this library delivers the material over an inherited descriptor. |
| `Budgets` | Wall clock and output size. Cost enforcement belongs to the metering substrate. |

**What core records.** `Binding.snapshot()` produces the binding-snapshot fields
a LineDeployment stores; `InvocationOutcome.receipt_fields()` produces the
fields a receipt records. Both carry all three identity levels. A backend switch
is a deployment revision — never a LineSpec successor and never an epoch change.

**What core must enforce that this library cannot.**

- **Structural egress.** The cloud backend needs default-deny plus an allowlist
  read from the accepted artifact. Local enforcement here is best-effort and is
  explicitly not a containment guarantee; what both backends do guarantee is
  *recording* what was contacted, and refusing on a contacted endpoint outside
  the declaration.
- **Capture and grade.** Provider output enters a Capture under the declared
  CaptureContract with a contract-governed grade. A provider can never emit
  `observed`, and provider success is never world-state truth. This library
  hands core a typed result; it does not create Captures.
- **Track records.** Keyed on `implementation_digest`, per interface and input
  bucket. Rendering may slice by materialization digest but must never key on
  it.
- **Cost budgets and metering.**

---

## The legacy path

Core's existing in-process `provider/` machinery (`provider_ref`, entrypoint
digests, `ProviderRuntime`) is the donor for this design, not a peer of it. v3
Lines bind only through the fetch-on-bind path; the legacy path follows
deprecate-then-remove on the core lane's schedule. Its `deterministic` and
`side_effects` flags survive, unchanged in meaning, in the new manifest.
