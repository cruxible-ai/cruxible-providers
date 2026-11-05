# cruxible-provider-web

Cruxible provider adapters for the web plane. Apache-2.0.

| Operation | Entrypoint | Engine | Extra |
|---|---|---|---|
| `web.fetch` | `cruxible_provider_web.fetch:WebFetch` | none — one HTTP GET | — |
| `search.web` | `cruxible_provider_web.search:SearxngSearch` | none — an HTTP client to somebody else's instance | — |
| `web.render` | `cruxible_provider_web.render:WebRender` | trafilatura (base) — a pure derive | — |
| `web.render.dom` | `cruxible_provider_web.dom:WebRenderDom` | Playwright (extra) — a browser | `browser` |

Operators install it into a daemon by name, through a governed install:
`cruxible playbill provider install cruxible-provider-web`. The umbrella
`cruxible-providers[web]` exists for developers.

## The base install is light

The base distribution carries the adapter logic, the registration, an HTTP
client, an extractor, and the recorded exchanges the conformance suite
replays. `uv run pytest` therefore downloads no browser and
opens no socket.

A **browser** is the one heavy engine here, and it lives behind the `browser`
extra. The `web.render.dom` manifest row declares that extra in
`requires_extras`, which is what makes the resolver materialize an environment
containing it — and what makes that environment different from the base one
`web.fetch`, `search.web` and `web.render` bind. One lock, two environments, two pins in the accepted artifact
(`linux-cp311-engines` and `linux-cp311-engines+browser`).

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

## A Capture is the payload, verbatim

Every operation here returns the shared
`playbill-provider-result-to-external-capture-v1` Capture wire, and its bytes are
the document itself, in whatever format it is — never a bundle around it:

- **`web.fetch`** — the body the origin sent, exactly. JSON, CSV, gzip, HTML or
  binary, it is committed as received; core reads it only through a declared
  `extract` under the CaptureKind's decoding, and never parses it at admission.
- **`search.web`** — the instance's response body, exactly. No ranking or
  recency filter is derived here; the instance's own `time_range` parameter is
  part of the request.
- **`web.render.dom`** — a fetch through a browser: it navigates to the URL the
  run names, runs the page's scripts, pulls its subresources, and the serialized
  DOM is the Capture. Use it for pages that carry nothing until assembled.
- **`web.render`** — a **derive** (D-R37): pure computation over the exact bytes
  of a Capture this run fetched (`source: capture<*>`; core hands the operation
  that Capture's wire, and `cruxible_provider_runtime.acquisition.capture_input`
  reads it). `text` is the main content as Markdown; `json` is canonical JSON
  `{url, final_url, title?, text, links}` (sorted keys, no insignificant
  whitespace; `links` are the page's http(s) `<a href>` targets, resolved,
  fragment dropped, first occurrence kept, in order). It contacts nothing and
  reads no clock: its observation time is the cited Capture's, so identical
  input gives a byte-identical result, the same Capture, and Claims that
  re-observe. The result is its own derived Capture, citing the fetched one.

How the bytes were obtained is the typed coordinate, `web.capture.coordinate@1`,
never inside the bytes: `url` and `final_url` (core's acquisition origin gate
reads both), the 2xx `status`, the bytes' `content_type`, `request_digest`, the
kept freshness `headers` (`etag`, `last_modified`, `content_length`), and
`source` (`network`, or `packaged-recording`). A derived document adds
`renderer` (the engine) and `derived_from` (the content digest of the Capture it
was made from) and otherwise carries the cited document's coordinate; a
browser-assembled page adds `renderer` (the browser) to its main frame's
coordinate. `pages`
is declared for an operation that assembles the pages of one listing into one
document; nothing in this plane pages today.

`web.fetch.request@1` carries ordinary fields. `query` is a typed list of
parameters, `{name, values, join?}` (`web.fetch.query_param@1`), so a Procedure
can pass values it read from state rather than spelling them into a URL (D-R34):
they are percent-encoded onto `url` in the order given, each value as its own
`name=value` (`join: repeat`, the default) or comma-joined into one
(`join: comma`, as an API taking `cve=A,B` wants). A parameter needs at least one
value, a name appears once, and a typed name the `url`'s own query already
carries (compared decoded) refuses, so a stale filter in the URL can never ride
along beside the one the run derived. Repeated values inside one parameter stay
allowed. A request URL longer than 8192 characters refuses, whether a query
built it or it arrived that long. The built URL is the request: the coordinate's `url` records it and
`request_digest` covers it. `logical_source` defaults to
`web.response`; callers should name their dataset and pin that name in the
CaptureKind. `expected_format` accepts `auto`, `html`, `json`, `csv`, `text`, or
`bytes`: an explicit mismatch refuses, and JSON and CSV bodies are validated by
the adapter before they are returned (the provider checks its own payload; core
does not). `max_bytes` is bounded at 32 MiB, including for structured APIs.

