#!/usr/bin/env python
"""Write the provider-runtime protocol 2.0 wire contract core mirrors.

Core deliberately does not import ``cruxible_provider_runtime``; it keeps its
own copy of the wire models and pins a fixture produced by this repository's
code. This script is that producer:

    uv run python scripts/export_runtime_contract.py          # rewrite
    uv run python scripts/export_runtime_contract.py --check  # exit 1 on drift

The fixture carries the wire models' JSON schemas, the runtime's reserved
refusal codes (and the subset the child harness itself produces, the only ones
an envelope may carry), the operation-code grammar, valid and invalid vectors (a
protocol-1.x context among the invalid ones), the receipt fields an invocation
records, and the digest goldens (implementation, materialization, operation
signature) with the cases that produce them. The commit it was produced at is
not inside it — a file cannot name the commit that adds it — so core records
the commit it vendored the fixture from alongside the copy.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
RUNTIME = REPO_ROOT / "packages" / "cruxible-provider-runtime"
sys.path.insert(0, str(RUNTIME / "src"))

from cruxible_provider_runtime import __version__ as RUNTIME_VERSION  # noqa: E402
from cruxible_provider_runtime import protocol  # noqa: E402
from cruxible_provider_runtime.artifact import ARTIFACT_DOMAIN_TAG  # noqa: E402
from cruxible_provider_runtime.digests import (  # noqa: E402
    IMPLEMENTATION_DOMAIN_TAG,
    MATERIALIZATION_DOMAIN_TAG,
    implementation_digest,
)
from cruxible_provider_runtime.egress import DYNAMIC_ENDPOINT_FORMS  # noqa: E402
from cruxible_provider_runtime.errors import (  # noqa: E402
    HARNESS_REFUSAL_CODES,
    REFUSAL_CODE_RE,
    RUNTIME_REFUSAL_CODES,
    ProviderErrorPayload,
    Refusal,
)
from cruxible_provider_runtime.manifest import (  # noqa: E402
    MANIFEST_DOMAIN_TAG,
    ImplementationManifest,
    ProviderManifest,
)
from cruxible_provider_runtime.operations import (  # noqa: E402
    OPERATION_SIGNATURE_DOMAIN,
    ContractRef,
    OperationRefusal,
    ProviderOperation,
    operation_signature_digest,
)
from cruxible_provider_runtime.registration import (  # noqa: E402
    PackageRegistration,
    RuntimeRequirement,
)

TARGET = RUNTIME / "tests" / "fixtures" / "provider_runtime_contract_v2.json"
GOLDEN = RUNTIME / "tests" / "fixtures" / "golden"

_MODELS: dict[str, type[Any]] = {
    "Budgets": protocol.Budgets,
    "ContractRef": ContractRef,
    "ImplementationManifest": ImplementationManifest,
    "OperationRefusal": OperationRefusal,
    "PackageRegistration": PackageRegistration,
    "ProtocolVersion": protocol.ProtocolVersion,
    "ProviderErrorPayload": ProviderErrorPayload,
    "ProviderManifest": ProviderManifest,
    "ProviderOperation": ProviderOperation,
    "Refusal": Refusal,
    "ResultEnvelope": protocol.ResultEnvelope,
    "RunContext": protocol.RunContext,
    "RuntimeRequirement": RuntimeRequirement,
    "SecretChannelSpec": protocol.SecretChannelSpec,
    "SecretRef": protocol.SecretRef,
    "Trace": protocol.Trace,
}

_RUN_ID = "RUN-contract-vector"
_TRACE = {"endpoints_contacted": [], "events": [], "metrics": {}}

SIGNATURE_CASES: dict[str, dict[str, str]] = {
    "pure": {
        "in_digest": "sha256:" + "1a" * 32,
        "out_digest": "sha256:" + "2b" * 32,
        "effect": "pure",
    },
    "external-read": {
        "in_digest": "sha256:" + "1a" * 32,
        "out_digest": "sha256:" + "2b" * 32,
        "effect": "external_read",
    },
    "other-out": {
        "in_digest": "sha256:" + "1a" * 32,
        "out_digest": "sha256:" + "3c" * 32,
        "effect": "pure",
    },
}


def _run_context() -> dict[str, Any]:
    context = protocol.RunContext(
        protocol_version=protocol.PROTOCOL_VERSION.render(),
        run_id=_RUN_ID,
        operation="demo.echo",
        operation_signature_digest=operation_signature_digest(**SIGNATURE_CASES["pure"]),  # type: ignore[arg-type]
        build_digest="sha256:" + "4d" * 32,
        implementation_digest="sha256:" + "2b" * 32,
        entrypoint="demo.echo:Echo",
        coordinates={"generation": 7},
        input={"value": "hello"},
        capture_contract=None,
        budgets=protocol.Budgets(wall_clock_seconds=3.0, output_bytes=4096),
        declared_endpoints=("https://example.test",),
        secret_channel=protocol.SecretChannelSpec(
            fd=3, refs=(protocol.SecretRef(ref="operator/demo", purpose="test"),)
        ),
        additive={"future": {"kept": True}},
    )
    return json.loads(context.model_dump_json())


def _result(status: str, **fields: Any) -> dict[str, Any]:
    envelope = protocol.ResultEnvelope(
        protocol_version=protocol.PROTOCOL_VERSION.render(),
        run_id=_RUN_ID,
        status=status,  # type: ignore[arg-type]
        trace=protocol.Trace(**_TRACE),
        **fields,
    )
    return json.loads(envelope.model_dump_json())


def contract() -> dict[str, Any]:
    run_context = _run_context()
    protocol_1_context = {
        key: value
        for key, value in run_context.items()
        if key not in {"operation", "operation_signature_digest", "build_digest"}
    } | {
        "protocol_version": "1.0",
        "interface_id": "demo.echo",
        "interface_digest": "sha256:" + "1a" * 32,
        "input_bucket": "legacy=none",
    }
    implementation_cases = json.loads(
        (GOLDEN / "implementation-cases.json").read_text(encoding="utf-8")
    )["cases"]
    digest_goldens = json.loads((GOLDEN / "expected-digests.json").read_text(encoding="utf-8"))
    return {
        "protocol_version": protocol.PROTOCOL_VERSION.render(),
        "runtime_version": RUNTIME_VERSION,
        "dynamic_endpoint_forms": sorted(DYNAMIC_ENDPOINT_FORMS),
        "runtime_refusal_codes": sorted(RUNTIME_REFUSAL_CODES, key=lambda code: code.encode()),
        "harness_refusal_codes": sorted(HARNESS_REFUSAL_CODES, key=lambda code: code.encode()),
        "operation_refusal_code_pattern": REFUSAL_CODE_RE.pattern,
        "operation_name_pattern": protocol.OPERATION_NAME_RE.pattern,
        "domains": {
            "artifact": ARTIFACT_DOMAIN_TAG,
            "implementation": IMPLEMENTATION_DOMAIN_TAG,
            "manifest": MANIFEST_DOMAIN_TAG,
            "materialization": MATERIALIZATION_DOMAIN_TAG,
            "operation_signature": OPERATION_SIGNATURE_DOMAIN,
        },
        "schemas": {name: model.model_json_schema() for name, model in sorted(_MODELS.items())},
        "valid_vectors": {
            "run_context": run_context,
            "results": {
                "ok": _result("ok", output={"echo": "hello"}),
                "refused_by_operation": _result(
                    "refused",
                    refusal=Refusal(
                        code="too_long", message="declined by fixture", detail={"limit": 3}
                    ),
                ),
                "refused_by_runtime": _result(
                    "refused",
                    refusal=Refusal(
                        code="undeclared_operation",
                        message="refused by fixture",
                        detail={"operation": "demo.other"},
                    ),
                ),
                "error": _result(
                    "error",
                    error=ProviderErrorPayload(kind="FixtureError", message="fixture failure"),
                ),
            },
        },
        "invalid_vectors": {
            "context_unknown_field": {**run_context, "not_additive": True},
            "context_protocol_1": protocol_1_context,
            "result_missing_output": {
                "protocol_version": protocol.PROTOCOL_VERSION.render(),
                "run_id": _RUN_ID,
                "status": "ok",
                "output": None,
                "refusal": None,
                "error": None,
                "trace": _TRACE,
            },
            "result_protocol_1": {
                **_result("ok", output={"echo": "hello"}),
                "protocol_version": "1.0",
            },
        },
        "invocation_outcome_receipt_fields": [
            "build_digest",
            "duration_seconds",
            "dynamic_endpoint_forms",
            "endpoints_contacted",
            "implementation_digest",
            "materialization_digest",
            "operation",
            "operation_signature_digest",
            "protocol_version",
            "status",
        ],
        "implementation_cases": implementation_cases,
        "operation_signature_cases": SIGNATURE_CASES,
        "digest_goldens": {
            **digest_goldens,
            "operation_signature": {
                name: operation_signature_digest(**case)  # type: ignore[arg-type]
                for name, case in sorted(SIGNATURE_CASES.items())
            },
        },
    }


def render() -> bytes:
    document = contract()
    reproduced = {
        name: implementation_digest(**case)
        for name, case in sorted(document["implementation_cases"].items())
    }
    if reproduced != document["digest_goldens"]["implementation"]:
        raise SystemExit("implementation goldens do not reproduce; regenerate them first")
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="exit 1 if the fixture is stale")
    args = parser.parse_args(argv)
    rendered = render()
    if args.check:
        current = TARGET.read_bytes() if TARGET.exists() else b""
        if current != rendered:
            print(f"{TARGET.relative_to(REPO_ROOT)} is stale; rerun without --check")
            return 1
        return 0
    TARGET.write_bytes(rendered)
    print(f"wrote {TARGET.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
