# cruxible-provider-web

Cruxible provider adapters for the web plane. Apache-2.0.

| Operation | Entrypoint | Engine | Extra |
|---|---|---|---|
| `web.fetch` | `cruxible_provider_web.fetch:WebFetch` | trafilatura (base), Playwright (extra) | `browser` |
| `search.web` | `cruxible_provider_web.search:SearxngSearch` | none — an HTTP client to somebody else's instance | — |

Operators install it into a daemon by name, through a governed install:
`cruxible playbill provider install cruxible-provider-web`. The umbrella
`cruxible-providers[web]` exists for developers.

## The base install is light

The base distribution carries the adapter logic, the registration, an HTTP
client, an extractor, and the recorded exchanges the conformance suite
replays. `uv run pytest` therefore downloads no browser and
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

### The browser environment pins

Resolved against the committed lock and the three launch marker environments in
`ci/marker-environments.json`:

| Extras | linux-cp311 | linux-cp312 | macos-arm-cp312 |
|---|---|---|---|
| *(base)* | resolves | resolves | resolves |
| `browser` | resolves | resolves | resolves |

The launch floors are now `manylinux_2_28` and `macosx_14_0`. Raising them
deliberately re-keyed every package's materialization digest before acceptance;
the browser closure remains resolvable on all three and selects the compatible
binary artifacts an installer on those floors would prefer.

`+browser` refused on all three until the resolver stopped matching declared tags
by exact string membership. Playwright publishes `py3-none-manylinux1_x86_64`,
`py3-none-macosx_11_0_universal2` and friends — none of them among the three
literal tags a launch environment lists, and every one of them installable on
the environment those tags describe. `docs/packaging.md` carries the rule.

## What the output claims

`web.fetch` returns the shared
`playbill-provider-result-to-external-capture-v1` acquisition envelope. Its
base64 content is a canonical JSON observation bundle. `search.web` retains its
existing result shape. Both distinguish:

- **`retrieved`** — what came off the wire. A record of an exchange that
  happened, and the material a CaptureContract may grade as observed-shaped.
- **`derived`** — what an extractor or this adapter made of it. Derived under
  every contract, whatever the engine's confidence.

A rendered fetch is where that line is easiest to lose, so `web.fetch` draws it
twice. `retrieved` describes the main-frame response the browser actually
received — the final URL after its redirects, the status it ended on, its
headers, and the exact body an origin sent (`body_base64`, byte count and digest).
The DOM a browser assembles
is script output over that body, not a body anyone sent, so it is reported under
`derived.assembled_document` with its own byte count and digest. A page that
redirects across origins and settles on a 404 a script repaints is a failed
retrieval, and it is reported as one.

**Two things about `retrieved` changed shape, and a contract author needs both.**
`byte_count` and `body_sha256` now describe the **wire bytes**; a rendered run
used to report them over the assembled DOM, so a contract comparing digests
across renders is comparing a different artifact than it was. And on a rendered
run `status_code`, `byte_count` and `body_sha256` are **nullable**: a navigation
that yields no main-frame response — a same-document navigation, a download —
leaves nothing true to say about the wire, and `null` says so where a hopeful
`200` used to. The acquisition operation now refuses a navigation without a
retained origin body; it cannot produce a successful Capture result in that case.

The operation's input Contract (`web.fetch.request@1`) carries ordinary fields,
and its output Contract (`web.fetch.result@1`) is the shared acquisition
result. `logical_source` defaults to `web.response`; callers should
name their dataset and pin that name in the CaptureContract. URLs remain in the
observation bundle, while the source coordinate carries a request digest.
`expected_format` accepts `auto`, `html`, `json`, `csv`, `text`, or `bytes`.
Explicit format mismatches refuse an observation; JSON and CSV are validated
and bypass HTML extraction. `max_bytes` is bounded at 32 MiB, including for
structured APIs. Set the invocation output and Capture budgets large enough for
the bundle's base64 encoding and extraction overhead as well as the origin body.

The operation's effect is `external_read`: it reads the world and mutates nothing.
This still contacts external systems and records egress. `credential_header`
selects a header for an admitted `credential_ref`; rendered fetches with
credentials are refused.

Neither adapter mints a Capture. They return a typed payload plus trace; the
executor carries both to the CaptureContract, which decides the grade. A provider
that graded its own output would be certifying itself.

