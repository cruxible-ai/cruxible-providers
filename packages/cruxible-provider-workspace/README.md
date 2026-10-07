# cruxible-provider-workspace

The `workspace.file` source adapter for [Cruxible](https://github.com/cruxible-ai/cruxible).
Apache-2.0.

| Interface | Entrypoint | Effect |
|---|---|---|
| `workspace.file` | `cruxible_provider_workspace.file:WorkspaceFile` | pure |

Cruxible core reads a file from an attached workspace. This package turns the
bytes it was handed into a structured capture body, and does nothing else.

## Installing

`workspace.file` is a Cruxible built-in. Core seeds its Provider into an
instance through an ordinary governed proposal that pins this distribution's
exact wheel and lock, and materializes an isolated environment from them; you do
not normally install it by hand. `pip install cruxible-provider-workspace` is for
development and testing.

The package depends on `cruxible-provider-runtime` and nothing else, and has no
extras.

## Where the boundary is

Everything with authority stays on core's side of the process boundary: the
workspace binding and its allowed roots, the path grammar (workspace-relative,
normalized POSIX, no absolute paths, no `..`), the symlink walk that refuses any
escaping link, the `O_NOFOLLOW` open, the containment re-check on the resolved
path, the size cap resolved at admission, and the read receipt that attests the
read was authorized. Host paths never enter the ledger.

What crosses into this adapter is the **outcome** of that read:

| Field | Meaning |
|---|---|
| `logical_source` | The logical source the read was resolved for; opaque here |
| `commitment_digest` | The digest of the request commitment core bound the read to before the adapter started; echoed |
| `content_encoding` | Always `base64` (RFC 4648 §4, padded, no line breaks) |
| `bytes` | The payload |
| `byte_length` | The declared length of the decoded payload; **checked** |
| `bytes_digest` | The declared `sha256:` of the decoded payload; **checked, then echoed** |

Every declaration is verified against the decoded bytes rather than trusted. A
length that disagrees refuses (`mismatched_lengths`), a digest that disagrees
refuses (`provider_declined`), and a field that is missing, malformed or
undeclared refuses (`invalid_parameter`). Only a digest that agrees is echoed
into the body, so a reader of the Capture gets the digest core's read receipt
names, re-verified by the process that structured the bytes.

## Pure

The adapter opens no file, contacts no endpoint, reads no clock and consults no
secret. Its output is a function of its input alone, so the same input replays
to the same body under a later evaluation. The manifest says so the only way a
manifest can — `declared_endpoints: []`, `deterministic: true`,
`side_effects: false` — and the conformance suite checks it three ways:
structurally (the adapter module's imports are an allowlist with no filesystem or
socket module on it), at runtime (`open` raises for the duration of an
invocation), and with outbound sockets blocked in both the executor and the
provider process.

## The body

Two shapes, selected by what the bytes are. The choice is the `content_kind`
input bucket, measured from the decoded bytes and never read from a declaration.

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

The text path never normalizes. A byte-order mark stays in `text` and is
reported in `bom`; the newline style is reported (`lf`, `crlf`, `cr`, `mixed`,
`none`) rather than rewritten. A line is what stands between two line feeds, with
one carriage return before the feed excluded; the empty tail after a trailing
feed is not a line. This is deliberately not `str.splitlines`, which also splits
on form feeds and Unicode separators that a line-numbered citation into a source
file does not expect.

Both `text` and `lines` ship. That doubles the body for text, on purpose: a claim
cites a line and a digest covers the text, and a consumer that reconstructs one
from the other is a consumer that can get it wrong.

**Bytes** — anything else:

```json
{
  "content": { "kind": "bytes", "encoding": "base64", "byte_length": 33, "bytes": "iVBOR…" }
}
```

## Input buckets

Two dimensions, both measured: `content_kind` (`text` or `binary`) and
`byte_size` (`tiny` ≤ 4 KiB, `small` ≤ 64 KiB, `medium` ≤ 1 MiB, `large`). The
manifest claims every `text` and `binary` class up to `medium` and leaves `large`
unclaimed, so a file above one mebibyte refuses at admission (`unclaimed_bucket`)
before any process starts.

Each claimed bucket has a fixture shipped in the package: the exact run input,
the bucket it measures into, and the digest of the body the adapter must
produce. The fixtures are generated from fixed rules, and a test asserts that
regenerating them is a byte-identical no-op.

## Backends

The manifest declares both backend kinds, `local_env` and `container`, and the
conformance suite runs every loop on both — the container path at protocol
level, without Docker.
In a hosted deployment, workspace reads themselves are refused by core, upstream
of this adapter.

## Identity

The implementation digest covers the interface id and digest, the entrypoint
`cruxible_provider_workspace.file:WorkspaceFile`, and the built wheel's sha256.
The materialization digests cover the package's lock, resolved for each
supported platform. Cruxible pins both; track record is keyed on the
implementation digest, which a change of backend does not move.

## Development

From a checkout of
[cruxible-providers](https://github.com/cruxible-ai/cruxible-providers):

```sh
uv run pytest packages/cruxible-provider-workspace -q
```
