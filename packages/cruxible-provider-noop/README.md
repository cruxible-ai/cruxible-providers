# cruxible-provider-noop

## In plain terms

This is a test package. It is a **provider** (an add-on package that does one
kind of work for Cruxible) whose two **operations** (named tasks) do nothing
useful. They exist so the test suite can check every rule a provider must
follow, including how credentials, failures and limits are handled.

| Operation | What it does | Network | Needs |
|---|---|---|---|
| `noop.echo` | You give it some text and a `mode` (see *The modes* below). In `echo` mode it returns the text; every other mode misbehaves on purpose in the way its name says, so a test can check how Cruxible reacts. | no (the `connect` mode opens a socket on purpose, to check that the network guard catches it) | nothing extra |
| `noop.captures` | You name up to 16 of the sample files shipped in the package. It returns each one unchanged as its own **Capture** (the exact bytes, plus a record of where they came from). | no | nothing extra |

What gets installed: `cruxible-provider-runtime` and nothing else; no extras.
The package ships the 7 **Contract** files its operations use (a Contract is
the declared shape of an input or output).

You do not need this package unless you are testing Cruxible or a provider.

## Reference

The reference no-op Cruxible provider. Apache-2.0.

It serves two operations, `noop.echo` and `noop.captures`, and does nothing
useful on purpose. Its job is to be the smallest package that exercises every rule the
provider protocol imposes, so that the conformance suite here is the suite every
plane package inherits.

## The modes

`noop.echo`'s input `mode` field selects a branch:

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
| `chatty` | engine-style printing to stdout; the harness keeps stdout for the envelope, so the run still succeeds |
| `context_repr` | the run context's own repr, which must not carry credentials |
| `egress` | recording an endpoint the manifest does not declare |
| `connect` | opening a real socket, so the egress-conformance guard has something to stop |
| `slow` | the wall-clock budget breach |
| `loud` | the output-size budget breach |

`leak` is the interesting one. Redaction happens in the harness on the way out,
not in the provider's good manners, so a provider that tries to leak still
cannot — which is the only version of that property worth testing.

## The operations

`registration.json` (schema 3) declares `noop.echo`, serving the capability
`noop.echo`: `noop.echo.request@1` in, `noop.echo.result@1` out, effect `pure`,
and two refusals, `provider_declined` and `credential_missing`, each with its
detail Contract (named `name@N`). The entrypoint `cruxible_provider_noop.provider:NoopEcho`
declares `operations = ("noop.echo",)`, and the harness refuses a run naming any
other operation (`undeclared_operation`).

It also declares `noop.captures` (serving `noop.captures`; `noop.captures.request@1` in,
`noop.captures.result@1` out, effect `pure`, one refusal, `provider_declined`),
served by `cruxible_provider_noop.captures:NoopCaptures`. It is the reference
for the list-of-Captures form: the run names up to 16 of the files shipped in
`recordings/`, and each comes back verbatim as its own member of the `captures`
field, with its own coordinate.

The manifest has one row per entrypoint: both backends, zero declared
endpoints, deterministic. The registration carries a copy of each of its seven
Contracts in `declares`, written by `scripts/render_noop_contracts.py`.

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
