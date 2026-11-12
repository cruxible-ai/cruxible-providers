# cruxible-provider-web

## In plain terms

This package lets Cruxible read the web: download a page, search, and turn a
downloaded page into clean text. It is a **provider**, an add-on package that
does one kind of work for Cruxible, and each task it offers is an
**operation**. What an operation returns is a **Capture**: the exact bytes it
got, plus a record of where and how it got them.

| Operation | What it does | Network | Needs |
|---|---|---|---|
| `web.fetch` | You give it a URL (optionally with query parameters and a credential). It downloads that one URL and returns the response body exactly as sent, as one Capture. | yes | nothing extra |
| `web.fetch.pages` | Like `web.fetch`, for a query too long for one URL: you give it a list of values, it splits them across as few requests as fit, and returns one Capture per page. | yes | nothing extra |
| `search.web` | You give it a search query. It asks a SearXNG search service and returns that service's answer exactly as sent. | yes | a SearXNG instance the operator runs or chooses, named in the run's configuration |
| `web.render` | You give it a page this run already fetched. It returns the page's main text as Markdown, or as JSON with the title, text and links. It works only from those bytes. | no | nothing extra |
| `web.render.dom` | You give it a URL. It opens the page in a real browser, lets the page's scripts run, and returns the finished page. Use it for pages that show nothing until scripts build them. | yes | the `browser` extra (Playwright) and a Chromium browser matching that Playwright version |

`web.fetch`, `web.fetch.pages` and `web.render.dom` take their target from the
run, so they refuse addresses on private or internal networks (this machine,
the local network, cloud metadata services); *Private addresses* below says
exactly what each one checks.

What gets installed: the base package needs `httpx` and `httpcore` (to make
requests) and `trafilatura` (to pull main text out of HTML). The one optional
extra, `browser`, adds Playwright; it is only for `web.render.dom`, and the
Chromium browser itself is a separate download. The package also carries the
38 **Contracts** its operations use (a Contract is the declared shape of an
input or output) inside its registration, so it installs without a kit.

You do not need this package if nothing you run reads from the web.

## Reference

