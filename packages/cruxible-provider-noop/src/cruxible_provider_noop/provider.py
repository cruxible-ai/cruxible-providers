"""The reference no-op provider.

One entrypoint, one interface, and a ``mode`` switch that reaches every branch
the protocol has to survive: success, a typed refusal, a crash, a credential
delivered by ref, a deliberate leak attempt, an undeclared endpoint, and both
budget breaches. Everything it does is local and instantaneous, so the whole
conformance suite runs without a network, a container engine, or a clock to wait
on — except the one test that is about waiting.

The leak mode is the interesting one. A provider that *tries* to write its
credential into trace material must still fail to: redaction happens in the
harness on the way out, not in the provider's good manners.
"""

from __future__ import annotations

import hashlib
import sys
import time
from typing import Any

from cruxible_provider_runtime.errors import RefusalCode
from cruxible_provider_runtime.provider_api import ProviderResult, ProviderRunContext

from .interface import INTERFACE_ID

__all__ = ["CREDENTIAL_REF", "NoopEcho"]

CREDENTIAL_REF = "noop.dummy_credential"


class NoopEcho:
    """Echoes its input back, or takes whichever branch ``mode`` names."""

    interface_id = INTERFACE_ID

    def __call__(self, context: ProviderRunContext) -> ProviderResult:
        mode = str(context.input.get("mode", "echo"))
        text = str(context.input.get("text", ""))
        handler = getattr(self, f"_mode_{mode}", None)
        if handler is None:
            return ProviderResult.refused(
                RefusalCode.PROVIDER_DECLINED,
                f"unknown mode {mode!r}",
                mode=mode,
            )
        result: ProviderResult = handler(context, text)
        return result

    # -- branches ----------------------------------------------------------

    def _mode_echo(self, context: ProviderRunContext, text: str) -> ProviderResult:
        return ProviderResult.ok(
            {"echo": text, "input_bucket": context.input_bucket},
            metrics={"characters": float(len(text))},
        )

    def _mode_refuse(self, context: ProviderRunContext, text: str) -> ProviderResult:
        del text
        return ProviderResult.refused(
            RefusalCode.PROVIDER_DECLINED,
            "the reference provider was asked to decline",
            run_id=context.run_id,
        )

    def _mode_error(self, context: ProviderRunContext, text: str) -> ProviderResult:
        del context, text
        raise RuntimeError("the reference provider was asked to fail")

    def _mode_credential(self, context: ProviderRunContext, text: str) -> ProviderResult:
        del text
        material = context.secrets.get(CREDENTIAL_REF)
        if material is None:
            return ProviderResult.refused(
                RefusalCode.UNRESOLVED_SECRET_REF,
                f"credential {CREDENTIAL_REF!r} was not delivered",
                ref=CREDENTIAL_REF,
            )
        fingerprint = hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
        return ProviderResult.ok(
            {
                "echo": "credential-received",
                "input_bucket": context.input_bucket,
                "credential_length": len(material),
                "credential_fingerprint": fingerprint,
            }
        )

    def _mode_leak(self, context: ProviderRunContext, text: str) -> ProviderResult:
        """Deliberately try to write the credential everywhere it must not go."""

        del text
        material = context.secrets.get(CREDENTIAL_REF, "")
        sys.stderr.write(f"leaking to stderr: {material}\n")
        return ProviderResult.ok(
            {"echo": material, "input_bucket": context.input_bucket},
            events=[{"note": "leaking to trace", "credential": material}],
        )

    def _mode_egress(self, context: ProviderRunContext, text: str) -> ProviderResult:
        """Contact an endpoint the manifest does not declare — without a network.

        Recording is what the conformance rule is about: the comparison of
        declared against observed happens on the recorded endpoint, so this
        branch needs no socket at all.
        """

        del text
        context.egress.record("https://undeclared.example")
        return ProviderResult.ok({"echo": "contacted", "input_bucket": context.input_bucket})

    def _mode_slow(self, context: ProviderRunContext, text: str) -> ProviderResult:
        del text
        time.sleep(context.budgets.wall_clock_seconds * 10 + 30)
        return ProviderResult.ok({"echo": "never", "input_bucket": context.input_bucket})

    def _mode_loud(self, context: ProviderRunContext, text: str) -> ProviderResult:
        del text
        chunk = "x" * 65536
        limit = context.budgets.output_bytes
        written = 0
        while written <= limit * 4:
            sys.stdout.write(chunk)
            sys.stdout.flush()
            written += len(chunk)
        return ProviderResult.ok({"echo": "never", "input_bucket": context.input_bucket})


def describe() -> dict[str, Any]:
    """The modes this reference provider offers, for documentation surfaces."""

    return {
        "interface_id": INTERFACE_ID,
        "modes": [
            name.removeprefix("_mode_")
            for name in sorted(dir(NoopEcho))
            if name.startswith("_mode_")
        ],
    }
