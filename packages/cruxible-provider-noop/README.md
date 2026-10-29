# cruxible-provider-noop

The reference no-op Cruxible provider. Apache-2.0.

It serves one operation, `noop.echo`, and does nothing useful on purpose. Its job is to be the smallest package that exercises every rule the
provider protocol imposes, so that the conformance suite here is the suite every
plane package inherits.

## The modes

The input's `mode` field selects a branch:

| Mode | What it exercises |
|---|---|
| `echo` | the success path |
| `refuse` | a typed refusal the operation declares (`provider_declined`) |
| `undeclared_refusal` | a refusal code the operation does not declare; the executor refuses it (`undeclared_refusal`) |
| `runtime_code` | a runtime code returned as if it were the operation's; the harness reports `provider_protocol_violation` |
| `raise_runtime_code` | a runtime code *raised* by provider code; the harness reports `provider_protocol_violation` |
| `error` | a crash, reported as a typed error rather than escaping |
| `credential` | a credential delivered by secret-ref over the inherited descriptor; absent, the declared `credential_missing` |
| `leak` | a provider *trying* to write its credential into output, trace, and stderr |
| `egress` | recording an endpoint the manifest does not declare |
| `slow` | the wall-clock budget breach |
| `loud` | the output-size budget breach |

`leak` is the interesting one. Redaction happens in the harness on the way out,
not in the provider's good manners, so a provider that tries to leak still
cannot — which is the only version of that property worth testing.

## The operation

`registration.json` declares `noop.echo`: `noop.echo.request@1` in,
`noop.echo.result@1` out, effect `pure`, and two refusals, `provider_declined`
and `credential_missing`, each with its detail Contract (named `name@N`; the
install pins them). The entrypoint `cruxible_provider_noop.provider:NoopEcho`
declares `operations = ("noop.echo",)`, and the harness refuses a run naming any
other operation (`undeclared_operation`). The manifest has one row for that
entrypoint: both backends, zero declared endpoints, deterministic.

The implementation digest is `cruxible.provider.implementation.v2` over the
entrypoint and the distribution's sha256; the run's operation, its signature
digest and the build are recorded beside it.

## Both backend kinds

Every loop test runs on both `local_env` and `container`.

- `local_env` runs a real child process through the ordinary child harness, with
  a real pipe carrying the credential.
- `container` runs at protocol level against a fake driver. **No test requires
  Docker.** `container/Dockerfile` and `container/provenance.md` are the
  buildable spelling of the same contract, and the executor's
  provenance-mismatch refusal is exercised against the fake.
