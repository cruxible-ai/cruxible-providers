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

The check computes each package's materialization digests at the base revision
and in the working tree, and fails if more than one package moved. A change that
genuinely needs to move two packages should be two commits.

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
| `uv.lock` | The identity source. Committed. |
| `src/<module>/manifest.yaml` | The package-side manifest. A transcription source, **never** authority. |
| `src/<module>/py.typed` | Typing marker |
| `LICENSE`, `NOTICE` | Apache-2.0, per package |
| `container/Dockerfile` | The container backend's build, when the package declares that backend |
| `container/provenance.md` | The four provenance fields the executor checks |
| `tests/` | The conformance suite, which every plane package inherits |

## Releasing

The distribution sha256 that enters the implementation digest is the hash of the
built artifact, so it does not exist until release. The package-side manifest
therefore names only the distribution's name and version; the accepted Provider
artifact carries the hash. A manifest that tried to contain its own artifact's
hash would be impossible to produce.
