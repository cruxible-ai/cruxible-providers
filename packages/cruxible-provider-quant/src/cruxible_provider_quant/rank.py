"""``score.rank`` — order candidates against a declared objective.

Two modes.

``weighted``
    A declared linear combination of declared signals. No model file, no
    deserialisation, no fitted state — the whole scorer is in the request, so
    the score is reproducible from the receipt alone. This is the mode the
    conformance fixtures use.

``pinned_model``
    Score with a pinned, serialised scikit-learn estimator. The reference names
    a path, a **required** sha256, an explicit feature order, and the score kind
    the model produces. The bytes are hashed before anything is loaded.

Two kinds of failure, two kinds of refusal
-------------------------------------------

A reference that is malformed and a reference whose bytes do not hash to their
pin are not the same event, and collapsing them would lose the one that matters.

*Shape* failures — no ``model_ref``, an unsupported ``kind``, a missing path, an
absent or ill-formed pin, an unnamed score scale, a missing ``feature_order`` —
say this implementation was asked for something it does not do. They decline
under ``malformed_model_ref``, alongside the plane's other capability limits. An
unreadable path belongs here too: a file that is not there is a request that
cannot be served, not a file that has been altered.

A *byte* mismatch says something else entirely. The file exists, it was read,
and it is **not the artifact that was reviewed** — the one event on this path
that is an integrity signal rather than a capability limit. That gets
``RefusalCode.ARTIFACT_HASH_MISMATCH``, which the runtime taxonomy already
defines for exactly this ("bytes do not hash to the pinned sha256"), so it
reaches a receipt and a track record wearing its own name and can be counted
apart from ordinary declines.

The honest boundary on ``pinned_model``
---------------------------------------

Verifying the hash proves the file is **the one that was pinned**. It does not
make loading it safe. Deserialising a pickle executes code by design, so a
pinned model is trusted code, and the local backend is a dependency-isolation
mechanism rather than a security boundary — a model loaded here runs with the
operator's privileges. Nothing in this module changes that, and it would be
dishonest to present the hash check as though it did. What the hash check buys
is that the trust decision is made **once**, at pinning time, over a specific
artifact, instead of implicitly on every run over whatever is at that path.

Containment for a third-party model exists in the cloud container backend and
nowhere else. A marketplace surface offering someone else's pinned model for
local execution has to say so.

No confidence score
-------------------

The output carries a ``score`` per item and a ``score_kind`` naming the scale it
lives on — ``weighted_sum``, ``decision_function``, ``positive_class_probability``
— because a score without its scale is uninterpretable and a score whose scale
is hidden is exactly what a "confidence" is. Ties are broken by ascending id, the
rule is reported in ``tie_break``, and every item tied with another lists them,
so a reader can see when the ordering was decided by the tie-break rather than
by the objective.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from cruxible_provider_runtime.errors import RefusalCode
from cruxible_provider_runtime.provider_api import ProviderResult, ProviderRunContext

from .outputs import ok_if_finite
from .refusals import DeclineReason, decline

__all__ = ["MODES", "SCORE_KINDS", "TIE_BREAK", "Rank"]

MODES: tuple[str, ...] = ("weighted", "pinned_model")
SCORE_KINDS: tuple[str, ...] = (
    "weighted_sum",
    "decision_function",
    "positive_class_probability",
)
TIE_BREAK = "id_ascending"
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


class Rank:
    """Order candidates by a computed score, against a declared objective."""

    interface_id = "score.rank"

    def __call__(self, context: ProviderRunContext) -> ProviderResult:
        payload = context.input

        objective = payload.get("objective")
        if not isinstance(objective, str) or not objective.strip():
            return decline(
                DeclineReason.INVALID_PARAMETER,
                "an ordering needs a declared objective; a rank means nothing without one",
            )

        items = self._items(payload)
        if isinstance(items, ProviderResult):
            return items

        mode = payload.get("mode")
        if mode == "weighted":
            scored = self._weighted(payload, items)
        elif mode == "pinned_model":
            scored = self._pinned_model(payload, items)
        else:
            return decline(
                DeclineReason.UNKNOWN_METHOD,
                f"mode {mode!r} is not one this implementation performs",
                supported=list(MODES),
            )
        if isinstance(scored, ProviderResult):
            return scored
        score_kind, signals_used, scores, engine = scored

        ranking = self._ranking(items, scores)
        return ok_if_finite(
            {
                "mode": str(mode),
                "objective": objective,
                "score_kind": score_kind,
                "tie_break": TIE_BREAK,
                "signals_used": signals_used,
                "ranking": ranking,
                "engine": engine,
            },
            metrics={"candidates": float(len(items))},
        )

    # -- input -------------------------------------------------------------

    @staticmethod
    def _items(payload: Any) -> list[tuple[str, Mapping[str, Any]]] | ProviderResult:
        raw = payload.get("items")
        if not isinstance(raw, Sequence) or isinstance(raw, str | bytes) or not raw:
            return decline(DeclineReason.INVALID_PARAMETER, "items must be a non-empty array")
        items: list[tuple[str, Mapping[str, Any]]] = []
        seen: set[str] = set()
        for item in raw:
            if not isinstance(item, Mapping):
                return decline(DeclineReason.INVALID_PARAMETER, "each item must be an object")
            identifier = item.get("id")
            signals = item.get("signals")
            if not isinstance(identifier, str) or not identifier:
                return decline(DeclineReason.INVALID_PARAMETER, "each item needs a string id")
            if identifier in seen:
                return decline(
                    DeclineReason.INVALID_PARAMETER,
                    f"item id {identifier!r} appears twice; an ordering needs distinct items",
                    id=identifier,
                )
            if not isinstance(signals, Mapping) or not signals:
                return decline(
                    DeclineReason.INVALID_PARAMETER, "each item needs a non-empty signals object"
                )
            seen.add(identifier)
            items.append((identifier, signals))
        return items

    # -- modes -------------------------------------------------------------

    def _weighted(
        self, payload: Any, items: list[tuple[str, Mapping[str, Any]]]
    ) -> tuple[str, list[str], list[float], dict[str, Any]] | ProviderResult:
        weights = payload.get("weights")
        if not isinstance(weights, Mapping) or not weights:
            return decline(
                DeclineReason.INVALID_PARAMETER,
                "weighted mode needs a non-empty weights object",
            )
        names = sorted(str(name) for name in weights)
        coefficients: list[float] = []
        for name in names:
            weight = weights[name]
            if isinstance(weight, bool) or not isinstance(weight, int | float):
                return decline(
                    DeclineReason.INVALID_PARAMETER, f"weight for {name!r} is not a number"
                )
            if not math.isfinite(float(weight)):
                return decline(DeclineReason.NON_FINITE_INPUT, "every weight must be finite")
            coefficients.append(float(weight))

        scores: list[float] = []
        for identifier, signals in items:
            total = 0.0
            for name, coefficient in zip(names, coefficients, strict=True):
                if name not in signals:
                    return decline(
                        DeclineReason.UNKNOWN_COLUMN,
                        f"item {identifier!r} carries no signal {name!r}, which the weights name",
                        id=identifier,
                        signal=name,
                    )
                value = signals[name]
                if isinstance(value, bool) or not isinstance(value, int | float):
                    return decline(
                        DeclineReason.INVALID_PARAMETER,
                        f"signal {name!r} of item {identifier!r} is not a number",
                    )
                if not math.isfinite(float(value)):
                    return decline(
                        DeclineReason.NON_FINITE_INPUT, "every signal value must be finite"
                    )
                total += coefficient * float(value)
            scores.append(total)
        return "weighted_sum", names, scores, {"name": "weighted_sum", "signals": len(names)}

    def _pinned_model(
        self, payload: Any, items: list[tuple[str, Mapping[str, Any]]]
    ) -> tuple[str, list[str], list[float], dict[str, Any]] | ProviderResult:
        reference = payload.get("model_ref")
        if not isinstance(reference, Mapping):
            return decline(
                DeclineReason.MALFORMED_MODEL_REF, "pinned_model mode needs a model_ref object"
            )
        if reference.get("kind") != "pickled_sklearn":
            return decline(
                DeclineReason.MALFORMED_MODEL_REF,
                f"model_ref.kind {reference.get('kind')!r} is not supported",
                supported=["pickled_sklearn"],
            )
        pin = reference.get("sha256")
        path = reference.get("path")
        score_kind = reference.get("score_kind")
        order = reference.get("feature_order")
        if not isinstance(path, str) or not path:
            return decline(DeclineReason.MALFORMED_MODEL_REF, "model_ref needs a path")
        if not isinstance(pin, str) or not _SHA256.match(pin):
            return decline(
                DeclineReason.MALFORMED_MODEL_REF,
                "model_ref needs a sha256:<hex> pin; an unpinned model reference is a "
                "trust decision nobody made",
            )
        model_score_kinds = tuple(kind for kind in SCORE_KINDS if kind != "weighted_sum")
        if score_kind not in model_score_kinds:
            return decline(
                DeclineReason.MALFORMED_MODEL_REF,
                f"model_ref.score_kind {score_kind!r} is not a named scale",
                supported=list(model_score_kinds),
            )
        if (
            not isinstance(order, Sequence)
            or isinstance(order, str | bytes)
            or not order
            or not all(isinstance(name, str) for name in order)
        ):
            return decline(
                DeclineReason.MALFORMED_MODEL_REF,
                "model_ref needs an explicit feature_order; column order read off a "
                "dictionary is not a contract",
            )
        feature_order = [str(name) for name in order]

        try:
            blob = Path(path).read_bytes()
        except OSError as exc:
            # A shape failure, not an integrity one: a file that is not there
            # has not been altered, it was never supplied.
            return decline(
                DeclineReason.MALFORMED_MODEL_REF,
                f"model_ref path could not be read: {exc.strerror}",
                path=path,
            )
        observed = "sha256:" + hashlib.sha256(blob).hexdigest()
        if observed != pin:
            # The integrity signal. Not a decline: the runtime taxonomy already
            # names this event, and it has to be countable apart from the
            # capability limits above — a provider that cannot serve a request
            # and a provider handed an artifact nobody approved are different
            # things for a track record to have seen.
            return ProviderResult.refused(
                RefusalCode.ARTIFACT_HASH_MISMATCH,
                "the model file does not hash to its pin; this is not the artifact that "
                "was reviewed",
                pinned=pin,
                observed=observed,
                path=path,
            )

        rows: list[list[float]] = []
        for identifier, signals in items:
            row: list[float] = []
            for name in feature_order:
                if name not in signals:
                    return decline(
                        DeclineReason.UNKNOWN_COLUMN,
                        f"item {identifier!r} carries no signal {name!r}, which the "
                        "model's feature_order names",
                        id=identifier,
                        signal=name,
                    )
                value = signals[name]
                if isinstance(value, bool) or not isinstance(value, int | float):
                    return decline(
                        DeclineReason.INVALID_PARAMETER,
                        f"signal {name!r} of item {identifier!r} is not a number",
                    )
                if not math.isfinite(float(value)):
                    return decline(
                        DeclineReason.NON_FINITE_INPUT, "every signal value must be finite"
                    )
                row.append(float(value))
            rows.append(row)

        # Verified above against its pin, and still executed as trusted code:
        # see the module docstring's boundary statement.
        import pickle

        import numpy as np
        import sklearn

        model = pickle.loads(blob)
        matrix = np.asarray(rows, dtype=np.float64)
        if score_kind == "decision_function":
            raw_scores = np.asarray(model.decision_function(matrix), dtype=np.float64)
        else:
            raw_scores = np.asarray(model.predict_proba(matrix), dtype=np.float64)[:, 1]

        return (
            str(score_kind),
            feature_order,
            [float(value) for value in raw_scores],
            {
                "name": f"sklearn.{type(model).__name__}",
                "version": str(sklearn.__version__),
                "model_sha256": pin,
            },
        )

    # -- ordering ----------------------------------------------------------

    @staticmethod
    def _ranking(
        items: list[tuple[str, Mapping[str, Any]]], scores: list[float]
    ) -> list[dict[str, Any]]:
        pairs = [(identifier, score) for (identifier, _), score in zip(items, scores, strict=True)]
        ordered = sorted(pairs, key=lambda pair: (-pair[1], pair[0]))
        by_score: dict[float, list[str]] = {}
        for identifier, score in ordered:
            by_score.setdefault(score, []).append(identifier)
        return [
            {
                "rank": position,
                "id": identifier,
                "score": score,
                "tied_with": [other for other in by_score[score] if other != identifier],
            }
            for position, (identifier, score) in enumerate(ordered, start=1)
        ]
