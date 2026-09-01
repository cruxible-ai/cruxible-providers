# Packaging rules

## One distribution per plane

`cruxible-provider-<plane>` is the identity, lock, and digest unit. One package
may implement several interfaces within its plane.

The rejected alternative was a single distribution with per-plane extras. It
fails for a specific reason rather than an aesthetic one: the implementation
digest covers the provider distribution's own sha256, so a single distribution
would put **one** sha256 under **every** provider's implementation digest, and
every release would re-digest every provider — vaporising track record earned
across the whole fleet on each publish.

The umbrella meta-package `cruxible-providers` — a pure dependency shell with one
extra per plane — exists for install ergonomics now that the first plane packages
do. It ships no code, so it enters no implementation digest. End users never
install providers at all: providers are fetched on bind, and the umbrella serves
developers.

Because its entire content is other packages, the umbrella declares itself exempt
from the one-package-one-digest-change gate (`[tool.cruxible] digest_scope =
"exempt"`). Its closure moves whenever any plane's does, so measuring it would
report two changed packages for every one-package change; and it pins no
environment, carries no implementation digest, and appears on no track record, so
there is nothing the gate would be protecting. The exemption is a written field
rather than a name the script recognises, and a repository test asserts it is the
only one.

## Per-engine extras, and the environment pin key

A heavy engine — a browser, a document-conversion stack, an OCR runtime — never
enters a base install. It sits behind an extra, and the implementation that needs
it declares that extra in its manifest (`requires_extras`).

The extras are a **resolution input**, not a note. `resolve()` walks the root's
`optional-dependencies` for exactly the extras named and follows dependency-level
extras (`{ name = "x", extra = ["y"] }`) transitively — the launch document plane
reaches its engine through three of those, and dropping them would produce an
environment pin that did not cover the environment. Selecting an extra therefore
changes the resolved set, and the resolved set is what the materialization
preimage hashes. Nothing about extras is added to the preimage separately: their
effect is the packages they pull in, and a second statement of the same fact is a
second thing that can be wrong.

One lock consequently produces one environment per extras set, and an accepted
artifact pins each under an **environment pin key**:

```
linux-cp311                     the base environment
linux-cp311+browser             the same lock, resolved with a browser
linux-cp311+docling+paddleocr   sorted, so the key is a set rather than an order
```

The no-extras spelling is the bare environment id, so every pin written before
extras existed still reads correctly; the two spellings cannot collide because an
extra name is never empty. Bind derives the extras from the **manifest**, never
from the bind request: a request that could name its own extras could materialize
an environment the accepted artifact never pinned. An artifact that pins one
implementation's environment and not another's refuses the second
(`lock_mismatch`, naming the key) rather than falling back to a neighbour's.

An extra the lock's root does not declare refuses with `unknown_extra` rather
than resolving to the base set. Resolving to the base set would be the fail-open
reading: the environment would come out one engine short and every digest over it
would still verify.

### A declared tag list is an ordering, not a vocabulary

A marker environment lists a handful of tags. Those tags name the *most
preferred* member of each family the environment supports, and
`cruxible_provider_runtime.tags` expands them into the ordered list an installer
would compute. Reading them as literal names instead is what made every engine
environment unpinnable in RP-1: PEP 600 says a `manylinux_2_5` wheel installs on
a `manylinux_2_17` host, PEP 425 says an `abi3` wheel built against `cp39`
installs on `cp311` and that a `py3-none-<platform>` wheel installs on any
interpreter of that major version, and three literal tags cannot enumerate the
dozen a binary closure reaches for — a browser driver ships
`py3-none-manylinux1_x86_64`, an OCR runtime ships
`cp311-cp311-manylinux1_x86_64`, and exact membership matched neither.

The expansion is derived from the declared tags and markers and from nothing
else — never from the running interpreter — and it enters **no digest preimage**:
`MarkerEnvironment.digest_payload()` carries the declared list, so the expansion
is not itself something a pin is keyed on.

#### Resolver tag-ordering correction (2026-08-20): four selected-artifact re-keys

