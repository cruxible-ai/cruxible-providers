# cruxible-provider-docs

## In plain terms

This package turns documents you already have into text Cruxible can work
with. It is a **provider**, an add-on package that does one kind of work for
Cruxible, and each task it offers is an **operation**. It downloads nothing:
the document comes in with the request.

| Operation | What it does | Network | Needs |
|---|---|---|---|
| `doc.to_markdown` | You give it a document (for example a PDF, or plain text, Markdown or CSV). It returns the document's content as Markdown, with a note of what was converted (file name, type, size, hash). | no | the `docling` extra |
| `ocr.extract` | You give it page images. It returns the text read off each page, with the OCR engine's own per-page confidence where the engine reports one. | no | the `paddleocr` extra, and the engine's model weights, which the operator downloads |

What gets installed: the base package depends only on
`cruxible-provider-runtime` and carries no conversion engine. The extras add
the engines: `docling` adds Docling (document conversion) and `paddleocr` adds
PaddleOCR with PaddlePaddle (text recognition). Both are large. Plain text,
Markdown and CSV are converted without running Docling, but `doc.to_markdown`
still installs with the `docling` extra, because one operation gets one
environment.

The package names the **Contracts** its operations use (a Contract is the
declared shape of an input or output) but does not ship them; they must already
be accepted in Cruxible, for example from a kit, before the package installs.

You do not need this package if your documents are already text you can pass
on as they are.

## Reference

Cruxible provider adapters for the document plane (the document family of
operations). Apache-2.0.

| Operation | Entrypoint | Engine | Extra |
|---|---|---|---|
| `doc.to_markdown` | `cruxible_provider_docs.to_markdown:DoclingToMarkdown` | Docling; already-linear documents are converted by the adapter itself | `docling` |
| `ocr.extract` | `cruxible_provider_docs.ocr:PaddleOcrExtract` | PaddleOCR | `paddleocr` |

Each operation is declared in `registration.json` (schema 2): input
`<operation>.request@1`, output `<operation>.result@1`, effect `pure`, and two
declared refusals: `provider_declined` (absorbable) and `engine_unavailable`
(the environment lacks the engine's extra; not absorbable), each with detail
Contract `<operation>.refusal.<code>@1`. The Contracts are named,
not shipped; an install pins each to the version accepted at its base.

Operators install it into a daemon by name, through a governed install:
`cruxible provider install cruxible-provider-docs` (add `--extra docling` or
`--extra paddleocr` for the engine the operation needs). An unpublished version
installs from a checkout instead; see the repository README. The umbrella
`cruxible-providers[docs]` exists for developers.

## Nothing here is observed

The document arrives in the run input. This plane retrieves nothing, so there is
no observed-shaped material to be had and the output shape says so: a `document`
block identifying what was converted (filename, media type, byte count, sha256,
origin) and a `derived` block holding the conversion or the reading. Both
Contracts are derived. An OCR reading in particular is a
model's opinion about marks on a page, and the per-page number the engine reports
travels as *the engine's* confidence under the engine's name — this system has no
generic confidence score and this adapter does not invent one.

## The base install carries no engine

Docling drags a tensor stack; PaddleOCR drags PaddlePaddle. Both live behind
per-engine extras, each declared by the implementation that needs it, and the
default lane installs neither. What the base distribution carries is the adapter
logic, the packaged documents, and the recorded engine responses the
conformance suite replays.

`doc.to_markdown` also carries a genuinely engine-free path: an already-linear
document — text, Markdown, CSV — is converted by the adapter itself, because
layout analysis of a document with no layout would be a tensor stack solving
nothing. That path is what gives this plane a success case that runs end to end
through a real child process in the default lane.

**A cost worth naming.** An implementation binds *one* environment, so
`requires_extras` is the union of what its inputs need, and the engine-free
plain-text path rides in an environment sized for PDF conversion. Splitting them
would mean a second operation served by a second entrypoint with its own
environment — possible, and a separate declaration rather than an accident.

## Both engine environments pin

Against the three launch marker environments in `ci/marker-environments.json`:

| Extras | linux-cp311 | linux-cp312 | macos-arm-cp312 |
|---|---|---|---|
| *(base)* | resolves | resolves | resolves |
| `docling` | resolves | resolves | resolves |
| `paddleocr` | resolves | resolves | resolves |

`+paddleocr` refused on two of the three until the resolver stopped matching
declared tags by exact string membership: PaddlePaddle publishes
`cp311-cp311-manylinux1_x86_64`, which a `manylinux_2_28` environment installs
and a literal tag list does not name.

`+docling` resolves now because the declared launch floors are glibc 2.28 and
macOS 14.0, the floors its tensor stack requires. The re-baseline deliberately
re-keyed every package's materialization digest before any Provider artifact was
accepted; it did not change the document operations or their implementation
digests. The engine suite's shared `ENGINE_MARKER_ENVIRONMENT` now matches the
Linux launch floor rather than standing in for a future one.

**The base install remains resolvable** for all three environments, so the
default lane, the digest-scope gate and the closure report remain unimpaired —
and the engine-free plain-text path stays bindable.

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

A conformance run of an engine adapter needs an engine, and the base install
has none. The suite therefore replays recorded engine responses, under three
rules:

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

The page count is the caller's description of a document nobody has opened
yet, so the adapter checks it: a document that opens to a different page-count
class than the run declared refuses, and a conversion that recovers no text from
a document declared born-digital refuses rather than returning an empty Markdown
file that looks complete.

## Running the tests

```sh
uv run pytest packages/cruxible-provider-docs/tests -q     # default: no engines
uv run pytest -m engine packages/cruxible-provider-docs    # needs an engine extra
```

The engine lane is opt-in, is not in the default CI lane, and every test in it
skips with a reason when its engine is absent, so collecting it on a machine with
no engines still works.
