"""Cruxible provider adapters for the quantitative plane.

Seven operations, one entrypoint each — ``calc.reduce``, ``ts.anomaly``,
``ts.forecast``, ``stat.test``, ``score.rank``, ``match.record``,
``calc.calibrate`` — each a classical baseline running a real engine. These are
the signatures on which narrow-ML models will later compete, so what matters
about a baseline here is not that it is clever but that it is *exactly* what
its operation declares: the same typed output, the same declared refusals, the
same digest discipline.

Two things this package will not do, in any implementation:

* emit a generic confidence score. Prediction intervals at declared levels,
  p-values beside the tests that produced them, match weights against declared
  m/u parameters, and Brier scores over stated bins are all typed fields with
  their own definitions. Collapsing them into one number would throw away the
  disagreement between them, which is the part worth having;
* emit a grade. A forecast, an anomaly flag, a rank, and a linkage score are
  derived readings. Grade is the CaptureContract's to govern, and a test
  asserts no output carries one.

**Imports are lazy on purpose.** Nothing here imports Polars, statsforecast,
splink, or SciPy. The bind path spawns a child per invocation and a child that
paid for every engine in the plane to answer one aggregate would be paying for
six engines it never calls. Each implementation module imports its own engine
inside the call.
"""

from __future__ import annotations

from pathlib import Path

from .refusals import QUANT_DECLINES, decline

__version__ = "0.3.0"

PACKAGE_ROOT = Path(__file__).resolve().parent
MANIFEST_PATH = PACKAGE_ROOT / "manifest.yaml"
CONTAINER_DIR = PACKAGE_ROOT.parent.parent / "container"

OPERATIONS: dict[str, str] = {
    "calc.calibrate": "cruxible_provider_quant.calibrate:Calibrate",
    "calc.reduce": "cruxible_provider_quant.reduce:Reduce",
    "match.record": "cruxible_provider_quant.linkage:RecordLinkage",
    "score.rank": "cruxible_provider_quant.rank:Rank",
    "stat.test": "cruxible_provider_quant.stat_test:StatTest",
    "ts.anomaly": "cruxible_provider_quant.anomaly:Anomaly",
    "ts.forecast": "cruxible_provider_quant.forecast:Forecast",
}
"""Each operation this package serves and the entrypoint serving it."""

__all__ = [
    "CONTAINER_DIR",
    "MANIFEST_PATH",
    "OPERATIONS",
    "PACKAGE_ROOT",
    "QUANT_DECLINES",
    "__version__",
    "decline",
]