`search.web` now returns that same acquisition envelope. The captured material
retains the exact HTTP body separately from normalized results and the derived
recency-filtered ranking. Its logical source is `search.response`; the explicit
`instance_url` configuration chooses the search service, rather than a test host
being embedded in the package's endpoint declaration. `as_of`, when supplied,
is the evaluation instant for filtering; observation time is recorded separately.

## Egress

`web.fetch` declares the **experimental** `dynamic:target-from-run-input` form.
Its target *is* the run input, so an endpoint list fixed at acceptance time could
only ever be wrong; what governs instead is the recording. Every request the
client issues — redirect hops included — reaches the run's egress recorder
through an httpx event hook, and the receipt records that the declaration was
dynamic so an empty `undeclared` set is not misread as an allowlist that held.

`search.web` declares `dynamic:target-from-configuration`. The exact service
origin comes from retained invocation configuration; it is recorded like other
dynamic egress. This is not a network allowlist or a hosted containment policy.
The adapter continues to enforce an explicit static allowlist when one is supplied.

A **rendered** fetch does not go through that client: a browser opens its own
sockets and pulls whatever the markup names. It gets its own hooks, on the page's
`request` and `response` events, so the redirect it follows, the CDN its script
comes from and the API that script queries all land in the same recorder.

## Credentials and redirects

`web.fetch` lets a run name the header its credential travels on, so this
package's HTTP client follows redirects **by hand**: httpx would carry every
header it was given to the destination, stripping only `Authorization`, and a
token on `x-api-key` would reach a host the run never named before the recorder
learned that host existed.

An authenticated fetch redirected to a **different origin refuses**, rather than
following the hop anonymously. A run that names a credential does not
silently go out anonymous — the undelivered-credential refusal
already says so — and stripping would hand the caller a document retrieved under
terms the receipt no longer describes. An `http` → `https` upgrade of the same
host is not an origin change for this purpose: the credential has already crossed
the wire in clear. An unauthenticated chain is followed to its end, and every hop
of it is recorded.

`search.web` declares a concrete origin, because its target is configuration
rather than input. The instance is named per run by an `instance_url` coordinate
and refused (`undeclared_instance`) unless the accepted declaration carries it.

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

## Operations and their refusals

`registration.json` declares both operations (runtime protocol 2.0). Each names
its input and output Contracts by `name@N` — the install pins them to the
versions accepted at its base — and declares the typed refusals it may return,
each with a detail Contract:

| Operation | In | Out | Effect | Refusals |
|---|---|---|---|---|
| `search.web` | `search.web.request@1` | `search.web.result@1` | `external_read` | `provider_declined` (absorbable), `redirect_limit`, `unsupported_redirect_scheme`, `cross_origin_credentialed_redirect`, `undeclared_instance` |
| `web.fetch` | `web.fetch.request@1` | `web.fetch.result@1` | `external_read` | `provider_declined` (absorbable), `redirect_limit`, `unsupported_redirect_scheme`, `cross_origin_credentialed_redirect`, `credential_unavailable`, `engine_unavailable` |

This package ships the bodies of all fifteen, and of the three nested Contracts
they pin (`web.capture.coordinate@1`, the zero-field `web.capture.selector@1`,
and `web.capture.source_effective_time@1`), under
`src/cruxible_provider_web/contracts/<name>/v1.json`. Each file is the exact
member Core accepts at `contracts/<name>/v1.json`; accept
`cruxible_provider_web.contract_members()` before installing the package.
`scripts/render_web_contracts.py` renders them from definitions derived from the
adapters' code, with Core's Contract model. Optional fields are absent keys,
never `null`: the shared result omits `source_effective_time` when the adapter
states no validity window, and a refusal detail omits what it cannot name.

A credential the run names but did not deliver (`credential_unavailable`), an
instance outside the declaration (`undeclared_instance`) and a missing browser
(`engine_unavailable`) are the operations' own declared refusals. Runtime codes
come from the runtime's machinery only; provider code never raises one.

Weight and freshness are declared by the caller and *checked* rather than
trusted: a response heavier than the run's `max_bytes` refuses, and the recency
filter is evaluated against an explicit `as_of` coordinate so the derivation can
be reproduced from the receipt.

## Running the tests

```sh
uv run pytest packages/cruxible-provider-web/tests -q     # default: no engines
uv run pytest -m engine packages/cruxible-provider-web    # needs the browser extra
```

The engine lane drives a real browser over a local file. It is opt-in, it is not
in the default CI lane, and every test in it skips with a reason when the engine
is absent, so collecting it on a machine with no engines still works.
