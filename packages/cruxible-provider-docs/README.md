# cruxible-provider-docs

Cruxible provider adapters for the document plane. Apache-2.0.

| Interface | Entrypoint | Engine | Extra |
|---|---|---|---|
| `doc.to_markdown` | `cruxible_provider_docs.to_markdown:DoclingToMarkdown` | Docling; already-linear documents are converted by the adapter itself | `docling` |
| `ocr.extract` | `cruxible_provider_docs.ocr:PaddleOcrExtract` | PaddleOCR | `paddleocr` |

End users do not install this package: providers are fetched on bind. The
umbrella `cruxible-providers[docs]` exists for developers.

## Nothing here is observed

The document arrives in the run input. This plane retrieves nothing, so there is
no observed-shaped material to be had and the output shape says so: a `document`
block identifying what was converted (filename, media type, byte count, sha256,
origin) and a `derived` block holding the conversion or the reading. Both
CaptureContract families are derived families. An OCR reading in particular is a
model's opinion about marks on a page, and the per-page number the engine reports
travels as *the engine's* confidence under the engine's name — this system has no
generic confidence score and this adapter does not invent one.

## The base install carries no engine

Docling drags a tensor stack; PaddleOCR drags PaddlePaddle. Both live behind
per-engine extras, each declared by the implementation that needs it, and the
default lane installs neither. What the base distribution carries is the adapter
logic, the schemas, the bucket classifiers, the packaged documents, and the
recorded engine responses the conformance fixtures replay.

`doc.to_markdown` also carries a genuinely engine-free path: an already-linear
document — text, Markdown, CSV — is converted by the adapter itself, because
layout analysis of a document with no layout would be a tensor stack solving
nothing. That path is what gives this plane a success case that runs end to end
through a real child process in the default lane.

**A cost worth naming.** An implementation binds *one* environment, so
`requires_extras` is the union of what its claimed buckets need, and the
engine-free plain-text bucket rides in an environment sized for the PDF bucket.
Splitting them would mean two implementations of `doc.to_markdown`, which is a
terminal `ambiguous_implementation` refusal — so this is a real cost of the
one-implementation-per-interface rule, not an oversight.

## Neither engine environment can be pinned yet

The extras mechanism is implemented and exercised by this package's tests, but
**neither `+docling` nor `+paddleocr` can be pinned in an accepted artifact
today**, against the three launch marker environments in
`ci/marker-environments.json`:

| Extras | linux-cp311 | linux-cp312 | macos-arm-cp312 |
|---|---|---|---|
| *(base)* | resolves | resolves | resolves |
| `docling` | refuses | refuses | refuses |
| `paddleocr` | refuses | refuses | resolves |

Every refusal is `no_compatible_artifact`, naming `torchvision` or
`paddlepaddle`. `paddleocr` resolving on one environment out of three is not
something to rely on: an artifact needs a pin for the environment a deployment
actually binds, and both Linux environments refuse.

The cause is the tag vocabulary rather than this package. Tag matching is exact
string membership; the platform-tag scheme it matches is an ordering, and PEP 600
says a `manylinux_2_17` wheel installs on a `manylinux_2_28` host. Three literal
tags cannot cover the dozen a tensor stack reaches for. Fixing that changes what a
materialization digest *means*, so it is a separately-owned follow-up rather than
something this package worked around; the conformance suite uses the runtime's
`ENGINE_MARKER_ENVIRONMENT`, which enumerates the tags instead.

**The base install is unaffected**: it resolves for all three environments, so the
default lane, the digest-scope gate and the closure report are unimpaired — and
the engine-free plain-text path is bindable today.

## Why PaddleOCR rather than Surya

Licensing decides it before quality does. PaddleOCR is Apache-2.0, which an
Apache-2.0 distribution can name as an optional dependency without qualification.
Surya is GPL-3.0-or-later with a separate commercial grant whose terms depend on
the adopter's revenue, and putting a licence question between an adopter and a
provider they are about to bind is a cost with no upside at this stage.
PaddleOCR also publishes CPU wheels for every platform the launch marker
environments name. Neither engine is vendored here; model weights are downloaded
by the operator, into the operator's environment, under the engine's own terms.

## Replay, and what keeps it honest

A conformance fixture for an engine adapter needs an engine, and the base install
has none. Fixtures therefore replay recorded engine responses, under three rules:

1. **A recorded response is served only for a packaged document.** The input's
   `source.kind` is either `inline` — a caller's document, which only a real
   engine ever converts — or `packaged_fixture`, which can name nothing but a
   document shipped inside this distribution.
2. **The replay is labelled.** The engine reported is `recorded:<id>`, never
   `docling` or `paddleocr`, and the trace carries an event saying no engine ran.
3. **The recording is checked.** Each recording names the engine it is recorded
   for and how it was obtained; the `engine`-marked lane runs that engine over
   the same shipped bytes and asserts it reproduces the recording. A recording
   that has drifted fails there, which is what the lane is for.

## Declarations are checked, not trusted

Page count, scan quality, and layout are the caller's description of a document
nobody has opened yet, so the classifier reads them from the input and the
adapter then checks them: a document that opens to a different page-count class
than the bucket the run was admitted into refuses, and a conversion that recovers
no text from a document declared born-digital refuses rather than returning an
empty Markdown file that looks complete.

## Running the tests

```sh
uv run pytest packages/cruxible-provider-docs/tests -q     # default: no engines
uv run pytest -m engine packages/cruxible-provider-docs    # needs an engine extra
```

The engine lane is opt-in, is not in the default CI lane, and every test in it
skips with a reason when its engine is absent, so collecting it on a machine with
no engines still works.