[Cruxible](https://github.com/cruxible-ai/cruxible) provider adapters for the web
plane (the web family of operations). Apache-2.0.

| Operation | Entrypoint | Engine | Extra |
|---|---|---|---|
| `web.fetch` | `cruxible_provider_web.fetch:WebFetch` | none — one HTTP GET | — |
| `web.fetch.pages` | `cruxible_provider_web.pages:WebFetchPages` | none — one HTTP GET per page | — |
| `search.web` | `cruxible_provider_web.search:SearxngSearch` | none — an HTTP client to somebody else's instance | — |
| `web.render` | `cruxible_provider_web.render:WebRender` | trafilatura (base) — a pure derive | — |
| `web.render.dom` | `cruxible_provider_web.dom:WebRenderDom` | Playwright (extra) — a browser | `browser` |

Operators install it into a daemon by name, through a governed install:
`cruxible provider install cruxible-provider-web` (add `--extra browser` for
`web.render.dom`). An unpublished version installs from a checkout instead; see
[*Installing from a checkout*](https://github.com/cruxible-ai/cruxible-providers/blob/main/README.md#installing-from-a-checkout)
in the repository README. The umbrella `cruxible-providers[web]` exists for
developers.

**The package carries its own Contracts.** Each operation's Contracts (the
declared shapes of its input, output and refusals) travel in the package's
registration, so it installs on an instance with no kit. Each operation serves
the base kit capability of the same name (`search.web` serves `web.search`);
whether it fits a Procedure's slot is checked when a Line binds it.

## The base install is light

The base distribution carries the adapter logic, the registration, an HTTP
client, an extractor, and the recorded exchanges the conformance suite
replays. `uv run pytest` therefore downloads no browser and
opens no socket.

A **browser** is the one heavy engine here, and it lives behind the `browser`
extra. The `web.render.dom` manifest row declares that extra in
`requires_extras`, which is what makes the resolver materialize an environment
containing it — and what makes that environment different from the base one
`web.fetch`, `web.fetch.pages`, `search.web` and `web.render` bind. One lock, two environments, two pins per marker
environment in the accepted artifact (for example `linux-cp311` and
`linux-cp311+browser`).

Trafilatura is base rather than an extra on purpose. The split exists to keep a
browser, a tensor stack, and an OCR runtime out of the base install; a
pure-Python extractor over an lxml wheel is none of those, and putting it behind
an extra would buy a few megabytes at the price of a default lane that never runs
a real extraction.

### The browser environment pins

Resolved against the committed lock and the three launch marker environments in
[`ci/marker-environments.json`](https://github.com/cruxible-ai/cruxible-providers/blob/main/ci/marker-environments.json):

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
the environment those tags describe.
[`docs/packaging.md`](https://github.com/cruxible-ai/cruxible-providers/blob/main/docs/packaging.md)
carries the rule.

## A Capture is the payload, verbatim

Every operation here returns the shared
`playbill-provider-result-to-external-capture-v1` Capture wire, and its bytes are
the document itself, in whatever format it is — never a bundle around it:

- **`web.fetch`** — the body the origin sent, exactly. JSON, CSV, gzip, HTML or
  binary, it is committed as received; core reads it only through a declared
  `extract` under the CaptureKind's decoding, and never parses it at admission.
- **`web.fetch.pages`** — `web.fetch`'s request, sent as pages: one Capture per
  page (`captures: list<capture<*>>`, a list of Captures), each the body that page's origin
  sent, exactly. See *Paging* below.
- **`search.web`** — the instance's response body, exactly. No ranking or
  recency filter is derived here; the instance's own `time_range` parameter is
  part of the request.
- **`web.render.dom`** — a fetch through a browser: it navigates to the URL the
  run names, runs the page's scripts, pulls its subresources, and the serialized
  DOM is the Capture. Use it for pages that carry nothing until assembled.
- **`web.render`** — a **derive** (an operation that only computes from a Capture already taken): pure computation over the exact bytes
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
coordinate. `pages` is declared for an operation that assembles the pages of
one listing into one document; nothing in this plane assembles one.
`web.fetch.pages` does the opposite, one Capture per page, and marks each
member's coordinate with `page_index` and `page_count` instead.

`web.fetch.request@1` carries ordinary fields. `query` is a typed list of
parameters, `{name, values, join?}` (`web.fetch.query_param@1`), so a Procedure
can pass values it read from state rather than spelling them into a URL:
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

### Paging: `web.fetch.pages`

A watched set grows, and the URL its values build eventually outgrows what an
origin accepts; `web.fetch` declines it and names this operation.
`web.fetch.pages.request@1` is `web.fetch.request@1` plus `page_param`, the name
of the one `query` parameter whose values are split, and `max_url_chars`
(default and maximum 8192). The values are taken in the order given and packed
greedily: a page takes the next value while its built URL fits, and every other
parameter is repeated on each page. That is the fewest pages any in-order split
can make, and it is a function of the request alone, so the same request always
sends the same pages and every value is sent exactly once. A value that does not
fit a page on its own, a value named twice in the paged parameter, and a split
needing more than 64 pages decline. The paged parameter may have no values:
then there are no pages, no request is sent, and the result is a complete one
with an empty `captures` (every other parameter still needs at least one value).

The result, `web.fetch.pages.result@1`, is `captures`: one Capture per page, in
page order, each the exact body that page's origin sent. Each member's
coordinate is `web.fetch`'s for that exchange (`url` is the page's built URL,
`request_digest` covers it) plus `page_index` (0-based) and `page_count`. A page
that is refused or fails is left out of `captures` and listed in `failed_pages`
(`web.fetch.pages.failure@1`: index, count, URL, request digest, `refused` or
`failed`, the code or error kind, the message, the status when the origin
answered outside 2xx). The result's envelope then names `captures` in its
generic `incomplete_captures`: core marks the extract incomplete and absence
never retires on it (a missing page is never read as "this item is gone"). When no page succeeds there is nothing to
commit: one page returns exactly what `web.fetch` would, and
several are one `AllPagesFailed` error listing each.

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

`web.fetch` (and `web.fetch.pages`, page by page) declares the **experimental**
`dynamic:target-from-run-input` form.
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

`search.web`'s target is configuration rather than input. The instance is named
per run by an `instance_url` coordinate; a run without one declines. The shipped
manifest declares `dynamic:target-from-configuration`, so any configured
instance is admitted and its egress recorded. A manifest that lists concrete
origins instead refuses an instance outside them (`undeclared_instance`).

## Private addresses

`web.fetch`, `web.fetch.pages` and `web.render.dom` reach whatever URL a run
names, so they refuse — with `private_or_internal_address`, a declared refusal
of each of the three — any target that reaches:

- loopback (`127.0.0.0/8`, `::1`), private networks (`10/8`, `172.16/12`,
  `192.168/16`, IPv6 `fc00::/7` and site-local `fec0::/10`) or link-local
  addresses (`169.254/16`, where cloud metadata services live, and `fe80::/10`);
- unspecified, multicast, carrier-grade NAT (`100.64/10`), reserved or
  documentation ranges, or anything else that is not globally routable;
- any of those written as an IPv4-mapped, IPv4-compatible, NAT64, 6to4 or Teredo
  IPv6 address;
- a `localhost`-style name, or a name under a local-only suffix (`.localhost`,
  `.local`, `.internal`, `.home.arpa`), whatever it resolves to.

A literal IP host is judged as written, including shorthand spellings such as
`127.1` or `2130706433`. A name is resolved, and if **any** address in the
answer is blocked, the target is refused. A name that does not resolve is a
connection failure, as it always was, not a refusal.

The refusal says why and where to go instead. Its detail is the origin only (the
path and query of the refused URL never enter it), the host, the blocked address
(absent when a name was refused as local-only), and the class it fell in
(`loopback`, `private`, `link_local`, `unspecified`, `multicast`,
`carrier_grade_nat`, `reserved`, `local_name`, or `ipv4_embedded_<class>` for an
IPv6 form carrying a blocked IPv4 address):

```json
{
  "code": "private_or_internal_address",
  "message": "the target is a private or internal address, which web retrieval does not reach; an internal source needs a provider whose manifest declares that endpoint, so the operator accepts it through governance rather than a run naming it",
  "detail": {
    "url": "https://wiki.corp.example",
    "host": "wiki.corp.example",
    "address": "10.0.0.9",
    "address_class": "private"
  }
}
```

What each operation checks:

| Operation | What is vetted before anything is sent | Pinned |
|---|---|---|
| `web.fetch` | The URL, and every redirect hop before it is followed. | Yes. The connection opens only to an address that was checked, while `Host`, TLS SNI and certificate verification keep the name, so a name re-pointed after the check (DNS rebinding) is never consulted for that hop. The client also ignores `HTTP_PROXY` / `HTTPS_PROXY`: a proxy would resolve the name itself. |
| `web.fetch.pages` | Each page's URL, and each of its redirect hops. A refused page is that page's refusal in `failed_pages`; when there is one page, it is the call's refusal. | Yes, as `web.fetch`, one client per page. |
| `web.render.dom` | The URL the run names, before the browser navigates. After navigation, the address the browser reports for the main-frame response is judged before the page is captured; a page served from a private address (after a redirect, or a re-pointed name) is refused, and one whose address the browser does not report is declined (`provider_declined`). That response is the one the captured DOM belongs to: a page whose main frame moves on to another document, before or while it is read, is declined (`provider_declined`) rather than captured under the first document's URL, status and validators. | No. A browser opens its own sockets and resolves names itself. Redirect hops before the final response, the subresources, fetches and WebSockets a page opens, and a navigation a script starts after the page has loaded are sent by the browser unchecked; they are recorded, not vetted. Containing them takes the network the browser runs in. |

The guard always applies; there is no opt-out. `search.web` is deliberately
**not** guarded: its SearXNG instance is an endpoint the operator configured,
and it is often a local one. A packaged recording (`fixture.invalid`) needs no
guard because it opens no socket.

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

`registration.json` (schema 3) declares all five operations (runtime protocol
2.0). Each says what it does and where its code is, names the base kit
capability it serves, names its input and output Contracts by `name@N`, and
declares the typed refusals it may return, each with a detail Contract:

| Operation | Serves | In | Out | Effect | Refusals |
|---|---|---|---|---|---|
| `search.web` | `web.search` | `search.web.request@1` | `search.web.result@1` | `external_read` | `provider_declined` (absorbable), `redirect_limit`, `unsupported_redirect_scheme`, `cross_origin_credentialed_redirect`, `undeclared_instance` |
| `web.fetch` | `web.fetch` | `web.fetch.request@1` | `web.fetch.result@1` | `external_read` | `provider_declined` (absorbable), `redirect_limit`, `unsupported_redirect_scheme`, `cross_origin_credentialed_redirect`, `credential_unavailable`, `private_or_internal_address` |
| `web.fetch.pages` | `web.fetch.pages` | `web.fetch.pages.request@1` | `web.fetch.pages.result@1` | `external_read` | `provider_declined` (absorbable), `redirect_limit`, `unsupported_redirect_scheme`, `cross_origin_credentialed_redirect`, `credential_unavailable`, `private_or_internal_address` |
| `web.render` | `web.render` | `web.render.request@1` | `web.render.result@1` | `pure` | `provider_declined` (absorbable) |
| `web.render.dom` | `web.render.dom` | `web.render.dom.request@1` | `web.render.dom.result@1` | `external_read` | `provider_declined` (absorbable), `engine_unavailable`, `private_or_internal_address` |

The registration carries a copy of all thirty-one, and of the seven nested
Contracts they pin (`web.capture.coordinate@1` with `web.capture.headers@1` and
`web.capture.page@1`, the zero-field `web.capture.selector@1`,
`web.capture.source_effective_time@1`, `web.fetch.query_param@1`, and
`web.fetch.pages.failure@1`), in its `declares` block. An install reads them
from the package; no kit and no accepted instance Contract is needed. The
results are still today's wire shape (the whole result is Capture wire); they
become a `capture` field per Capture before release, and then they fit the base
kit's capabilities.
[`scripts/render_web_contracts.py`](https://github.com/cruxible-ai/cruxible-providers/blob/main/scripts/render_web_contracts.py)
renders them from definitions derived from the
adapters' code, with Core's Contract model, and writes them into the
registration. Optional fields are absent keys,
never `null`: the shared result omits `source_effective_time` when the adapter
states no validity window, and a refusal detail omits what it cannot name.

A credential the run names but did not deliver (`credential_unavailable`), an
instance outside the declaration (`undeclared_instance`), a missing browser
(`engine_unavailable`) and a private target (`private_or_internal_address`, not
absorbable) are the operations' own declared refusals. Runtime codes
come from the runtime's machinery only; provider code never raises one.

Weight is declared by the caller and *checked* rather than trusted: a response
heavier than the run's `max_bytes` refuses.

[`tests/test_web_capture_conformance.py`](https://github.com/cruxible-ai/cruxible-providers/blob/main/packages/cruxible-provider-web/tests/test_web_capture_conformance.py)
holds the payload rule for every
Capture-producing operation the registration declares, in every form Core reads
(the whole result, a `capture<K>` field, each member of a `list<capture<K>>`
field): against every recorded exchange, each member's stored bytes are the
origin bytes (digest equality), carry no wrapper keys and have their own
coordinate, and a derived document's coordinate cites the bytes it read. The
repository's
[`tests/test_pure_operations.py`](https://github.com/cruxible-ai/cruxible-providers/blob/main/tests/test_pure_operations.py)
runs `web.render` (like every
`effect: pure` operation) twice with the network blocked and requires
byte-identical output.

## Running the tests

```sh
uv run pytest packages/cruxible-provider-web/tests -q     # default: no engines
uv run pytest -m engine packages/cruxible-provider-web    # needs the browser extra
```

The engine lane drives a real browser over a page served from a loopback server
(through a guard that allows that loopback server and nothing else, plus one
test that the production guard refuses it). It is opt-in, it is not
in the default CI lane, and every test in it skips with a reason when the engine
is absent, so collecting it on a machine with no engines still works.
