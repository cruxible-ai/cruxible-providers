# The core-integration seam

Core owns governance: the Provider build artifact, capabilities, Procedure
slots, the fit of an operation to a slot, and the Line that binds each slot to
one exact `{build, operation}` when it is accepted. This repository owns the package side
— the manifest, the registration descriptor, the provider runtime protocol and
the child harness — and exports package-owned registration data through
`cruxible_provider_runtime.registration`. It never installs into an instance or
writes core state. `StubRegistry` is the standalone conformance harness, not the
production authority.

Provider runtime protocol **2.0** is the only protocol. There is no interface,
no input bucket, no classifier and no shared decline vocabulary.

---

## 1. What a package declares

**`registration.json`, schema 3.** The manifest by package-relative path and
digest, the package's operations, and the shapes they carry:

```json
{"name": "web.fetch",
 "description": "Retrieves one resource with one HTTP GET ...",
 "source": {"path": "packages/cruxible-provider-web/src/cruxible_provider_web/fetch.py",
            "repository": "https://github.com/cruxible-ai/cruxible-providers"},
 "serves": ["web.fetch"],
 "in": "web.fetch.request@1", "out": "web.fetch.result@1",
 "effect": "external_read",
 "refusals": {"provider_declined": {"contract": "web.fetch.refusal.provider_declined@1",
                                    "absorbable": true}},
 "entrypoint": "cruxible_provider_web.fetch:WebFetch"}
```

The shape is core's `DeclaredOperation` and `PackageRegistration`
(`cruxible_client.contracts.provider_registration`) exactly;
`cruxible_provider_runtime.operations.ProviderOperation` and
`cruxible_provider_runtime.registration.PackageRegistration` mirror them, and
`tests/test_package_registration.py` holds every schema-3 registration here to
core's model and registration law. `serves` names capabilities (no versions,
never `workspace.`, which only core's builtin serves); core binds a slot to an
operation that serves its capability and fits it. Contracts are named `name@N`
and resolve inside `declares`, which carries a copy of every Contract and
CaptureKind the operations reach and nothing else (the install needs no kit
and installs none of them as instance state), plus `built_against`, the
capability and kind versions the copies came from. `enumerates` is gone.
Runtime requirements are keyed by the `entrypoint` of the manifest row they
belong to.

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

The result envelope carries `status` (`ok`, `refused` or `error`) and exactly
one of `output`, `refusal` or `error`, plus the trace. An `ok` envelope may also
name `incomplete_captures`: out-Contract `list<capture<...>>` fields its output
is missing members of (a page that failed to fetch, say). The field is generic,
so core reads no operation-specific field to learn it: it treats the call as
incomplete, and absence over it never retires. It is omitted when empty, and no
other status may carry it. A provider sets it through
`ProviderResult.ok(..., incomplete_captures=[...])`.

A refused envelope's `refusal.code` is one of two disjoint vocabularies: the
runtime's own closed `RefusalCode` (reserved; no operation may declare one), or a
code the bound operation declares. Runtime codes are produced by runtime
machinery only: provider code that raises or returns one — at import,
construction, declaration access or the call, or from inside the result it
returns — is reported by the child as `provider_protocol_violation` naming the
stage, and conditions only a provider can see
(a missing engine, a model file that misses its pin, a credential the run named
but did not deliver) are refusals its operation declares. The child harness
produces only the runtime codes in `HARNESS_REFUSAL_CODES` (exported in the
contract fixture as `harness_refusal_codes`); an envelope carrying any other
runtime code names a condition only the executor observes and is refused as a
protocol violation. An executor holding the operation refuses an undeclared
operation code (`undeclared_refusal` here; core validates the detail against
the declared Contract).

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
| Contract digests | Core pins every Contract an operation names to its carried copy's digest; the stub pins with a stand-in digest. |
| `Budgets` | Wall clock and output size. Cost enforcement belongs to the metering substrate. |

`Binding.snapshot()` and `InvocationOutcome.receipt_fields()` carry the build
digest, the operation and its signature digest beside the implementation and
materialization digests.

## 4. What core must enforce that this library cannot

- **Contracts.** The runtime never interprets an operation's Contracts at run.
  Core validates input before the run, output after it, and a refusal's detail
  against the operation's own carried Contracts. The registration reader checks
  the carried closure by name and version only; core judges the copies'
  digests, types and layouts, and an operation's fit to a capability.
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
