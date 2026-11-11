# cruxible-provider-quant

**Status: not yet released.** No kit uses this package yet and it ships no Contracts, so an instance cannot install it; it will be released when a kit uses it.

## In plain terms

This package does standard number work for Cruxible: totals and averages,
forecasts, spotting unusual values, statistical tests, ranking, matching
records that may describe the same thing, and checking how well past
predictions turned out. It is a **provider**, an add-on package that does one
kind of work for Cruxible, and each task it offers is an **operation**. Every
operation computes only from the data in the request: none of them uses the
network, a clock or a random number, and the same input always gives the same
answer.

| Operation | What it does | Network | Needs |
|---|---|---|---|
| `calc.reduce` | You give it a table of rows. It returns sums, means, minimums, maximums, counts, medians or standard deviations, for the whole table, per group, or over moving windows. | no | nothing extra |
| `ts.forecast` | You give it a time series, a model (ARIMA or ETS) and how many steps ahead. It returns the forecast with low and high bounds at the levels you ask for (for example 80% and 95%), and names the exact model it fitted. | no | nothing extra |
| `ts.anomaly` | You give it a time series and a method. It returns either the points that stand out from the series' usual pattern, with a score for each, or the positions where the series changes level, for a number of changes you set. | no | nothing extra |
| `stat.test` | You name a test (for example Student's t, Mann-Whitney U, Pearson correlation, Shapiro-Wilk) and give it the samples. It runs exactly that test and returns the statistic, the p-value, whether the result clears the threshold you set, and whether the test's assumptions look met. | no | nothing extra |
| `score.rank` | You give it candidates and either fixed weights for their scores or a saved scikit-learn model with its SHA-256 hash. It returns the candidates in order, with their scores and how ties were broken. | no | nothing extra; a saved model must be a file the operator trusts |
| `match.record` | You give it a list of records, how to compare their fields, and how likely each comparison outcome is for a true match and for a non-match. It returns pairs of records that may describe the same real thing, each with a match score. It never merges records; every result is marked for review. | no | nothing extra |
| `calc.calibrate` | You give it past predictions together with what actually happened. It returns how well the predicted probabilities matched the outcomes (Brier score and its parts, a per-bin breakdown, expected calibration error). | no | nothing extra |

What gets installed: the base package carries every engine, with no optional
extras: NumPy, pandas, Polars, ruptures, scikit-learn, SciPy, splink,
statsforecast and statsmodels. None of them needs a GPU.

The package names the **Contracts** its operations use (a Contract is the
declared shape of an input or output) but does not ship them; they must already
be accepted in Cruxible, for example from a kit, before the package installs.
When an operation cannot answer, it returns a **refusal**: a typed "no" with a
code saying why, such as a series too short for the model.

You do not need this package if nothing you run computes statistics,
forecasts, rankings or record matches.

## Reference

Cruxible provider adapters for the quantitative plane (the numeric family of
operations). Apache-2.0.

Seven classical baselines, one operation each. These are the signatures on
which narrow-ML implementations will later compete, so what matters here is not
sophistication but **contract fidelity**: the same input and output Contracts,
the same declared refusals, the same digests. A
baseline that is clever and slightly off-contract is worth less than a dull one
that is exactly on it, because only the second one makes the comparison mean
anything.

| Operation | Engine | What it does |
|---|---|---|
| `calc.reduce` | Polars | scalar, grouped, and windowed aggregation over a relation |
| `ts.anomaly` | statsmodels STL + ruptures | STL-residual scoring against a MAD scale estimate; binary-segmentation changepoints |
| `ts.forecast` | statsforecast | `AutoARIMA` / `AutoETS` with an explicit horizon and prediction intervals |
| `stat.test` | SciPy, statsmodels | runs **the declared test**, reports its assumptions |
| `score.rank` | deterministic weights, or a pinned scikit-learn model | orders candidates against a declared objective |
| `match.record` | splink (DuckDB, in process) | Fellegi-Sunter linkage scores with declared m/u parameters |
| `calc.calibrate` | NumPy | Brier score, its decomposition, reliability bins, ECE |

## Two standing laws, and where to check them

**No generic confidence score.** Not one output in this package carries a
`confidence`, a `certainty`, or any other single number claiming to summarise
how much to believe the answer. What they carry instead are named quantities
with their own definitions — prediction intervals at declared levels, a p-value
beside the test that produced it, a match weight against declared m/u, a Brier
score over stated bins, a modified z-score against a reported scale estimate.
Those quantities disagree with each other on purpose, and the disagreement is
the information a unified confidence would destroy. `tests/test_output_laws.py`
scans every fixture's output for the banned vocabulary.

**Grade is the CaptureContract's.** No output carries a `grade` field. A
forecast, an anomaly flag, a rank, and a linkage score are derived readings;
a test asserts it. A provider that graded its own output would be
self-certifying.

## Operations

`registration.json` (schema 2) declares each operation: its input and output
Contracts (`<operation>.request@1`, `<operation>.result@1`), its effect (all
seven are `pure`), the refusals it may return — each with a detail Contract
`<operation>.refusal.<code>@1` — and the entrypoint serving it. The Contracts
are named, not shipped: an install pins each to the version accepted at its
base. `tests/test_registration_stability.py` asserts that every operation
declares exactly the refusals its implementation can return, read from the
implementation's own source.

What each baseline deliberately does not model — large or out-of-core
relations, categorically-encoded series, intermittent demand, covariates,
dependence-modelling tests, trained linkage, rare-event calibration — is no
longer enforced by classifying the input against bucket claims; there is no
bucket in protocol 2.0. Where a bound matters it belongs in the operation's
input Contract, and a narrow-ML implementation of the same signature is a
different implementation with its own digest. Two of those inputs now reach the
implementation where they used to be refused at admission, and are ignored
there: a `value_kind` declaring categorical encoding on `ts.anomaly`, and
`known_matches` on `match.record`, which never trains. Closing them is the
input Contracts' job.

## Determinism and tolerances

Every engine here is deterministic given a fixed input, and no implementation
draws a random number. What varies is the last bits of a float, across BLAS
builds and platforms. The suite is explicit about which properties are asserted
exactly and which carry a tolerance:

| Asserted exactly | Asserted to a tolerance |
|---|---|
| flagged indices, changepoint positions, segment boundaries | STL residuals, MAD scale estimates (rtol 1e-9) |
| interval nesting, ordering, lengths, finiteness of forecasts | point forecasts and interval bounds (rtol 1e-6) |
| chosen test name, reject/retain at the declared alpha | test statistics and p-values (rtol 1e-9) |
| ranking order, tie sets, tie-break rule | weighted and model scores (rtol 1e-12) |
| comparison vectors, pair ordering, pair counts | match weights and probabilities (rtol 1e-9) |
| bin edges, bin counts | Brier score, decomposition, ECE (rtol 1e-12) |

`tests/test_determinism.py` also runs every fixture twice in one process and
asserts the outputs are identical, which is the property that actually matters
for identity: a run whose answer moves is a run whose track record means
nothing.

## The pinned-model boundary

`score.rank`'s `pinned_model` mode loads a serialised scikit-learn estimator.
The reference requires a `sha256`, the bytes are hashed before anything is
loaded, and a mismatch refuses. **That does not make loading it safe.**
Deserialisation executes code by design, so a pinned model is trusted code, and
the local backend is a dependency-isolation mechanism rather than a security
boundary — the model runs with the operator's privileges. What the pin buys is
that the trust decision is made once, at pinning time, over one specific
artifact, instead of implicitly on every run over whatever is at that path.
Containment for third-party models exists in the cloud container backend and
nowhere else. The `weighted` mode needs no deserialisation at all and is what
the fixtures use.

The two failures on this path are reported as two different things, because they
are two different things:

| Failure | Refusal |
|---|---|
| no `model_ref`, unsupported `kind`, missing path, absent or ill-formed pin, unnamed score scale, missing `feature_order`, **unreadable path** | `malformed_model_ref` — a request this implementation cannot serve |
| the file was read and its bytes do not hash to the pin | `model_digest_mismatch` — an **integrity** signal, declared by `score.rank` apart from its declines (not absorbable), countable on a track record apart from them |

A file that is not there has not been altered; folding a missing path into the
tampering signal would put one on a track record every time somebody mistyped a
path, and folding tampering into the declines would hide the only event here
worth an alarm.

## The stdout hazard, and where it is handled

A single line of engine chatter ahead of the result envelope turns a successful
run into `provider_protocol_violation`, and splink prints timing lines by
default. This package used to redirect file descriptor 1 around every engine
call itself. It no longer does: the child harness now reserves stdout for the
envelope before any provider code is imported and points fd 1 at stderr for the
whole run, so engine chatter lands in stderr — which the executor captures,
redacts, and measures against the output budget — without any plane package
remembering to arrange it.

## No number that is not a number

Every `ok` this package emits passes through `outputs.ok_if_finite`. Input
validation is not result validation, and the gap is where the useful failure
lives: two constant samples are perfectly well-formed input, and a t test over
them returns a NaN statistic and a NaN p-value. Emitting that as `status=ok`
with `reject_null=False` puts a statistical conclusion nobody drew into the
evidence path with a successful status on it. A non-finite value anywhere in the
output declines with `non_finite_result` instead — a decline rather than an
error, because the question was unanswerable rather than the implementation
broken, and distinct from `non_finite_input`, which is the caller's to fix.

## Refusals

Executor-side refusals — budget breaches, `undeclared_egress` — are the
runtime's own codes, produced by its machinery only; no operation declares one,
and provider code never returns or raises one. The one event on this plane that
is about integrity rather than capability — a model file that misses its pin —
is `score.rank`'s separately declared `model_digest_mismatch`.

The conditions only a quantitative implementation can detect — a series too short
for the declared seasonal model, a test name that is not a test, a model
reference of the wrong shape — are the operations' own refusals, declared per
operation in `registration.json`, so the code a caller reads *is* the reason.

`refusals.py` is the closed set this plane's operations draw from,
`QUANT_DECLINES`, plus a constructor that refuses to build anything outside it:
the runtime's codes are reserved for the runtime, and an implementation
reporting `cache_integrity` would be making a judgement it is not positioned to
make. `tests/test_refusals.py` fails if any member of that set is not exercised
by a named test.

The line between the two is capability versus integrity. "I do not do that" is a
decline; "this is not the artifact that was approved" is not, and it must stay
countable on its own.

## Secrets and egress

Every implementation declares **zero endpoints** and computes locally; the
egress-conformance lane asserts declared equals observed equals empty for all
seven. None of them requests a credential — no implementation reads
`context.secrets`, and a test asserts that statically across the sources and
behaviourally by checking that a run with a credential present produces output
identical to a run without one.

## Registration and numerical transport

The package declares its typed operations through `registration.json`. Core
reads them and pins each named Contract at install; it need not contain a
quantitative-provider catalog. A competing implementation serves the same
signature — the same input and output Contracts and effect.

Quantitative engines compute with native numbers. At the invocation boundary,
finite fractional values use canonical `{"$decimal": "0.125"}` objects so accepted
inputs and outputs contain no binary JSON floats. Integers stay integers. The
`numeric` helpers encode and decode this representation; the adapter encodes its
output before returning. This does not increase the numerical precision of the
underlying engines. Noncanonical decimals and values outside engine range refuse.
