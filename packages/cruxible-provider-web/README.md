# cruxible-provider-web

Web retrieval and web search providers for [Cruxible](https://github.com/cruxible-ai/cruxible).
Apache-2.0.

| Interface | Entrypoint | What it does |
|---|---|---|
| `web.fetch` | `cruxible_provider_web.fetch:WebFetch` | Retrieves one URL the run names and, for HTML, extracts its main content as Markdown |
| `search.web` | `cruxible_provider_web.search:SearxngSearch` | Queries a SearXNG instance the operator configured |

A provider is an adapter Cruxible runs out of process, on an exact, governed
pin. It returns a typed result plus a trace; it never grades its own output.
That is the job of the CaptureContract the result is carried to.

## Installing

Operators install the package into a Cruxible daemon through a governed install:

```sh
cruxible provider install cruxible-provider-web
```

The install fetches the wheel, checks it against the index's hash, materializes
an isolated environment from the lock embedded in the wheel — so it resolves
exactly what this release was tested with — and proposes the Provider
registration through Cruxible's ordinary change-set governance. Every run is
bound to the exact accepted distribution, lock and manifest.

`pip install cruxible-provider-web` works for development and testing; it is not
how a daemon runs providers.

### No extras

The install is light and has no extras: the adapters, the interface schemas, the
input classifiers, an HTTP client ([httpx](https://www.python-httpx.org/)), a
main-content extractor ([trafilatura](https://trafilatura.readthedocs.io/)), and
the recorded exchanges the conformance fixtures replay. Both implementations
bind the same environment.

## `web.fetch`

**Input.** The field set is closed.

| Field | Default | Meaning |
|---|---|---|
| `url` | — | The `http` or `https` URL to retrieve |
| `render` | `false` | Must be `false`: this implementation does not render pages (see below) |
| `extract` | `true` | Extract main content from HTML as Markdown; structured formats are carried verbatim |
| `expected_format` | `auto` | `auto`, `html`, `json`, `csv`, `text` or `bytes`. A mismatch with what arrives is an error; JSON and CSV are validated |
| `max_bytes` | 256 KiB | Response size cap, at most 32 MiB, enforced while the body streams |
| `credential_ref` | — | A credential the run was granted, delivered by Cruxible — never the secret itself |
| `credential_header` | `authorization` | The header the credential travels on |
| `logical_source` | `web.response` | The dataset name to pin in the CaptureContract |
| `paced` | `false` | Marks the target as rate-limited, for input classification |

**Output.** Cruxible's shared external-capture envelope, whose content is a
canonical JSON bundle in two halves that a CaptureContract grades separately:

- **`retrieved`** — what came off the wire: the requested and final URL, the
  status, selected headers, and the exact body the origin sent (base64, byte
  count, sha256), after any redirects. `renderer` is always `null`.
- **`derived`** — what was made of it: the extracted Markdown and metadata, or
  the verbatim structured body.

A non-2xx final status is a failed retrieval.

**Input buckets.** Static pages up to the medium weight class and structured
(JSON, CSV) endpoints are claimed. Binary payloads, heavy HTML pages and every
rendered (`js_rendered`) page are not, so such inputs refuse at admission
(`unclaimed_bucket`) before any provider code runs.

**No rendering.** `web.fetch` does not run a browser. The interface keeps its
`render` field, and a run that sets it classifies into the `js_rendered` bucket,
which this package does not claim: it is refused at admission, and the adapter
refuses it as well if invoked directly. Pages that are assembled by JavaScript
are out of scope for this release. The declared weight is checked, not
trusted: a response heavier than the bucket the run was admitted under refuses.

## `search.web`

Queries a [SearXNG](https://docs.searxng.org/) instance that has the JSON output
format enabled.

| Input | Default | Meaning |
|---|---|---|
| `query` | — | The search query |
| `limit` | 10 | How many results to return |
| `max_age_hours` | — | Drop results older than this, measured from `as_of` |
| `language` | `en` | The SearXNG language |

The instance is configuration, not input: the `instance_url` configuration field
names it, and an optional `as_of` fixes the instant recency is evaluated at, so
the filtering can be reproduced from the receipt. An instance credential, if one
is granted, arrives as the `search.web.instance_credential` secret and is sent as
the `Authorization` header. The output keeps the exact instance response
separate from the normalized, recency-filtered ranking derived from it.

## Security

### `web.fetch` refuses private-network targets

`web.fetch` retrieves whatever URL a run names, so it refuses — with
`provider_declined`, before anything is sent — any target that reaches:

- loopback (`127.0.0.0/8`, `::1`), private networks (`10/8`, `172.16/12`,
  `192.168/16`, IPv6 `fc00::/7`) or link-local addresses (`169.254/16`, where
  cloud metadata services live, and `fe80::/10`);
- unspecified, multicast, carrier-grade NAT (`100.64/10`), reserved or
  documentation ranges, or anything else that is not globally routable;
- any of those written as an IPv4-mapped, IPv4-compatible, NAT64, 6to4 or Teredo
  IPv6 address;
- a `localhost`-style name, or a name under a local-only suffix (`.localhost`,
  `.local`, `.internal`, `.home.arpa`), whatever it resolves to.

A literal IP host is judged as written, including shorthand spellings such as
`127.1` or `2130706433`. A name is resolved, and if **any** address in the
answer is blocked, the target is refused.

The check applies to every hop: the first request and every redirect.

The refusal says why and where to go instead:

```json
{
  "code": "provider_declined",
  "message": "web.fetch does not retrieve private or internal addresses; internal sources need a provider with a declared endpoint",
  "detail": {
    "reason": "private_or_internal_address",
    "remedy": "web.fetch retrieves public web resources only. Reach an internal or local source through a provider whose manifest declares that endpoint, so the operator accepts the endpoint through governance rather than a run naming it.",
    "url": "https://wiki.corp.example",
    "host": "wiki.corp.example",
    "address": "10.0.0.9",
    "address_class": "private"
  }
}
```

`address_class` is one of `loopback`, `private`, `link_local`, `unspecified`,
`multicast`, `carrier_grade_nat`, `reserved`, `local_name` (refused by name, so
`address` is `null`), or `ipv4_embedded_<class>` for an IPv6 form carrying a
blocked IPv4 address. `url` is the origin only; the path and query of the
refused URL never enter the refusal.

**DNS rebinding is closed by pinning.** The addresses that passed the check are
the only ones the connection may open to: the client's network layer connects to
the vetted address, while the `Host` header, TLS SNI and certificate
verification keep using the name. A resolver that answers differently a moment
later is never consulted for that hop; each new hop is resolved and checked
afresh.

The guard always applies; there is no opt-out. Two consequences:

- `web.fetch` does not use `HTTP_PROXY` / `HTTPS_PROXY`. A proxy resolves names
  itself, which would hand the address decision to somebody else.
- `search.web` is deliberately **not** guarded: its SearXNG instance is an
  endpoint the operator configured, and it is often a local one.

### Credentials and redirects

A run can name the header its credential travels on, so the client follows
redirects by hand rather than letting the HTTP library forward every header. An
authenticated fetch redirected to a **different origin** refuses
(`cross_origin_credentialed_redirect`) rather than either handing the credential
to a host the run never named or continuing anonymously under terms the receipt
no longer describes. An `http` → `https` upgrade of the same host is followed.

### Egress recording

`web.fetch` declares the `dynamic:target-from-run-input` endpoint form: its
target *is* the run input, so an endpoint list fixed at acceptance time could
only be wrong. What governs instead is the recording. Every request the client
sends, redirect hops included, lands in the run's egress record, and nothing
that was refused does. `search.web` declares
`dynamic:target-from-configuration`, and its instance is recorded the same way.

Recording is not containment. A provider in the local backend runs with the
operator's privileges; containment comes only from the cloud container backend's
default-deny network policy.

## Recorded fixtures

Every claimed input bucket has a conformance fixture, replayed from a recording
shipped in this distribution. Every recording targets the reserved host
`fixture.invalid` (RFC 2606: it can never resolve), so a recording can never
stand in for a resource a caller actually asked for, and a run served from one
says so in `retrieved.source`. Recordings are replayed through the real client —
only the socket is replaced — which is how the package proves its own fixtures
after installation, with no network.

## Development

From a checkout of
[cruxible-providers](https://github.com/cruxible-ai/cruxible-providers):

```sh
uv run pytest packages/cruxible-provider-web -q   # no network
```
