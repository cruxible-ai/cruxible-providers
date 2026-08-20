"""Cruxible provider adapters for the quantitative plane.

Seven implementations of seven interfaces — ``calc.reduce``, ``ts.anomaly``,
``ts.forecast``, ``stat.test``, ``score.rank``, ``match.record``,
``calc.calibrate`` — each a classical baseline running a real engine. These are
the slots on which narrow-ML models will later compete for the same track-record
key, so what matters about a baseline here is not that it is clever but that it
is *exactly* what its interface says it is: same input buckets, same typed
output, same refusals, same digest discipline.

Two things this package will not do, in any implementation:

* emit a generic confidence score. Prediction intervals at declared levels,
  p-values beside the tests that produced them, match weights against declared
  m/u parameters, and Brier scores over stated bins are all typed fields with
  their own definitions. Collapsing them into one number would throw away the
  disagreement between them, which is the part worth having;
* emit a grade. A forecast, an anomaly flag, a rank, and a linkage score are
  derived readings. Grade is the CaptureContract's to govern, the declared
  contract families are named ``derived``, and a test asserts it.

**Imports are lazy on purpose.** Nothing here imports Polars, statsforecast,
splink, or SciPy. The bind path spawns a child per invocation and a child that
paid for every engine in the plane to answer one aggregate would be paying for
six engines it never calls. Each implementation module imports its own engine
inside the call.
"""

from __future__ import annotations

from pathlib import Path

from .classifiers import CLASSIFIERS
from .interfaces import (
    INTERFACE_DIGESTS,
    INTERFACE_IDS,
    INTERFACE_PREIMAGES,
    recompute_interface_digest,
    registration,
)
from .refusals import DeclineReason, decline

__version__ = "0.1.0"

PACKAGE_ROOT = Path(__file__).resolve().parent
MANIFEST_PATH = PACKAGE_ROOT / "manifest.yaml"
CONTAINER_DIR = PACKAGE_ROOT.parent.parent / "container"

__all__ = [
    "CLASSIFIERS",
    "CONTAINER_DIR",
    "INTERFACE_DIGESTS",
    "INTERFACE_IDS",
    "INTERFACE_PREIMAGES",
    "MANIFEST_PATH",
    "PACKAGE_ROOT",
    "DeclineReason",
    "__version__",
    "decline",
    "recompute_interface_digest",
    "registration",
]
