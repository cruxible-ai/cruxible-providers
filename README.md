# cruxible-providers

This repository holds the optional add-on packages that let
[Cruxible](https://github.com/cruxible-ai/cruxible) do work outside its own core: fetch web pages, convert documents, run statistics, and
so on. Each add-on is called a **provider**: a Python package that does one
kind of computation and reports the result back to Cruxible. A provider offers
one or more **operations**, each a named task with a fixed input and output
(for example `web.fetch`: give it a URL, get back the page). The packages are
Apache-2.0. Each one has its own committed lock file, so its dependencies are
pinned to exact versions.

Supported platforms: Linux on x86-64 and macOS on Apple silicon, with Python
3.11 or newer. Windows works through WSL2 only.

| Package | What it is for | Operations |
|---|---|---|
| `cruxible-provider-web` | Fetching web pages and searching the web, then turning a fetched page into readable text | `web.fetch`, `web.fetch.pages`, `search.web`, `web.render`, `web.render.dom` |
| `cruxible-provider-docs` | **Not yet released.** Turning documents (PDF, text, CSV) into Markdown, and reading text out of page images (OCR) | `doc.to_markdown`, `ocr.extract` |
| `cruxible-provider-quant` | **Not yet released.** Standard numeric methods: totals and averages, forecasts, anomaly checks, statistical tests, ranking, record matching, checking how well past predictions held up | `calc.calibrate`, `calc.reduce`, `match.record`, `score.rank`, `stat.test`, `ts.anomaly`, `ts.forecast` |
| `cruxible-provider-workspace` | Turning a file's bytes into a structured text or binary body. Core already has this built in; the package is kept for testing | `workspace.file` |
| `cruxible-provider-noop` | A test package that does nothing useful on purpose; it checks that the provider rules hold | `noop.echo`, `noop.captures` |
| `cruxible-provider-runtime` | The shared library every provider package depends on. You install it directly only if you write a provider | none |
| `cruxible-providers` | A convenience package with no code that pulls in the web, docs and quant packages, for development | none |

`cruxible-provider-docs` and `cruxible-provider-quant` are not yet released: no
kit uses them yet and they ship no Contracts, so an instance cannot install
them. They will be released when a kit uses them. Their code and tests are here
and run in this repository.

To install a provider into a running Cruxible daemon, an operator with `admin`
permission runs, for example:

```sh
cruxible provider install cruxible-provider-web
```

The section *What a provider is, and how core admits one* below says what that
command checks, and *Installing from a checkout* says how to install a version
that is not on the package index.

## How the pieces fit

This repository is the third layer of a three-layer split. Nothing heavy ever
enters the core install:

| Layer | What it is | Where it lives |
|---|---|---|
| **Core** (`cruxible`) | Machinery only — Contracts, Procedure slots, Lines binding each slot to an exact `{build, operation}`, executor, provider protocol. Zero heavy dependencies. | the core repository |
| **Kits** | Exported definitions (vocabulary, Procedures, the Contracts they pin) plus the provider packages those Procedures need. Installed with `cruxible kit add`, never baked into the install. | kit releases |
| **Providers** | Separate uv-locked packages, installed into isolated environments by an explicit, governed operator install. **This repository.** | here |

The packages speak provider runtime protocol 2.0: one operation of one build
per run. `docs/core-integration-seam.md` describes what each side owns.

## What a provider is, and how core admits one

A few Cruxible terms are used from here on. A **Contract** is the declared shape
of an input or output. A **Procedure** is a stored, step-by-step method that
calls operations through named slots. A **Line** fixes which installed provider
build fills each slot. A **refusal** is a typed "no" an operation returns
instead of a result, with a code saying why.

A provider is pure computation behind shared Contracts. Its package declares
typed operations in `registration.json`: a name, an input and an output
Contract (`name@N`), an effect (`pure`, `external_read` or
`external_mutation`), the refusal codes it may return, and the entrypoint that
serves it. A Procedure never names a provider; it names a slot by signature, and
any installed build whose operation has exactly that signature fits the slot. A
Line binds each slot to one exact `{build, operation}` when it is accepted.

Core never fetches a provider when a Procedure binds. An operator installs one
explicitly, with `admin` permission:

```sh
cruxible provider install cruxible-provider-web          # or NAME==VERSION
```

(MCP: `cruxible_provider_install`.) By name, the wheel comes from the daemon's
configured provider repository, or else from the provider index (PyPI unless
the operator configures `provider_index_urls`). The wheel must match the hash the
index publishes, and the environment is built from the `uv.lock` the wheel
embeds. The install then proposes the Provider build as an ordinary change: it
lands at once when the approval policy requires no approval, and otherwise waits
for approval. It refuses an operation whose Contracts are not already accepted
on the instance; a kit that needs a provider accepts those Contracts first.
Installing grants no permission to run anything.

Every new instance already has `workspace.file` built in; it needs no install
(see `packages/cruxible-provider-workspace/README.md`).

### Installing from a checkout

An install by name works only for a version published on the index. The
packages on this branch require `cruxible-provider-runtime` at the version its
`pyproject.toml` states. Until those versions are published, build the wheels
and transfer them to the daemon:

```sh
uv build --wheel --out-dir dist packages/cruxible-provider-runtime
uv build --wheel --out-dir dist packages/cruxible-provider-web
cruxible provider install dist/cruxible_provider_web-*.whl \
    --lock packages/cruxible-provider-web/uv.lock \
    --dependency dist/cruxible_provider_runtime-*.whl
```

`--lock` is required with a local wheel. `--dependency` supplies the wheels the
lock names by path (here the runtime); registry dependencies resolve by hash
from the configured indexes. Like every `provider` command it talks to a running
Cruxible daemon; the CLI reads the files and transfers them, so this also works
against a remote one. Add `--extra browser` (or another engine
extra) when the operation you need declares it.

## Layout

Where things are. A *plane* is one family of related operations (web,
documents, numbers), shipped as one package.

```
packages/cruxible-provider-runtime/   the support library every provider uses
packages/cruxible-provider-noop/      the reference provider: the smallest
                                      package that exercises every rule
packages/cruxible-provider-web/       the web plane: web.fetch(.pages), search.web, web.render(.dom)
packages/cruxible-provider-docs/       the document plane: doc.to_markdown, ocr.extract
packages/cruxible-providers/          the umbrella meta-package: zero code, plane extras
packages/cruxible-provider-quant/     the quantitative plane: classical
                                      baselines on the seven quant operations
packages/cruxible-provider-workspace/ workspace.file, the pure Source adapter
                                      core's built-in was ported from
packages/_template/                   copy this to start a new plane package
ci/marker-environments.json           the environments every digest is computed for
scripts/                              digest computation, the packaging checks, and
                                      the protocol 2.0 contract export core vendors
docs/                                 the packaging rules and the core seam
```

## The three levels of identity

A provider carries three identifiers, and confusing them is the mistake this
design exists to prevent.

**`implementation_digest`** — backend-invariant. It covers the entrypoint object
path and the provider distribution's own sha256. Never a bare version, which is
a claim rather than an identity. Switching a provider from local execution to a
container does not change it and does not split earned track record. Which
operation a run executed — and under which exact `{in, out, effect}` signature
and build — is recorded beside it, because one entrypoint may serve several
operations.

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
registration, and the recorded fixtures, while each engine sits behind an
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

Two implementations of two operations in one package may therefore bind two
different environments — `doc.to_markdown` binds one with a conversion engine,
`ocr.extract` one with an OCR runtime — and an artifact that pins only one of
them refuses the other rather than falling back.

**Every launch engine closure pins on every declared environment.** A declared
tag list is read as an ordering (PEP 425/600) rather than as literal names, and
the launch floors are `manylinux_2_28` and `macosx_14_0`. Consequently
`+browser`, `+paddleocr`, and `+docling` all resolve on the three environments in
`ci/marker-environments.json`. Raising those floors deliberately re-keyed every
package's materialization digest before any Provider artifact was accepted; it
does not enter or move the backend-invariant implementation digest.
`docs/packaging.md` carries the table, cloud base-image coupling, and Windows
posture.

The consequence for testing is the point of the whole arrangement: `uv run
pytest` installs no engine and downloads nothing. Real-engine tests exist, are
marked `engine`, are excluded from the default run by construction, and have
their own opt-in CI lane. What they are *for* is keeping the recorded engine
responses honest: they run the real engine over the same shipped bytes and assert
it still produces what the recording claims.

## Fail closed, everywhere

Every path in this repository refuses rather than guessing: an unknown manifest
field, an operation the entrypoint does not declare, an unsupported protocol
major (a 1.x run context included), a lock that resolves away from its pin, a
cache entry whose seal does not verify, a refusal code the operation does not
declare, a budget breach, an endpoint contacted outside the declaration. The
runtime's own refusals are closed and enumerated in
`cruxible_provider_runtime.errors.RefusalCode`; an operation's refusals are the
ones its registration declares, and the two never overlap.

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
- `web.fetch` and `web.fetch.pages` retrieve URLs a run names, so they refuse
  loopback, private-network, link-local and other non-public targets on every
  hop and pin each connection to the address they checked. `web.render.dom`
  checks the URL the run names and the address that served the page it returns,
  but a browser opens its own connections: the redirects, subresources and
  scripts a page pulls in between are recorded, not vetted. `search.web` is not
  guarded; its instance is operator configuration. See the
  [web package](packages/cruxible-provider-web#private-addresses).
- `UvSyncBuilder`, the production local builder, needs a `uv` on the path, and a
  test that has one drives it end to end against a PEP 503 index under `file://`
  built from wheels the test itself produces — no network. What it cannot cover
  is the runtime living in the sealed environment, because an offline index
  cannot serve the runtime's own third-party dependencies: the sealed
  interpreter there resolves and calls the pinned
  entrypoint the way the child harness does rather than through it, and the
  harness is covered end to end by the reference provider's suite.

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

The template expects to sit under `packages/`, beside the runtime: its
`[tool.uv.sources]` names `../cruxible-provider-runtime`. The repository tests
(`tests/test_repo_layout.py`) then also require the new package to be wired in:

- add it to the root `pyproject.toml` (`dependencies`, `[tool.uv.sources]` and
  `testpaths`) so `uv run pytest` at the root reaches it;
- add it to the `typecheck` matrix in `.github/workflows/ci.yml`;
- give it a `container/Dockerfile` whose `ENTRYPOINT` is the secret shim (the
  template has none; copy the noop package's `container/` and adjust);
- add its name to the package list in `test_there_are_real_packages`.

A plane meant for the umbrella also needs an extra in
`packages/cruxible-providers/pyproject.toml`, in its `all` extra, and in the
umbrella test's expected set. A manual publish run needs the name in
`.github/workflows/publish.yml`'s package choices; a tag push does not.
