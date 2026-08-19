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
from collections.abc import Callable
from typing import ClassVar

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
        # An explicit table, not getattr dispatch. Attribute-name dispatch on an
        # input-controlled string reaches every attribute the class happens to
        # have, which in a reference implementation is exactly the pattern a
        # plane package should not be copying.
        handler = self._MODES.get(mode)
        if handler is None:
            return ProviderResult.refused(
                RefusalCode.PROVIDER_DECLINED,
                f"unknown mode {mode!r}",
                mode=mode,
                known=sorted(self._MODES),
            )
        return handler(self, context, text)

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

    def _mode_context_repr(self, context: ProviderRunContext, text: str) -> ProviderResult:
        """Return the run context's own repr, which must not carry credentials."""

        del text
        sys.stderr.write(f"context: {context!r}\n")
        return ProviderResult.ok(
            {
                "echo": "context-repr",
                "input_bucket": context.input_bucket,
                "context_repr": repr(context),
            }
        )

    def _mode_connect(self, context: ProviderRunContext, text: str) -> ProviderResult:
        """Actually open a socket, so the conformance lane's guard has a target.

        Outside the lane this reaches a reserved-for-testing name that does not
        resolve, so it fails either way; inside the lane it must fail *because
        the guard stopped it*, which is what makes the lane's premise checkable.
        """

        del text
        import socket

        socket.create_connection(("egress.invalid", 80), timeout=1)
        return ProviderResult.ok({"echo": "connected", "input_bucket": context.input_bucket})

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

    _MODES: ClassVar[dict[str, Callable[[NoopEcho, ProviderRunContext, str], ProviderResult]]] = {
        "echo": _mode_echo,
        "refuse": _mode_refuse,
        "error": _mode_error,
        "credential": _mode_credential,
        "leak": _mode_leak,
        "egress": _mode_egress,
        "context_repr": _mode_context_repr,
        "connect": _mode_connect,
        "slow": _mode_slow,
        "loud": _mode_loud,
    }