That is a narrower statement than it looks, and the wider version of it is
false. **The 2026-08-20 resolver correction re-keyed four environment pins**,
correctly:
`cruxible-provider-quant` on all three declared environments and
`cruxible-provider-web` on `macos-arm-cp312` now resolve to different artifacts,
because the resolver selects the binary wheels an installer would select instead
of falling back to an sdist or to `py3-none-any` — an abi3 `polars-runtime-32`,
an abi3 `igraph`, a universal2 `lxml` and `charset-normalizer`. A materialization
digest hashes the resolved set, so a resolution that changes moves the digest.
Any pin computed before this change must be recomputed, and any accepted Provider
artifact carrying one must be re-issued.

What the preimage property does buy is the *next* such change: widening the
expansion again moves a pin only where it moves the resolution, and never by
re-keying the environment itself.

Reproduced against the committed locks, for the three environments in
`ci/marker-environments.json`, and asserted by
`tests/test_engine_environments.py`:

| Package | Extras | linux-cp311 | linux-cp312 | macos-arm-cp312 |
|---|---|---|---|---|
| `cruxible-provider-web` | *(base)* | resolves | resolves | resolves |
| `cruxible-provider-web` | `browser` | resolves | resolves | resolves |
| `cruxible-provider-docs` | *(base)* | resolves | resolves | resolves |
| `cruxible-provider-docs` | `docling` | resolves | resolves | resolves |
| `cruxible-provider-docs` | `paddleocr` | resolves | resolves | resolves |

#### Floor re-baseline (2026-09-01): global marker-payload re-key

The declared Linux environments target `manylinux_2_28`; the declared macOS
environment targets `macosx_14_0`. Those floors admit every engine closure the
launch manifests require, including Docling's tensor stack. The Linux floor is
also the cloud base-image intent: a cloud image that claimed a lower glibc floor
could not honestly materialize the same accepted pin.

Raising the floors changed every declared marker-environment payload, so **every
package's local materialization digest moved**, including base environments and
extras sets whose selected wheels did not change. That global re-key was
deliberate, pre-acceptance, and free: no accepted Provider artifact carried the
old pins, so nothing needed re-issuance. The marker environments do not enter the
backend-invariant implementation preimage, and this slice changes no interface,
entrypoint, or released distribution pin; implementation digests do not move.

Windows development uses WSL2 now. Native Windows is demand-gated and, if
needed, arrives later as an **additive** marker environment. It will create new
materialization digests for that environment without re-keying any existing
Linux or macOS pin.

The base lane also continues to resolve for every package on all three launch
environments, so the floor raise adds the Docling closure without trading away
an existing environment.

## Test directories are not packages

A package's `tests/` directory carries **no `__init__.py`**, and the suite runs
under `--import-mode=importlib`. The reason is mechanical: with `__init__.py`,
every package's test directory is an importable package named `tests`, and the
second one collected is imported under the first one's module name and refused —
so adding a plane package breaks an unrelated package's suite. Under importlib
mode the module name is derived from the path, `from .conftest import` keeps
working, and test modules may be named whatever suits their package.
`tests/test_repo_layout.py` asserts both halves.

## One lock per package

Every package carries its **own committed `uv.lock`**. There is no shared
uv-workspace root lock.

The reason is the same reason as above, one level down: a shared lock makes
every package's resolved set move whenever any dependency anywhere moves. Every
provider would be re-pinned, re-verified, re-materialised, and — in the cloud
backend — re-attested, because of a change in a package it does not depend on.

This is enforced, not merely documented:

```sh
uv run python scripts/check_single_package_digest_change.py --base origin/main
```

The check computes each package's **dependency-closure** digests at the base
revision and in the working tree, and fails if more than one package moved. A
change that genuinely needs to move two packages should be two commits.

Closure digest, not materialization digest, and the distinction is not
pedantry. A materialization digest also pins the root distribution's sha256,
which does not exist until the package is built and which moves on every release
by construction — it would drown out the signal the gate is looking for. The
closure digest covers the root's *name*, the marker environment, and the
resolved dependency set: exactly what a dependency bump moves.

An unresolvable base **fails** the check. A gate that waves changes through
whenever it cannot do its job reports green in precisely the situation nobody
checked. `--allow-missing-base` exists for the initial commit and has to be
asked for.

### Local sources

A provider environment admits **registry artifacts only**. A path, git,
editable, or direct-URL dependency has no artifact hash, so it cannot be pinned;
such a source refuses with `unresolvable_source`. An earlier cut silently
dropped these entries instead, which produced an environment pin that did not
cover part of the environment.

