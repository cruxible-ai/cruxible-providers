# cruxible-provider-workspace

The `workspace.file` built-in Source adapter. Apache-2.0.

Core reads the file. This package structures the bytes it was handed into a
capture body, and does nothing else.

## Where the boundary is

The G5 law puts everything with authority on core's side of the process boundary:
the attached workspace is the binding, the allowed roots widen it (daemon-local,
never in governed state), the path grammar is workspace-relative normalized POSIX
with no absolute paths and no `..`, the symlink walk refuses any escaping link,
the final open is `O_NOFOLLOW`, containment is re-checked on the resolved path,
the size cap is policy-carried and resolved at admission, and a
`source-read-receipt` attests that the read was authorized. Host paths never
enter the ledger.

What crosses into this adapter is the **outcome** of that read:

| Field | Meaning |
|---|---|
| `logical_source` | the logical source id the read was resolved for; opaque here |
| `commitment_digest` | the G4 derived-request commitment the read was bound to before spawn; echoed |
| `content_encoding` | always `base64` (RFC 4648 §4, padded, no line breaks) |
| `bytes` | the payload |
| `byte_length` | the declared length of the decoded payload; **checked** |
| `bytes_digest` | the declared `sha256:` of the decoded payload; **checked, then echoed** |

It serves one operation, `workspace.file`: `workspace.file.request@1` in,
`workspace.file.result@1` out, effect `pure`, with three declared refusals —
the same signature and refusal Contracts core seeds for its built-in port of
this adapter.

Every declaration is verified against the decoded bytes rather than trusted. A
length that disagrees refuses (`mismatched_lengths`); a digest that disagrees
refuses (`provider_declined`); a field that is missing, malformed, or undeclared
refuses (`invalid_parameter`); a payload over one mebibyte refuses
(`provider_declined`). Only a digest that agrees is echoed into the body
as the source digest, so a reader of the Capture gets the digest core's receipt
names, re-verified by the process that structured the bytes.

## Pure (RAT-9)

The adapter opens no file, contacts no endpoint, reads no clock, and consults no
secret. Its output is a function of its input alone, which is what lets the same
input replay to the same body under a later evaluation. The manifest spells the
same fact the only way a manifest can: `declared_endpoints: []` and
`deterministic: true`; the operation's effect is `pure`. The conformance suite asserts it
three ways — structurally (the adapter module's imports are an allowlist with no
filesystem or socket module on it), at runtime (`open` is replaced with something
that raises for the duration of an invocation), and in the egress-conformance
lane (outbound sockets blocked in both processes, declared == observed == empty).

Time-bearing fields: none. The body carries no clock.

## The body

Two shapes, selected by what the bytes are, measured from the decoded bytes and
never read from a declaration. The result's `input_bucket` names that
measurement (`content_kind` and `byte_size`); nothing classifies the input
before the run.

**Text** — strict UTF-8 with no NUL byte (an empty file is text):

```json
{
  "input_bucket": "content_kind=text;byte_size=tiny",
  "source": {
    "logical_source": "…", "commitment_digest": "sha256:…",
    "bytes_digest": "sha256:…", "byte_length": 71
  },
  "content": {
    "kind": "text", "encoding": "utf-8",
    "bom": false, "newline": "lf", "trailing_newline": true,
    "line_count": 3, "character_count": 70,
    "text": "…", "lines": ["…", "…", "…"]
  }
}
```

The text path never normalises. The byte-order mark stays in `text` and is
reported in `bom`; the newline style is reported (`lf`, `crlf`, `cr`, `mixed`,
`none`) rather than rewritten. A line is what stands between two line feeds,
with one carriage return before the feed excluded; the empty tail after a
trailing feed is not a line. This is deliberately not `str.splitlines`, which
also splits on form feeds and the Unicode separators that a line-numbered
citation into a source file does not expect.

`text` and `lines` both ship. That doubles the body's size for text, and it is
the trade the operation makes on purpose: a claim cites a line, a digest covers
the text, and a consumer that reconstructs one from the other is a consumer that
can get it wrong.

**Bytes** — anything else:

```json
{
  "content": { "kind": "bytes", "encoding": "base64", "byte_length": 33, "bytes": "iVBOR…" }
}
```

## Size

Classes for the result's `input_bucket` label: `tiny` ≤ 4 KiB, `small` ≤ 64 KiB,
`medium` ≤ 1 MiB, `large` above. The adapter structures at most one mebibyte
and refuses anything larger (`provider_declined`, with `byte_length` and
`max_bytes`) rather than structuring it into a body twice its size.

## The protocol

The adapter speaks provider runtime protocol 2.0.
`tests/test_workspace_protocol_contract.py` drives the child harness with the
run-context vector of the contract core vendors
(`packages/cruxible-provider-runtime/tests/fixtures/provider_runtime_contract_v2.json`)
re-pointed at this operation, and checks the envelopes it gets back against the
contract's result vectors — and that its protocol-1.x vector is refused.

## Both backend kinds

Every loop test runs on both `local_env` and `container`, with no Docker: the
container path runs at protocol level against the runtime's fake driver, and
`container/` is the buildable spelling of the same contract. On the Cloud
profile the *read* refuses typed under the no-mounts law; that refusal is core's,
upstream of this adapter, and a bundled-workspace upload is a follow-on.

## Identity

Three levels, as everywhere in this repository. The implementation digest covers
the entrypoint `cruxible_provider_workspace.file:WorkspaceFile` and the built
wheel's sha256;
the materialization digests cover the committed lock resolved for each marker
environment in `ci/marker-environments.json`. `scripts/seed_pins.py` prints the
table core's seed bundle pins.
