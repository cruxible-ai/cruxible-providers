# The core-integration seam

Core owns governance: the Provider build artifact, the Contracts an operation
names, Procedure slots, and the Line that binds each slot to one exact
`{build, operation}` when it is accepted. This repository owns the package side
— the manifest, the registration descriptor, the provider runtime protocol and
the child harness — and exports package-owned registration data through
`cruxible_provider_runtime.registration`. It never installs into an instance or
writes core state. `StubRegistry` is the standalone conformance harness, not the
production authority.

Provider runtime protocol **2.0** is the only protocol. There is no interface,
no input bucket, no classifier and no shared decline vocabulary.

---

## 1. What a package declares

**`registration.json`, schema 2.** The manifest by package-relative path and
digest, and the package's typed operations:

```json
{"name": "web.fetch", "in": "web.fetch.request@1", "out": "web.fetch.result@1",
 "effect": "external_read",
 "refusals": {"provider_declined": {"contract": "web.fetch.refusal.provider_declined@1",
                                    "absorbable": true}},
 "entrypoint": "cruxible_provider_web.fetch:WebFetch"}
```

The shape is core's `ProviderOperation` exactly (`cruxible_provider_runtime
.operations.ProviderOperation` mirrors it). Contracts are named `name@N`; the
install pins each to the version accepted at its base. Runtime requirements
are keyed by the `entrypoint` of the manifest row they belong to.

**`manifest.yaml`, schema 2.** One row per entrypoint: backends, declared
endpoints, required extras, `deterministic`. `supported_protocol_majors: [2]`.
What an entrypoint serves is the registration's business; its effect is the
operation's.

**Entry points.** The `cruxible.providers` group holds one entry per operation,
named by the operation, valued by its entrypoint.

## 2. The run context and the envelope

A run context names **one operation of one build**:

| Field | Meaning |
|---|---|
| `protocol_version` | `"2.0"`; any other major refuses `unsupported_protocol` before anything else is read |
| `operation` | the bound operation's name |
| `operation_signature_digest` | core's `operation_signature_digest({in, out, effect})` over the pinned Contracts |
| `build_digest` | the accepted Provider build the Line bound |
| `implementation_digest` | `cruxible.provider.implementation.v2` over `{entrypoint, distribution_sha256}` |
| `entrypoint` | the object the child dispatches to; it must declare `operation` in its `operations` |
| `coordinates`, `input`, `capture_contract`, `budgets`, `declared_endpoints`, `secret_channel`, `additive` | unchanged from 1.x |

The child refuses `undeclared_operation` for an operation the entrypoint does
not declare, and a protocol violation for an entrypoint that declares none or an
`--entrypoint` argument that disagrees with the context.

A refused envelope's `refusal.code` is one of two disjoint vocabularies: the
runtime's own closed `RefusalCode` (reserved; no operation may declare one), or a
code the bound operation declares. An executor holding the operation refuses an
undeclared operation code (`undeclared_refusal` here; core validates the detail
against the declared Contract).

**The contract fixture.** `scripts/export_runtime_contract.py` writes
`packages/cruxible-provider-runtime/tests/fixtures/provider_runtime_contract_v2.json`
from this runtime's own models: wire schemas, the reserved codes, valid and
invalid vectors (a protocol-1.x context among them), receipt fields, and the
implementation, materialization and operation-signature digest goldens. Core
vendors that file, records the commit it took it from, and holds its mirror to
it.

## 3. What core supplies to the stub-registry path

```python
build = registry.register_provider(payload, load_registration(package_root).operations)
binding = bind(registry, BindRequest(provider_id=..., operation=..., ...), ...)
outcome = invoke(binding, registry=registry, payload=..., budgets=..., secrets=...)
```

| Input | Notes |
|---|---|
| `IndexConfig` | Pinned index URLs from configuration, and the air-gapped flag. Nothing else may name an index. |
| A `Transport` | This library ships no HTTP client. The executor supplies one that does not follow redirects. |
| An `EnvironmentBuilder` | `UvSyncBuilder` is the production spelling; the seam is injectable so that conformance suites need no network. |
| A `ContainerDriver` | The container-engine seam, for the cloud backend. |
| Resolved secret material | Core resolves secret-refs; this library delivers the material over an inherited descriptor. |
| Contract digests | Core pins every Contract an operation names; the stub pins with a stand-in digest. |
| `Budgets` | Wall clock and output size. Cost enforcement belongs to the metering substrate. |

`Binding.snapshot()` and `InvocationOutcome.receipt_fields()` carry the build
digest, the operation and its signature digest beside the implementation and
materialization digests.

## 4. What core must enforce that this library cannot

- **Contracts.** The runtime never interprets an operation's Contracts. Core
  validates input before the run, output after it, and a refusal's detail
  against its declared Contract.
- **Structural egress.** The cloud backend needs default-deny plus an allowlist
  read from the accepted artifact. *Recording conformance* (declared equals
  observed) holds in both backends and is what this repository's conformance
  lane tests; *containment* exists in the cloud backend only.
- **Cost budgets.** `Budgets.cost_units` travels with every run; core's metering
  substrate raises through `budget.enforce_cost_budget`.
- **Capture and grade.** Provider output enters core as the operation's declared
  output Contract; a provider can never emit `observed`.
- **Dev pins.** A binding snapshot carrying `dev_sources_permitted` describes a
  pre-publication development pin and must not accrue production track record.
