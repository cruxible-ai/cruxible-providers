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
packages/cruxible-provider-web/       the web plane: web.fetch, search.web
packages/cruxible-provider-docs/       the document plane: doc.to_markdown, ocr.extract
packages/cruxible-providers/          the umbrella meta-package: zero code, plane extras
packages/cruxible-provider-quant/     the quantitative plane: classical
                                      baselines on the seven quant interfaces
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
root distribution's identity plus the hashed *resolution* of the package's lock
for an explicit marker environment; in cloud it is the container image digest.
The resolution is hashed, never the lock file's bytes: lock formats churn across
resolver releases, and one lock resolves differently per platform. The root
identity is in the preimage because without it two packages with identical
dependency closures — the ordinary case inside one monorepo — collide, and the
cache is keyed on nothing else.

**`protocol_version`** — the transport envelope version. It is recorded in
receipts and binding snapshots and appears in neither preimage, because an
executor upgrade must not split track records.

## Heavy engines live behind per-engine extras

A browser, a document-conversion stack, an OCR runtime: each is gigabytes, and
none of them belongs in an install somebody does by accident. So a plane
package's **base** distribution carries the adapter logic, the schemas, the
bucket classifiers, and the recorded fixtures, while each engine sits behind an
extra that the implementation needing it declares in its manifest
(`requires_extras`).

That declaration is not documentation: it is a **resolution input**. Selecting an
extra changes the resolved set, which changes the materialization digest, which
means one lock produces one environment per extras set. An accepted artifact
pins each separately, under an environment pin key that names both:

```
linux-cp311                 the base environment
linux-cp311+browser         the same lock, resolved with a browser in it
linux-cp311+docling
```

Two implementations of two interfaces in one package may therefore bind two
different environments — `doc.to_markdown` binds one with a conversion engine,
`ocr.extract` one with an OCR runtime — and an artifact that pins only one of
them refuses the other rather than falling back.

**Remaining limitation: one engine closure needs a newer floor than the declared
environments have.** A declared tag list is read as an ordering (PEP 425/600)
rather than as literal names, so `+browser` and `+paddleocr` pin on all three
launch environments in `ci/marker-environments.json`. `+docling` still refuses
with `no_compatible_artifact`, naming `torchvision`, and that refusal is right:
the declared environments target glibc 2.17 and macOS 11.0, and torchvision
publishes `manylinux_2_28` and `macosx_14_0` wheels only. Making that closure
pinnable means raising the declared floors, which re-pins every package at once.
`docs/packaging.md` carries the table.

The consequence for testing is the point of the whole arrangement: `uv run
pytest` installs no engine and downloads nothing. Real-engine tests exist, are
marked `engine`, are excluded from the default run by construction, and have
their own opt-in CI lane. What they are *for* is keeping the recorded engine
responses honest: they run the real engine over the same shipped bytes and assert
it still produces what the recording claims.

## Fail closed, everywhere

Every path in this repository refuses rather than guessing: an unknown manifest
field, an undeclared interface, an unsupported protocol major, a lock that
resolves away from its pin, a cache entry whose seal does not verify, an input
that classifies into an unclaimed bucket, a budget breach, an endpoint contacted
outside the declaration. Refusals are typed and enumerated in
`cruxible_provider_runtime.errors.RefusalCode`.

## Honest boundaries

Things this repository does **not** claim:

- The local isolated environment is a *dependency-isolation* mechanism, **not a
  security boundary**. A local provider runs with the operator's privileges.
  Third-party providers are contained only in the cloud container backend, and
  marketplace surfaces must label local execution of third-party providers
  accordingly.
- Container builds are **not** bit-reproducible. The image digest is
  authoritative; the recorded provenance is what makes it checkable.
- The egress-conformance lane tests **recording conformance** — declared equals
  observed, in both the executor process and the provider child. It does not
  demonstrate **containment**; that exists in the cloud backend's default-deny
  network policy alone.
- `UvSyncBuilder`, the production local builder, is marked **experimental**: it
  needs a network and a `uv` on the path, so no test here executes it end to
  end. Only its argument construction and its post-build verification are
  covered. What is not left to trust is the result — a materialized tree is
  checked against its resolution before the cache will seal it.

## Getting started

```sh
uv sync                       # the root harness: a development convenience only
uv run pytest -q              # the whole conformance suite, no engines
uv run pytest -q -m engine    # the opt-in lane; needs the per-engine extras
uv run ruff check .
uv run ruff format --check .
uv run mypy packages/cruxible-provider-runtime/src
uv run python scripts/dependency_closure_digests.py
```

No test in the default suite requires a network, a container engine, or a heavy
engine. If one does, it is a bug in the test. The `engine`-marked lane needs the
engines by definition, is excluded from the default run, and skips with a reason
rather than failing when an engine is absent — so even `pytest -m engine`
collects cleanly on a machine that has none.

## Adding a plane package

Copy `packages/_template/`, strip the `.template` suffixes, replace the
placeholders, and run `uv lock` **inside the new directory**. The full checklist
is in the template's README, and the obligations your conformance suite must
discharge are enumerated in its `tests/test_conformance.py`.
