# cruxible-provider-web

Cruxible provider adapters for the web plane. Apache-2.0.

| Interface | Entrypoint | Engine | Extra |
|---|---|---|---|
| `web.fetch` | `cruxible_provider_web.fetch:WebFetch` | trafilatura (base), Playwright (extra) | `browser` |
| `search.web` | `cruxible_provider_web.search:SearxngSearch` | none — an HTTP client to somebody else's instance | — |

End users do not install this package: providers are fetched on bind. The
umbrella `cruxible-providers[web]` exists for developers.

## The base install is light

The base distribution carries the adapter logic, the schemas, the bucket
classifiers, an HTTP client, an extractor, and the recorded exchanges the
conformance fixtures replay. `uv run pytest` therefore downloads no browser and
opens no socket.

A **browser** is the one heavy engine here, and it lives behind the `browser`
extra. The `web.fetch` manifest declares that extra in `requires_extras`, which
is what makes the resolver materialize an environment containing it — and what
makes that environment different from the one `search.web` binds. One lock, two
environments, two pins in the accepted artifact (`linux-cp311-engines` and
`linux-cp311-engines+browser`).

Trafilatura is base rather than an extra on purpose. The split exists to keep a
browser, a tensor stack, and an OCR runtime out of the base install; a
pure-Python extractor over an lxml wheel is none of those, and putting it behind
an extra would buy a few megabytes at the price of a default lane that never runs
a real extraction.

## What the output claims

Both implementations split their output in two, and the split is the contract:

- **`retrieved`** — what came off the wire. A record of an exchange that
  happened, and the material a CaptureContract may grade as observed-shaped.
- **`derived`** — what an extractor or this adapter made of it. Derived under
  every contract, whatever the engine's confidence.

Neither adapter mints a Capture. They return a typed payload plus trace; the
executor carries both to the CaptureContract, which decides the grade. A provider
that graded its own output would be certifying itself.

## Egress

`web.fetch` declares the **experimental** `dynamic:target-from-run-input` form.
Its target *is* the run input, so an endpoint list fixed at acceptance time could
only ever be wrong; what governs instead is the recording. Every request the
client issues — redirect hops included — reaches the run's egress recorder
through an httpx event hook, and the receipt records that the declaration was
dynamic so an empty `undeclared` set is not misread as an allowlist that held.

`search.web` declares a concrete origin, because its target is configuration
rather than input. The instance is named per run by an `instance_url` coordinate
and refused (`undeclared_egress`) unless the accepted declaration carries it.

**As shipped, the declared instance is the reserved conformance host, so this
package is not deployable against a real instance as-is.** Under
manifest-verbatim transcription, *which* instance a deployment queries is part of
what the accepted artifact says, so pointing this adapter at a real instance means
publishing a distribution whose manifest declares that instance — which also makes
the instance part of the implementation's identity and of its track record.
Whether that is the intended cost is an open question for the artifact
vocabulary, recorded with the batch rather than decided here.

## The reserved fixture host

Every recorded exchange targets `fixture.invalid`. `.invalid` is reserved by
RFC 2606 and can never resolve, so a recording cannot stand in for a resource a
caller actually asked for; and a run served from one says so, in
`retrieved.source` and in an event on its trace. The recordings are served
through an `httpx.MockTransport`, so the real client, the real event hook, and
the real size cap all execute — what is replaced is the socket, and nothing above
it.

## Bucket claims

`web.fetch` claims static pages up to the medium weight class, one rendered face
of the cube, and light structured endpoints. Binary payloads, heavy pages, and
authenticated rendered pages are deliberately **unclaimed**, so an input reaching
for them refuses at admission rather than being served badly.

`search.web` claims shallow and standard depths at any-time, and shallow at the
recent bound. Realtime and deep are unclaimed.

Weight and freshness are declared by the caller and *checked* rather than
trusted: a response heavier than the cap the run was admitted under refuses, and
the recency filter is evaluated against an explicit `as_of` coordinate so the
derivation can be reproduced from the receipt.

## Running the tests

```sh
uv run pytest packages/cruxible-provider-web/tests -q     # default: no engines
uv run pytest -m engine packages/cruxible-provider-web    # needs the browser extra
```

The engine lane drives a real browser over a local file. It is opt-in, it is not
in the default CI lane, and every test in it skips with a reason when the engine
is absent, so collecting it on a machine with no engines still works.
