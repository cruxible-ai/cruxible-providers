#!/usr/bin/env python
"""Write the Contracts the web plane's operations pin, as Core accepts them.

``cruxible-provider-web`` ships the bodies of every Contract its registration
names -- each operation's input and output, every declared refusal's detail --
and of the nested Contracts those pin. Each file is the exact accepted-tree
member Core stores at ``contracts/<name>/v<N>.json`` (canonical JSON, every
nested pin carrying its digest), so an operator accepts them as ordinary members
before installing the package, and the install pins its operations to them.

Rendering uses Core's own Contract model, so this script runs in an interpreter
that has ``cruxible_client`` (Core's environment)::

    "$CRUXIBLE_CORE_PYTHON" scripts/render_web_contracts.py          # rewrite
    "$CRUXIBLE_CORE_PYTHON" scripts/render_web_contracts.py --check  # exit 1 on drift

The definitions below are derived from the adapters' code: the request fields
``_Request.parse`` (fetch) and ``_parse_input`` (search) read, the shared
acquisition result ``cruxible_provider_runtime.acquisition`` builds and the
coordinate both adapters pass it, and the detail keys of every refusal each
operation raises. An optional field is an absent key; ``null`` is never valid.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
TARGET = REPO_ROOT / "packages/cruxible-provider-web/src/cruxible_provider_web/contracts"

SHA256 = 'string{pattern: "^sha256:[0-9a-f]{64}$"}'
LOGICAL_NAME = 'string{pattern: "^[a-z][a-z0-9_.-]{0,255}$"}'
#: ``fetch._Request.parse``: an RFC 9110 token names the credential's header.
HEADER_TOKEN = 'string{pattern: "^[!#$%&\'*+.^_`|~0-9A-Za-z-]+$"}'
#: http or https, either case (``urlsplit`` lowercases the scheme it reads).
HTTP_URL = 'string{pattern: "^[Hh][Tt][Tt][Pp][Ss]?://\\\\S+$"}'
#: ``search._parse_input``: a query is more than whitespace.
NON_BLANK = 'string{pattern: "^\\\\s*\\\\S[\\\\s\\\\S]*$"}'
#: ``cruxible_provider_runtime.egress.DYNAMIC_ENDPOINT_FORMS``.
DYNAMIC_FORMS = 'enum["dynamic:target-from-configuration", "dynamic:target-from-run-input"]'
#: An accepted manifest's concrete endpoints a refusal may list.
MAX_LISTED_ENDPOINTS = 256

ACQUISITION_TAG = "playbill-provider-result-to-external-capture-v1"
EFFECTIVE_TIME_TAG = "playbill-source-effective-time-v1"


def _result(description: str, source_identity: str) -> dict[str, Any]:
    """The shared external-capture result both operations return."""

    return {
        "description": description,
        "fields": {
            "tag": f'enum["{ACQUISITION_TAG}"]',
            "source_identity": source_identity,
            "coordinate_type": 'enum["http-response-v1"]',
            "coordinate": "web.capture.coordinate@1",
            "selector_type": 'enum["whole-response-v1"]',
            "selector": "web.capture.selector@1",
            "replayability": 'enum["attested_only"]',
            "content_encoding": 'enum["base64"]',
            "content_base64": "string",
            "byte_length": "int{0..}",
            "bytes_digest": SHA256,
            "observed_at": "datetime",
            "source_effective_time": "web.capture.source_effective_time@1?",
        },
    }


def _redirect_refusals(operation: str) -> dict[str, dict[str, Any]]:
    """``http._next_hop`` and ``RecordingClient.get``: both operations follow redirects."""

    return {
        f"{operation}.refusal.cross_origin_credentialed_redirect": {
            "description": (
                "Detail of a cross_origin_credentialed_redirect refusal: the origin an "
                "authenticated request was at, and the different origin it was sent to."
            ),
            "fields": {"url": "string", "redirect_to": "string"},
        },
        f"{operation}.refusal.redirect_limit": {
            "description": (
                "Detail of a redirect_limit refusal: the requested URL, the hop it would "
                "have followed next, and the hop limit."
            ),
            "fields": {"url": "string", "next_url": "string", "max_redirects": "int{1..}"},
        },
        f"{operation}.refusal.unsupported_redirect_scheme": {
            "description": (
                "Detail of an unsupported_redirect_scheme refusal: the URL that redirected "
                "and the non-http(s) URL it named."
            ),
            "fields": {"url": "string", "redirect_to": "string"},
        },
    }


#: Name to definition, nested Contracts before the Contracts that pin them.
DEFINITIONS: dict[str, dict[str, Any]] = {
    "web.capture.coordinate": {
        "description": (
            "The HTTP exchange a web acquisition observed: the request's digest, the 2xx "
            "status, the body's digest, and whether a network or a packaged recording "
            "served it."
        ),
        "fields": {
            "request_digest": SHA256,
            "status": "int{200..299}",
            "body_sha256": SHA256,
            "source": 'enum["network", "packaged-recording"]',
        },
    },
    "web.capture.selector": {
        "description": "The whole response: a web acquisition selects no part of it.",
        "fields": {},
    },
    "web.capture.source_effective_time": {
        "description": "The half-open interval a web observation is valid for.",
        "fields": {
            "tag": f'enum["{EFFECTIVE_TIME_TAG}"]',
            "effective_from": "datetime",
            "effective_until": "datetime?",
        },
    },
    "web.fetch.request": {
        "description": (
            "One resource to retrieve. A credential is named by reference; its material "
            "stays in the run's secret custody, never in this payload."
        ),
        "fields": {
            "url": HTTP_URL,
            "logical_source": LOGICAL_NAME + "?",
            "max_bytes": "int{1..33554432}?",
            "render": "bool?",
            "extract": "bool?",
            "paced": "bool?",
            "expected_format": "enum[auto, bytes, csv, html, json, text]?",
            "credential_ref": "string?",
            "credential_header": HEADER_TOKEN + "?",
        },
    },
    "web.fetch.result": _result(
        "One retrieved web resource as Capture wire: the observation bundle's exact bytes "
        "(retrieved exchange and derived reading), its coordinate and the observation time.",
        LOGICAL_NAME,
    ),
    "web.fetch.refusal.credential_unavailable": {
        "description": (
            "Detail of a credential_unavailable refusal: the credential reference the run "
            "named and did not deliver."
        ),
        "fields": {"ref": "string"},
    },
    "web.fetch.refusal.engine_unavailable": {
        "description": (
            "Detail of an engine_unavailable refusal: the Python extra the environment "
            "lacks and the engine that needs it."
        ),
        "fields": {"required_extra": 'enum["browser"]', "engine": "string"},
    },
    "web.fetch.refusal.provider_declined": {
        "description": (
            "Detail of a provider_declined refusal: the URL when one was named; for a "
            "weight refusal, the cap and the observed weight class; for an extraction "
            "refusal, the extracting engine."
        ),
        "fields": {
            "url": "string?",
            "cap_bytes": "int{1..}?",
            "observed_weight": "enum[heavy, light, medium]?",
            "engine": "string?",
        },
    },
    **_redirect_refusals("web.fetch"),
    "search.web.request": {
        "description": (
            "One query to the configured search instance. The instance and the evaluation "
            "instant are run configuration, not request fields."
        ),
        "fields": {
            "query": NON_BLANK,
            "limit": "int{1..}?",
            "max_age_hours": "int{1..}?",
            "language": "string{min_len: 1}?",
        },
    },
    "search.web.result": _result(
        "One search answer as Capture wire: the observation bundle's exact bytes (the "
        "instance's answer and the derived recency-filtered ranking), its coordinate and "
        "the observation time.",
        'enum["search.response"]',
    ),
    "search.web.refusal.provider_declined": {
        "description": (
            "Detail of a provider_declined refusal: none; the message names the input "
            "or configuration the adapter declined."
        ),
        "fields": {},
    },
    "search.web.refusal.undeclared_instance": {
        "description": (
            "Detail of an undeclared_instance refusal: the implementation, the instance "
            "origin the run named, and what the accepted declaration carries."
        ),
        "fields": {
            "implementation_digest": SHA256,
            "instance": "string",
            "declared": f"list<string>{{max_items: {MAX_LISTED_ENDPOINTS}}}",
            "dynamic_forms": f"list<{DYNAMIC_FORMS}>{{max_items: 2}}",
        },
    },
    **_redirect_refusals("search.web"),
}


def render() -> dict[str, bytes]:
    """Every Contract's relative path under ``contracts/`` to its canonical bytes."""

    from cruxible_client.contracts.shapes import (
        Contract,
        contract_digest,
        contract_path,
        pin_contract_refs,
        render_contract,
    )

    rendered: dict[str, Contract] = {}

    def pin(ref: Any) -> Any:
        target = rendered.get(ref.name)
        if target is None or target.version != ref.version:
            raise SystemExit(f"{ref.spelling} is pinned before it is defined")
        return ref.model_copy(update={"digest": contract_digest(target).tagged})

    files: dict[str, bytes] = {}
    for name, definition in DEFINITIONS.items():
        contract = pin_contract_refs(
            Contract.model_validate({"name": name, "version": 1, **definition}), pin
        )
        rendered[name] = contract
        path = contract_path(name, 1)
        files[path.removeprefix("contracts/")] = render_contract(contract)
    return files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="exit 1 if the files drifted")
    args = parser.parse_args(argv)
    files = render()
    present = {
        str(path.relative_to(TARGET)): path.read_bytes() for path in sorted(TARGET.rglob("*.json"))
    }
    if args.check:
        if present != files:
            print("web Contracts drifted; run scripts/render_web_contracts.py", file=sys.stderr)
            return 1
        return 0
    for stale in set(present) - set(files):
        (TARGET / stale).unlink()
    for relative, content in files.items():
        path = TARGET / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