The operation's effect is `external_read`: it reads the world and mutates nothing.
This still contacts external systems and records egress. `credential_header`
selects a header for an admitted `credential_ref`.

No adapter mints a Capture. Each returns Capture wire plus trace; the executor
carries both to the CaptureKind, which governs them. A provider that graded its
own output would be certifying itself.

`search.web`'s logical source is `search.response`; the explicit `instance_url`
configuration chooses the search service, rather than a test host being
embedded in the package's endpoint declaration.

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

`web.render.dom` does not go through that client: a browser opens its own
sockets and pulls whatever the markup names. It gets its own hooks, on the page's
`request`, `response` and `websocket` events, so the main frame, the CDN its
script comes from, the API that script queries and any `ws`/`wss` stream the page
opens land in the same recorder (a WebSocket endpoint as `wss://host[:port]`,
normalised like an HTTP origin). It declares
`dynamic:target-from-run-input`: the run names the page, and the page names its
subresources. `web.render` declares no endpoints: a derive contacts nothing.

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
caller actually asked for; and a run served from one says so, in the
coordinate's `source` and in an event on its trace. A recorded browser render
(`recorded:<id>`) replays the post-assembly DOM recorded for that URL. The recordings are served
through an `httpx.MockTransport`, so the real client, the real event hook, and
the real size cap all execute — what is replaced is the socket, and nothing above
it.

## Operations and their refusals

`registration.json` declares all four operations (runtime protocol 2.0). Each names
its input and output Contracts by `name@N` — the install pins them to the
versions accepted at its base — and declares the typed refusals it may return,
each with a detail Contract:

| Operation | In | Out | Effect | Refusals |
|---|---|---|---|---|
| `search.web` | `search.web.request@1` | `search.web.result@1` | `external_read` | `provider_declined` (absorbable), `redirect_limit`, `unsupported_redirect_scheme`, `cross_origin_credentialed_redirect`, `undeclared_instance` |
| `web.fetch` | `web.fetch.request@1` | `web.fetch.result@1` | `external_read` | `provider_declined` (absorbable), `redirect_limit`, `unsupported_redirect_scheme`, `cross_origin_credentialed_redirect`, `credential_unavailable` |
| `web.render` | `web.render.request@1` | `web.render.result@1` | `pure` | `provider_declined` (absorbable) |
| `web.render.dom` | `web.render.dom.request@1` | `web.render.dom.result@1` | `external_read` | `provider_declined` (absorbable), `engine_unavailable` |

This package ships the bodies of all twenty-one, and of the six nested Contracts
they pin (`web.capture.coordinate@1` with `web.capture.headers@1` and
`web.capture.page@1`, the zero-field `web.capture.selector@1`,
`web.capture.source_effective_time@1`, and `web.fetch.query_param@1`), under
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

Weight is declared by the caller and *checked* rather than trusted: a response
heavier than the run's `max_bytes` refuses.

`tests/test_web_capture_conformance.py` holds the payload rule for every
Capture-producing operation the registration declares, in every form Core reads
(the whole result, a `capture<K>` field, each member of a `list<capture<K>>`
field): against every recorded exchange, each member's stored bytes are the
origin bytes (digest equality), carry no wrapper keys and have their own
coordinate, and a derived document's coordinate cites the bytes it read. The
repository's `tests/test_pure_operations.py` runs `web.render` (like every
`effect: pure` operation) twice with the network blocked and requires
byte-identical output.

## Running the tests

```sh
uv run pytest packages/cruxible-provider-web/tests -q     # default: no engines
uv run pytest -m engine packages/cruxible-provider-web    # needs the browser extra
```

The engine lane drives a real browser over a page served from a loopback server. It is opt-in, it is not
in the default CI lane, and every test in it skips with a reason when the engine
is absent, so collecting it on a machine with no engines still works.
