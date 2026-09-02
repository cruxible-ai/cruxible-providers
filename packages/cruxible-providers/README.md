# cruxible-providers

The umbrella meta-package for the Cruxible provider planes. Apache-2.0.

**Zero code.** This distribution is a dependency shell with one extra per plane,
and it exists for install ergonomics during development:

```sh
pip install "cruxible-providers[web]"
pip install "cruxible-providers[web,docs]"
```

## Who this is for

Developers. **End users never install a provider at all** — providers are fetched
on bind, into an isolated environment, from the pins an accepted Provider
artifact carries. Nothing in this package is ever part of an implementation
digest, a materialization pin, or a track record.

## Why the planes stay separate distributions

The identity unit is the per-plane distribution (`cruxible-provider-web`,
`cruxible-provider-docs`), and it has to be. A provider's implementation digest
covers its distribution's own sha256, so collapsing the planes into one
distribution with extras would put **one** sha256 under **every** provider's
implementation digest — and every release would re-digest every provider in the
fleet, vaporising track record earned across all of them. The umbrella ships no
code precisely so that it enters no digest.

## Extras and engines

A plane extra installs the plane package at its **base** install, which carries
no heavy engine. Which engine a deployment needs is a property of the
implementation it binds, not of the umbrella somebody installed, so the
per-engine extras stay on the plane package:

```sh
pip install "cruxible-providers[docs]"          # adapters, schemas, fixtures
pip install "cruxible-provider-docs[docling]"   # and the conversion engine
```

## Path sources are a development detail

The `[tool.uv.sources]` table in `pyproject.toml` points the extras at in-tree
paths so the monorepo can install its own umbrella before the plane packages are
published. A **published** umbrella carries no such table: its extras resolve
from the registry by distribution name. The distinction is not cosmetic — a path
source has no distribution sha256, so nothing published from one could ever be
registered as a Provider artifact.

## The planes

`web`, `docs`, `quant`, and `workspace` -- one extra per plane distribution
that exists, and `all` is exactly their union. A plane is wired the moment it
lands, never anticipated: naming a distribution that does not exist yet would
produce an umbrella that cannot resolve, and an `all` extra that means "all but
one" is worse than no `all` at all.

`workspace` is the built-in Source adapter core seeds by proposal. It is in the
umbrella for the same reason the others are -- a developer installing the planes
gets the built-in too -- and for no other: end users never install it, and core
pins its digests rather than its distribution.
