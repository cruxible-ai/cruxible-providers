# cruxible-providers

Provider packages for Cruxible. Apache-2.0, one package per plane, each with its
own committed lock.

This repository is the third layer of a three-layer split. Nothing heavy ever
enters the core install:

| Layer | What it is | Where it lives |
|---|---|---|
| **Core** (`cruxible`) | Machinery only — slot grammar, interface registry, LineSpec closure, executor, provider protocol. Zero heavy dependencies. | the core repository |
| **Rails** | Accepted artifacts in forkable reference repositories: generic assemblies plus domain packs. Imported via change-set proposals, never baked into the install. | reference repositories |
| **Providers** | Separate uv-locked packages, fetched on bind into isolated environments. **This repository.** | here |

The program that governs this work is `rails-providers-program-v1.md`; §7 is the
RP-0 contract these packages implement.

## Layout

```
packages/cruxible-provider-runtime/   the support library every provider uses
packages/cruxible-provider-noop/      the reference provider: the smallest
                                      package that exercises every rule
packages/_template/                   copy this to start a new plane package
vocab/interfaces/                     the launch bucket vocabularies, as draft data
vocab/stub/                           the stub interface's vocabulary
ci/marker-environments.json           the environments every digest is computed for
scripts/                              digest computation and the packaging checks
docs/                                 the packaging rules and the core seam
```

## The three levels of identity

A provider carries three identifiers, and confusing them is the mistake this
design exists to prevent.

**`implementation_digest`** — backend-invariant, and *the* track-record key. It
covers the interface id, the interface digest, the entrypoint object path, and
the provider distribution's own sha256. Never a bare version, which is a claim
rather than an identity. Switching a provider from local execution to a
container does not change it and does not split earned track record.

**`materialization_digest`** — a per-backend environment pin. Locally it is the
hashed *resolution* of the package's lock for an explicit marker environment;
in cloud it is the container image digest. The resolution is hashed, never the
lock file's bytes: lock formats churn across resolver releases, and one lock
resolves differently per platform.

**`protocol_version`** — the transport envelope version. It is recorded in
receipts and binding snapshots and appears in neither preimage, because an
executor upgrade must not split track records.

## Fail closed, everywhere

Every path in this repository refuses rather than guessing: an unknown manifest
field, an undeclared interface, an unsupported protocol major, a lock that
resolves away from its pin, a cache entry whose seal does not verify, an input
that classifies into an unclaimed bucket, a budget breach, an endpoint contacted
outside the declaration. Refusals are typed and enumerated in
`cruxible_provider_runtime.errors.RefusalCode`.

## Honest boundaries

Two things this repository does **not** claim:

- The local isolated environment is a *dependency-isolation* mechanism, **not a
  security boundary**. A local provider runs with the operator's privileges.
  Third-party providers are contained only in the cloud container backend, and
  marketplace surfaces must label local execution of third-party providers
  accordingly.
- Container builds are **not** bit-reproducible. The image digest is
  authoritative; the recorded provenance is what makes it checkable.

## Getting started

```sh
uv sync                       # the root harness: a development convenience only
uv run pytest -q              # the whole conformance suite
uv run ruff check .
uv run ruff format --check .
uv run mypy packages/cruxible-provider-runtime/src
uv run python scripts/materialization_digests.py
```

No test in this repository requires a network or a container engine. If one
does, it is a bug in the test.

## Adding a plane package

Copy `packages/_template/`, strip the `.template` suffixes, replace the
placeholders, and run `uv lock` **inside the new directory**. The full checklist
is in the template's README, and the obligations your conformance suite must
discharge are enumerated in its `tests/test_conformance.py`.
