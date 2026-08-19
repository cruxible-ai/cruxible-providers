# cruxible-provider-noop

The reference no-op Cruxible provider. Apache-2.0.

It implements one stub interface, `noop.echo`, and does nothing useful on
purpose. Its job is to be the smallest package that exercises every rule the
provider protocol imposes, so that the conformance suite here is the suite every
plane package inherits.

## The modes

The input's `mode` field selects a branch:

| Mode | What it exercises |
|---|---|
| `echo` | the success path |
| `refuse` | a typed provider refusal |
| `error` | a crash, reported as a typed error rather than escaping |
| `credential` | a credential delivered by secret-ref over the inherited descriptor |
| `leak` | a provider *trying* to write its credential into output, trace, and stderr |
| `egress` | recording an endpoint the manifest does not declare |
| `slow` | the wall-clock budget breach |
| `loud` | the output-size budget breach |

`leak` is the interesting one. Redaction happens in the harness on the way out,
not in the provider's good manners, so a provider that tries to leak still
cannot — which is the only version of that property worth testing.

## The stub interface

`noop.echo` is a stub with a literal interface digest, not a value recomputed at
import time: an identity that recomputes itself is an identity that can drift
silently. A test asserts the literal still matches its preimage.

Its bucket vocabulary has two dimensions, `payload_size` and `charset`. The
manifest deliberately leaves `payload_size=large` **unclaimed**, so that an
oversized input exercises the `unclaimed_bucket` refusal at admission.

## Both backend kinds

Every loop test runs on both `local_env` and `container`.

- `local_env` runs a real child process through the ordinary child harness, with
  a real pipe carrying the credential.
- `container` runs at protocol level against a fake driver. **No test requires
  Docker.** `container/Dockerfile` and `container/provenance.md` are the
  buildable spelling of the same contract, and the executor's
  provenance-mismatch refusal is exercised against the fake.
