# cruxible-provider-quant

Cruxible provider adapters for the quantitative plane. Apache-2.0.

Seven classical baselines on seven interfaces. These are the slots where
narrow-ML implementations will later compete for the same track-record key, so
what matters here is not sophistication but **contract fidelity**: the same
input buckets, the same typed output, the same refusals, the same digests. A
baseline that is clever and slightly off-contract is worth less than a dull one
that is exactly on it, because only the second one makes the comparison mean
anything.

| Interface | Engine | What it does |
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
every declared capture-contract family here is named `…capture.derived.v1`, and
a test asserts that none of them is anything else. A provider that graded its
own output would be self-certifying.

## Why these buckets and not others

Each implementation claims the faces of its cube these baselines actually serve.
The exclusions are deliberate, and each one is a slot a later implementation can
claim on the same key:

| Interface | Not claimed | Why |
|---|---|---|
| `calc.reduce` | `row_count=large`, `very_large`, `memory_fit=out_of_core` | the relation arrives as a payload and is in memory by construction; an out-of-core reducer takes a *reference* to storage and is a different implementation |
| `ts.anomaly` | `domain_class=categorical_encoded` | numeric distance between encoded states is not meaningful, so a residual against them is not either |
| `ts.anomaly` | `series_length=very_short`, `medium`, `long`; `gap_profile` other than `regular`; multi-series | STL needs two full periods; imputation and batching are modelling acts this baseline does not perform |
| `ts.forecast` | `domain_class=rates`, `continuous_bounded` | Gaussian prediction intervals can cross a hard bound, and an interval that leaves the feasible set is a wrong interval, not a wide one |
| `ts.forecast` | `domain_class=intermittent` | mostly-zero demand is Croston-family territory, not ARIMA or ETS |
| `ts.forecast` | `exogenous` other than `none`; multi-series | covariate-aware and batched forecasting are separate implementations |
| `stat.test` | `test_family=count_model`; `design=clustered`, `time_ordered`; multivariate | tests that model dependence structure or multiplicity are a different set of tests |
| `score.rank` | `candidate_count=large` | above ten thousand candidates the ordering cost, not the scoring, is what needs designing |
| `match.record` | `label_availability=partial`, `labeled` | this implementation is told its m/u probabilities and never estimates them; a trained variant claims those buckets |
| `match.record` | `pair_space` above `small`, `blocking=none` | an unblocked cross product is a different scaling problem |
| `calc.calibrate` | `outcome_type` other than `binary`; `base_rate=rare_event` | reliability binning over a rare event puts almost every observation in one bin, which is a reading nobody should act on |

Every claimed selector has a passing conformance fixture in
`tests/test_bucket_conformance.py`, and a test asserts the manifest's fixture
ids are exactly the ids the suite defines — so a claim without a passing fixture
cannot exist, in either direction.

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
| the file was read and its bytes do not hash to the pin | `artifact_hash_mismatch` — an **integrity** signal, from the runtime taxonomy, countable on a track record apart from ordinary declines |

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

Executor-side refusals — `unclaimed_bucket`, `unclassified_input`, budget
breaches, `undeclared_egress` — come from the runtime taxonomy unchanged, and so
does `artifact_hash_mismatch`, which the taxonomy already defines for the one
event on this plane that is about integrity rather than capability.

The conditions only a quantitative implementation can detect — a series too short
for the declared seasonal model, a test name that is not a test, a model
reference of the wrong shape — are codes in that same taxonomy. They were a
second enum here for one release, carried inside the `reason` detail of
`provider_declined` because the runtime package was owned by a concurrent batch.
They have since been lifted, so the code a caller reads *is* the reason.

`refusals.py` is now the closed subset a provider on this plane may reach for,
`QUANT_DECLINES`, plus a constructor that refuses to build anything outside it:
most of the taxonomy belongs to the executor, and an implementation reporting
`cache_integrity` would be making a judgement it is not positioned to make.
`tests/test_refusals.py` fails if any member of that subset is not exercised by
a named test.

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

## Where the classifiers live, and where they belong

`classifiers.py` derives every bucket from the actual input. Those functions
belong to **core**, registered with their interfaces, because two
implementations of one slot have to be measured the same way or the comparison
they exist for is meaningless. Core's interface-registration surface does not
exist yet, so they live here against the stub registry, exactly as the reference
no-op provider's stub interface does. They move when the registry lands.