`allow_editable_dev_sources` is a development-only escape hatch, false
everywhere by default. It admits local sources under a path-derived identity so
that this monorepo can bind its own in-tree packages before they are published,
and so that the digest-scope gate can see cross-package edges. A binding
computed with it set records `dev_sources_permitted` in its snapshot, so a
receipt shows it.

An accepted Provider artifact cannot be produced this way in any case: the
artifact's `DistributionPin` requires a `sha256` that a local source does not
have and cannot be given. The escape hatch is therefore structurally confined to
pre-publication development — it cannot leak into anything governed.

### The root lock is not an identity source

The repository root carries a `pyproject.toml` and a `uv.lock`. They exist so
that `uv run pytest` at the root can drive the whole suite. The root project is
`cruxible-providers-dev`, is marked `package = false`, has no build system, and
is never published. It is not a uv workspace root — a workspace would suppress
the member locks that carry identity — it merely declares path sources onto the
packages.

`tests/test_repo_layout.py` asserts all of this, so the distinction cannot decay
into a comment nobody reads.

## The marker environments

`ci/marker-environments.json` lists the `(python version, platform tag)`
environments every materialization digest is computed for. Deriving an
environment from whatever interpreter happened to run would produce a digest
that cannot be reproduced anywhere else, which is not an identity.

Changing that file re-pins every package at once. That is why it is a committed
artifact rather than a runtime default, and why the digest-scope check holds the
environment list fixed across revisions: re-pinning the environments is a
deliberate, separate act.

## What goes in a package

| File | Role |
|---|---|
| `pyproject.toml` | Distribution metadata, dependencies, `cruxible.providers` entry points |
| `uv.lock` | The identity source. Committed, and compared byte-for-byte at bind against what the accepted artifact pinned. |
| `src/<module>/manifest.yaml` | The package-side manifest. A transcription source, **never** authority. |
| `src/<module>/py.typed` | Typing marker |
| `LICENSE`, `NOTICE` | Apache-2.0, per package |
| `container/Dockerfile` | The container backend's build, when the package declares that backend |
| `container/provenance.md` | The four provenance fields the executor checks |
| `src/<module>/vocab/*.yaml` | The bucket vocabularies the package classifies against, copied from `vocab/interfaces/`; a repository test asserts the copies do not fork |
| `src/<module>/recordings/`, `fixtures/` | Recorded exchanges or engine responses, and the per-bucket conformance fixtures that replay them |
| `tests/` | The conformance suite, which every plane package inherits. No `__init__.py` |

### Test packages carry no `__init__.py`

Every package's suite lives in `tests/`, and that directory must **not** contain
an `__init__.py`. With one, pytest derives the module name by walking up while
`__init__.py` exists, so every package's suite would be called `tests` and the
second one imported collides with the first (`ImportPathMismatchError`). Without
one, and with `--import-mode=importlib` set in the root `addopts`, pytest derives
a name from the path — `packages.cruxible-provider-quant.tests.conftest` — which
is unique per package by construction.

Relative imports inside a suite (`from .conftest import ...`) keep working: pytest
inserts the parent modules for the derived name.

## Releasing

The distribution sha256 that enters the implementation digest is the hash of the
built artifact, so it does not exist until release. The package-side manifest
therefore names only the distribution's name and version; the accepted Provider
artifact carries the hash. A manifest that tried to contain its own artifact's
hash would be impossible to produce.


## The two lock checks at bind

Bind checks the lock twice, and the checks are not redundant.

1. **Bytes** (`lock_bytes_mismatch`) — cheap tamper-evidence over the exact file
   that was reviewed. An accepted artifact pinned a specific lock; a different
   file, however innocently reformatted, is one nobody approved. Re-accepting a
   rewritten lock is a governance step, not an inconvenience to route around.
2. **Resolution** (`lock_mismatch`) — the primary gate. The lock must resolve,
   for the target marker environment, to the materialization digest the accepted
   artifact pinned.

Identity is still never *keyed* on lock bytes: the materialization digest hashes
the resolution, and a reformatted lock produces a byte-identical one. The test
`test_formatting_churn_refuses_on_bytes_but_does_not_move_the_resolution` asserts
both halves of that sentence at once.
