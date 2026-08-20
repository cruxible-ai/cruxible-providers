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

An umbrella meta-package (`cruxible-providers`, a pure dependency shell with
plane extras) is planned for install ergonomics once the first plane packages
exist. It ships no code, so it enters no implementation digest. End users never
install providers at all — providers are fetched on bind.

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
| `tests/` | The conformance suite, which every plane package inherits |

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
