"""Package-owned operation contracts and request classifiers.

Exact definitions live in bundled contracts/*.json; their frozen predecessors
remain in contracts/history for historical digest verification.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from cruxible_provider_runtime.buckets import BucketVocabulary
from cruxible_provider_runtime.canonical import domain_digest
from cruxible_provider_runtime.registration import read_interface_definition
from cruxible_provider_runtime.registry import InterfaceRegistration

from .classifiers import CLASSIFIERS

__all__ = [
    "INTERFACE_DIGESTS",
    "INTERFACE_IDS",
    "INTERFACE_PREIMAGES",
    "STUB_INTERFACE_DOMAIN_TAG",
    "recompute_interface_digest",
    "registration",
]

STUB_INTERFACE_DOMAIN_TAG = "cruxible.interface.stub.v1"

INTERFACE_IDS: tuple[str, ...] = (
    "calc.calibrate",
    "calc.reduce",
    "match.record",
    "score.rank",
    "stat.test",
    "ts.anomaly",
    "ts.forecast",
)

INTERFACE_PREIMAGES: dict[str, dict[str, Any]] = {
    identity: read_interface_definition(Path(__file__).parent, identity)
    for identity in INTERFACE_IDS
}

INTERFACE_DIGESTS: dict[str, str] = {
    "calc.reduce": "sha256:eb3af17ce73448162d8ca7374c9d97b0e3b240feb1148a8b3525903bddc13fcd",
    "calc.calibrate": "sha256:d8cfaf2528fe8abc5da1142f54e73d6faad86ab93f87d3eca821298e797dc6a5",
    "stat.test": "sha256:99835a41b6b8ccc61dc8d36468fcd111b7e890f595983184dcfc488d9014d998",
    "score.rank": "sha256:6cdf784479702b0df2b513746f6bd4ab0127644ec80bba2c70c28c7119dbd906",
    "ts.forecast": "sha256:ef8f0bed68b4283b3d74381cd64c293eab8d8deb7ee5cf21c07f44766b3a8b80",
    "ts.anomaly": "sha256:5db32340ce1f143443552ead5ac6de1d70187640689b584b11ca40fd133bf2b2",
    "match.record": "sha256:144dee9499e89fc2504bf1b9a47e98033582efbc592b789ffb9c377cd530c2dd",
}


def recompute_interface_digest(interface_id: str) -> str:
    """Recompute the retained interface digest rule for the drift test."""

    return domain_digest(STUB_INTERFACE_DOMAIN_TAG, INTERFACE_PREIMAGES[interface_id])


def registration(
    interface_id: str, vocabulary: BucketVocabulary, description: str = ""
) -> InterfaceRegistration:
    """Build an executable registration for the standalone conformance harness.

    The package-owned descriptor is the generic, data-only export. This helper
    accepts an explicitly loaded vocabulary for local classifier execution.
    """

    return InterfaceRegistration(
        interface_id=interface_id,
        interface_digest=INTERFACE_DIGESTS[interface_id],
        bucket_vocabulary=vocabulary,
        classifier=CLASSIFIERS[interface_id],
        description=description or vocabulary.description.strip(),
    )
