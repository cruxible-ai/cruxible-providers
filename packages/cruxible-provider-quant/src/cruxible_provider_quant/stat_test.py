"""``stat.test`` — run the declared test, exactly, and report what it rests on.

**The declared test is executed exactly.** There is no automatic test selection
here, and the absence is a design position rather than an omission. A slot that
looked at the data and chose a test would make the reported p-value conditional
on a choice nobody recorded, and a test result that quietly switched families
because an assumption looked shaky is not a more careful result — it is a
different result, silently substituted. So an unknown test name refuses
(``unknown_test_name``), and a declared family that does not match the named
test's family refuses too (``declared_family_mismatch``).

Assumptions travel with the result. The caller declares which assumptions the
design rests on; where an assumption is checkable from the sample, this
implementation checks it and reports the check, its own p-value, and whether it
holds — *without* changing which test runs. A run whose normality check fails is
still a run of the test that was declared, with a flag saying so. That is what
makes the flag worth anything.

Engines: SciPy for everything except the two-proportion z test, which is
statsmodels' ``proportions_ztest``.

No confidence score. What the output carries is a statistic with its kind named,
a p-value, the degrees of freedom where the test has them, the alpha the caller
declared, the resulting reject/retain decision at that alpha, and a named effect
measure where one is defined. Each is meaningful only against the test that
produced it, which is why the test name is in the output beside them.

Determinism: every test here is a closed-form or exactly-enumerated computation,
so results are reproducible run to run and across platforms to within float
noise. The suite asserts statistics and p-values to a relative tolerance of
1e-9.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from cruxible_provider_runtime.provider_api import ProviderResult, ProviderRunContext

from .refusals import DeclineReason, decline
from .stdio import stdout_to_stderr

__all__ = ["TESTS", "StatTest", "StatTestSpec"]


@dataclass(frozen=True)
class StatTestSpec:
    """One declared test: its family, its samples, and its effect measure.

    Named ``StatTestSpec`` rather than ``TestSpec`` because pytest collects any
    importable class whose name starts with ``Test``. It would find this one in
    any downstream test module that imported it and report it as an uncollectable
    test class — a warning with nothing behind it, arriving in somebody else's
    suite.
    """

    family: str
    groups: int
    paired: bool
    statistic_kind: str
    effect_kind: str | None
    checkable_assumptions: tuple[str, ...]


TESTS: dict[str, StatTestSpec] = {
    "student_t": StatTestSpec("location", 2, False, "t", "mean_difference", ("normality",)),
    "welch_t": StatTestSpec("location", 2, False, "t", "mean_difference", ("normality",)),
    "paired_t": StatTestSpec("location", 2, True, "t", "mean_difference", ("normality",)),
    "mann_whitney_u": StatTestSpec("location", 2, False, "u", "rank_biserial", ()),
    "wilcoxon_signed_rank": StatTestSpec("location", 2, True, "w", None, ()),
    "proportions_z": StatTestSpec("proportion", 2, False, "z", "proportion_difference", ()),
    "levene": StatTestSpec("variance", 2, False, "f", "variance_ratio", ()),
    "bartlett": StatTestSpec("variance", 2, False, "chi2", "variance_ratio", ("normality",)),
    "pearson_r": StatTestSpec("association", 2, True, "r", "pearson_r", ("normality",)),
    "spearman_rho": StatTestSpec("association", 2, True, "rho", "spearman_rho", ()),
    "chi2_contingency": StatTestSpec("association", 2, False, "chi2", "cramers_v", ()),
    "ks_2samp": StatTestSpec("distributional", 2, False, "d", None, ()),
    "shapiro_wilk": StatTestSpec("distributional", 1, False, "w", None, ()),
}

_ALTERNATIVES = ("two-sided", "less", "greater")


class StatTest:
    """Execute one declared statistical test and report its assumptions."""

    interface_id = "stat.test"

    def __call__(self, context: ProviderRunContext) -> ProviderResult:
        payload = context.input

        name = payload.get("test")
        if not isinstance(name, str) or name not in TESTS:
            return decline(
                DeclineReason.UNKNOWN_TEST_NAME,
                f"test {name!r} is not one this implementation runs; nothing is substituted for it",
                supported=sorted(TESTS),
            )
        spec = TESTS[name]

        declared_family = payload.get("test_family")
        if declared_family != spec.family:
            return decline(
                DeclineReason.DECLARED_FAMILY_MISMATCH,
                f"test {name!r} belongs to the {spec.family!r} family, and "
                f"{declared_family!r} was declared",
                test=name,
                declared_family=declared_family,
                actual_family=spec.family,
            )

        alpha = payload.get("alpha")
        if isinstance(alpha, bool) or not isinstance(alpha, int | float) or not 0.0 < alpha < 1.0:
            return decline(
                DeclineReason.INVALID_PARAMETER,
                "alpha must be a number strictly between 0 and 1",
                alpha=alpha,
            )
        alpha = float(alpha)

        alternative = payload.get("alternative", "two-sided")
        if alternative not in _ALTERNATIVES:
            return decline(
                DeclineReason.INVALID_PARAMETER,
                f"alternative must be one of {list(_ALTERNATIVES)}",
                alternative=alternative,
            )

        samples = self._samples(payload, spec)
        if isinstance(samples, ProviderResult):
            return samples

        return self._run(name, spec, samples, alpha, str(alternative), payload)

    # -- input -------------------------------------------------------------

    @staticmethod
    def _samples(payload: Any, spec: StatTestSpec) -> list[list[float]] | ProviderResult:
        raw = payload.get("samples")
        if not isinstance(raw, Mapping) or not raw:
            return decline(
                DeclineReason.INVALID_PARAMETER, "samples must be a non-empty object of groups"
            )
        groups: list[list[float]] = []
        for key in sorted(raw):
            values = raw[key]
            if not isinstance(values, Sequence) or isinstance(values, str | bytes) or not values:
                return decline(
                    DeclineReason.INVALID_PARAMETER,
                    f"sample group {key!r} must be a non-empty array of numbers",
                )
            numbers: list[float] = []
            for value in values:
                if isinstance(value, bool) or not isinstance(value, int | float):
                    return decline(
                        DeclineReason.INVALID_PARAMETER,
                        f"sample group {key!r} carries a non-numeric observation",
                    )
                if not math.isfinite(float(value)):
                    return decline(
                        DeclineReason.NON_FINITE_INPUT,
                        f"sample group {key!r} carries a non-finite observation",
                    )
                numbers.append(float(value))
            groups.append(numbers)
        if len(groups) != spec.groups:
            return decline(
                DeclineReason.INVALID_PARAMETER,
                f"this test takes {spec.groups} sample group(s), and {len(groups)} were given",
                groups=len(groups),
                required=spec.groups,
            )
        if spec.paired and len({len(group) for group in groups}) != 1:
            return decline(
                DeclineReason.MISMATCHED_LENGTHS,
                "a paired test needs its groups to be the same length",
                lengths=[len(group) for group in groups],
            )
        return groups

    # -- execution ---------------------------------------------------------

    def _run(
        self,
        name: str,
        spec: StatTestSpec,
        groups: list[list[float]],
        alpha: float,
        alternative: str,
        payload: Any,
    ) -> ProviderResult:
        import numpy as np
        import scipy
        from scipy import stats

        arrays = [np.asarray(group, dtype=np.float64) for group in groups]
        engine: dict[str, Any] = {"name": "scipy.stats", "version": str(scipy.__version__)}

        with stdout_to_stderr():
            statistic, p_value, dof, effect_value = self._execute(
                name, arrays, alternative, engine, np, stats
            )

        if statistic is None or p_value is None:
            return decline(
                DeclineReason.INVALID_PARAMETER,
                f"test {name!r} could not be computed on the samples as given",
                test=name,
            )

        declared = payload.get("declared_assumptions")
        declared_names = (
            [str(item) for item in declared]
            if isinstance(declared, Sequence) and not isinstance(declared, str | bytes)
            else []
        )
        assumptions = self._assumptions(spec, declared_names, arrays, alpha, stats)
        holds = [entry["holds"] for entry in assumptions if entry["holds"] is not None]

        return ProviderResult.ok(
            {
                "test": name,
                "test_family": spec.family,
                "alternative": alternative,
                "alpha": alpha,
                "statistic": float(statistic),
                "statistic_kind": spec.statistic_kind,
                "p_value": float(p_value),
                "degrees_of_freedom": None if dof is None else float(dof),
                "reject_null": bool(float(p_value) < alpha),
                "effect": (
                    None
                    if spec.effect_kind is None or effect_value is None
                    else {"kind": spec.effect_kind, "value": float(effect_value)}
                ),
                "assumptions": assumptions,
                "assumptions_satisfied": (None if not holds else all(holds)),
                "engine": engine,
            },
            metrics={"p_value": float(p_value), "statistic": float(statistic)},
        )

    def _execute(
        self,
        name: str,
        arrays: list[Any],
        alternative: str,
        engine: dict[str, Any],
        np: Any,
        stats: Any,
    ) -> tuple[float | None, float | None, float | None, float | None]:
        """Dispatch on the declared name through an explicit table."""

        if name in {"student_t", "welch_t"}:
            a, b = arrays
            result = stats.ttest_ind(a, b, equal_var=(name == "student_t"), alternative=alternative)
            return result.statistic, result.pvalue, result.df, float(a.mean() - b.mean())
        if name == "paired_t":
            a, b = arrays
            result = stats.ttest_rel(a, b, alternative=alternative)
            return result.statistic, result.pvalue, result.df, float((a - b).mean())
        if name == "mann_whitney_u":
            a, b = arrays
            result = stats.mannwhitneyu(a, b, alternative=alternative)
            rank_biserial = 2.0 * float(result.statistic) / (a.size * b.size) - 1.0
            return result.statistic, result.pvalue, None, rank_biserial
        if name == "wilcoxon_signed_rank":
            a, b = arrays
            result = stats.wilcoxon(a, b, alternative=alternative)
            return result.statistic, result.pvalue, None, None
        if name == "proportions_z":
            import statsmodels
            from statsmodels.stats.proportion import proportions_ztest

            engine["name"] = "statsmodels.stats.proportion.proportions_ztest"
            engine["version"] = str(statsmodels.__version__)
            a, b = arrays
            successes = [float(a.sum()), float(b.sum())]
            sizes = [float(a.size), float(b.size)]
            statistic, p_value = proportions_ztest(successes, sizes, alternative=alternative)
            difference = successes[0] / sizes[0] - successes[1] / sizes[1]
            return float(statistic), float(p_value), None, difference
        if name == "levene":
            a, b = arrays
            result = stats.levene(a, b, center="median")
            return result.statistic, result.pvalue, None, self._variance_ratio(a, b)
        if name == "bartlett":
            a, b = arrays
            result = stats.bartlett(a, b)
            return result.statistic, result.pvalue, 1.0, self._variance_ratio(a, b)
        if name == "pearson_r":
            a, b = arrays
            result = stats.pearsonr(a, b, alternative=alternative)
            return result.statistic, result.pvalue, float(a.size - 2), float(result.statistic)
        if name == "spearman_rho":
            a, b = arrays
            result = stats.spearmanr(a, b, alternative=alternative)
            return float(result.statistic), float(result.pvalue), None, float(result.statistic)
        if name == "chi2_contingency":
            table = np.vstack(arrays)
            chi2, p_value, dof, _ = stats.chi2_contingency(table)
            total = float(table.sum())
            smaller = min(table.shape) - 1
            cramers_v = math.sqrt(float(chi2) / (total * smaller)) if total and smaller else None
            return float(chi2), float(p_value), float(dof), cramers_v
        if name == "ks_2samp":
            a, b = arrays
            result = stats.ks_2samp(a, b, alternative=alternative)
            return result.statistic, result.pvalue, None, None
        assert name == "shapiro_wilk", name
        result = stats.shapiro(arrays[0])
        return result.statistic, result.pvalue, None, None

    @staticmethod
    def _variance_ratio(a: Any, b: Any) -> float | None:
        denominator = float(b.var(ddof=1))
        if denominator == 0.0:
            return None
        return float(a.var(ddof=1)) / denominator

    @staticmethod
    def _assumptions(
        spec: StatTestSpec,
        declared: Sequence[str],
        arrays: list[Any],
        alpha: float,
        stats: Any,
    ) -> list[dict[str, Any]]:
        """Report every declared assumption; check the ones that are checkable.

        A check never changes which test ran. It records what the sample says
        about the ground the declared test stands on, which is a different and
        more useful thing than quietly standing somewhere else.
        """

        entries: list[dict[str, Any]] = []
        for assumption in declared:
            entry: dict[str, Any] = {
                "name": assumption,
                "declared": True,
                "checked": False,
                "check": None,
                "p_value": None,
                "holds": None,
            }
            if assumption == "normality" and "normality" in spec.checkable_assumptions:
                p_values = [
                    float(stats.shapiro(array).pvalue) for array in arrays if array.size >= 3
                ]
                if p_values:
                    entry.update(
                        checked=True,
                        check="shapiro_wilk",
                        p_value=min(p_values),
                        holds=bool(min(p_values) >= alpha),
                    )
            elif assumption == "equal_variance" and len(arrays) == 2:
                p_value = float(stats.levene(*arrays, center="median").pvalue)
                entry.update(
                    checked=True,
                    check="levene",
                    p_value=p_value,
                    holds=bool(p_value >= alpha),
                )
            entries.append(entry)
        return entries
