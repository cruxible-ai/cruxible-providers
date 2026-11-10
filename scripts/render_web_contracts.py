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
``FetchRequest.parse`` / ``_query`` (fetch), ``pages._paging`` (fetch.pages),
``_parse_input`` (search),
``_RenderRequest.parse`` (render) and ``dom._parse`` (render.dom) read, the
shared acquisition result ``cruxible_provider_runtime.acquisition`` builds and
the HTTP coordinate every operation passes it (``coordinate.http_coordinate``),
and the detail keys of every refusal each operation raises. An optional field is an absent key;
``null`` is never valid.
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
#: ``fetch.FetchRequest.parse``: an RFC 9110 token names the credential's header.
HEADER_TOKEN = 'string{pattern: "^[!#$%&\'*+.^_`|~0-9A-Za-z-]+$"}'
#: http or https, either case (``urlsplit`` lowercases the scheme it reads).
HTTP_URL = 'string{pattern: "^[Hh][Tt][Tt][Pp][Ss]?://\\\\S+$"}'
#: ``search._parse_input``: a query is more than whitespace.
NON_BLANK = 'string{pattern: "^\\\\s*\\\\S[\\\\s\\\\S]*$"}'
#: ``cruxible_provider_runtime.egress.DYNAMIC_ENDPOINT_FORMS``.
DYNAMIC_FORMS = 'enum["dynamic:target-from-configuration", "dynamic:target-from-run-input"]'
#: An accepted manifest's concrete endpoints a refusal may list.
MAX_LISTED_ENDPOINTS = 256
#: ``fetch.MAX_QUERY_PARAMETERS`` / ``fetch.MAX_QUERY_VALUES``.
MAX_QUERY_PARAMETERS = 64
MAX_QUERY_VALUES = 1000
#: ``fetch.MAX_URL_LENGTH``: the longest request URL the adapters build.
MAX_URL_LENGTH = 8192
#: ``pages.MAX_PAGES``: the most page requests one web.fetch.pages call sends.
MAX_PAGES = 64

ACQUISITION_TAG = "playbill-provider-result-to-external-capture-v1"
EFFECTIVE_TIME_TAG = "playbill-source-effective-time-v1"


def _result(description: str, source_identity: str) -> dict[str, Any]:
    """The shared external-capture result every operation returns."""

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
    """``http._next_hop`` and ``RecordingClient.get``: fetch and search follow redirects."""

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


#: ``addresses.ADDRESS_CLASSES``: every class a private-address refusal names.
_IPV4_ADDRESS_CLASSES = (
    "unspecified",
    "loopback",
    "link_local",
    "private",
    "carrier_grade_nat",
    "multicast",
    "reserved",
)
ADDRESS_CLASS = (
    "enum["
    + ", ".join(
        sorted(
            [
                *_IPV4_ADDRESS_CLASSES,
                "local_name",
                *(f"ipv4_embedded_{name}" for name in _IPV4_ADDRESS_CLASSES),
            ]
        )
    )
    + "]"
)


def _private_address_refusal(operation: str) -> dict[str, dict[str, Any]]:
    """``addresses.AddressGuard``: fetch, fetch.pages and render.dom refuse private targets."""

    return {
        f"{operation}.refusal.private_or_internal_address": {
            "description": (
                "Detail of a private_or_internal_address refusal: the origin of the refused "
                "target (never its path or query), its host, the blocked address when one "
                "was judged (absent for a name refused as local-only), and the class of "
                "address it fell in."
            ),
            "fields": {
                "url": "string",
                "host": "string{min_len: 1}",
                "address": "string?",
                "address_class": ADDRESS_CLASS,
            },
        },
    }


#: ``fetch``/``render`` ``page_weight_class``.
WEIGHT = "enum[heavy, light, medium]"

#: ``fetch.FetchRequest.parse``: the request web.fetch and web.fetch.pages share.
FETCH_REQUEST_FIELDS: dict[str, str] = {
    "url": HTTP_URL,
    "query": f"list<web.fetch.query_param@1>{{max_items: {MAX_QUERY_PARAMETERS}}}?",
    "logical_source": LOGICAL_NAME + "?",
    "max_bytes": "int{1..33554432}?",
    "paced": "bool?",
    "expected_format": "enum[auto, bytes, csv, html, json, text]?",
    "credential_ref": "string?",
    "credential_header": HEADER_TOKEN + "?",
}

_FETCH_DECLINED_FIELDS = {
    "url": "string?",
    "cap_bytes": "int{1..}?",
    "observed_weight": WEIGHT + "?",
}

#: Name to definition, nested Contracts before the Contracts that pin them.
DEFINITIONS: dict[str, dict[str, Any]] = {
    "web.capture.headers": {
        "description": (
            "The response headers a web Capture keeps for freshness, each present only "
            "when the origin sent it."
        ),
        "fields": {
            "etag": "string?",
            "last_modified": "string?",
            "content_length": "string?",
        },
    },
    "web.capture.page": {
        "description": (
            "One page of a listing an operation assembled into one document: its URL, "
            "its 2xx status and its body's digest."
        ),
        "fields": {"url": "string", "status": "int{200..299}", "bytes_digest": SHA256},
    },
    "web.capture.coordinate": {
        "description": (
            "How a web Capture's bytes were obtained, never inside them: the URL "
            "requested and the URL settled on, the 2xx status, the bytes' media type, "
            "the request digest, the kept freshness headers and whether a network or a "
            "packaged recording answered; for a derived document, the engine that made "
            "it and the content digest of the Capture it was made from; for an "
            "assembled listing, its pages."
        ),
        "fields": {
            "url": "string",
            "final_url": "string",
            "status": "int{200..299}",
            "content_type": "string",
            "request_digest": SHA256,
            "source": 'enum["network", "packaged-recording"]',
            "headers": "web.capture.headers@1?",
            "renderer": "string{min_len: 1}?",
            "derived_from": SHA256 + "?",
            "pages": "list<web.capture.page@1>{max_items: 10000}?",
        },
    },
    "web.capture.selector": {
        "description": "The whole document: a web Capture selects no part of it.",
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
    "web.fetch.query_param": {
        "description": (
            "One query parameter of a fetch: its name and its values, each sent as its "
            "own name=value (join: repeat, the default) or comma-joined into one "
            "(join: comma). A parameter has at least one value."
        ),
        "fields": {
            "name": "string{min_len: 1}",
            "values": f"list<string>{{max_items: {MAX_QUERY_VALUES}}}",
            "join": "enum[comma, repeat]?",
        },
    },
    "web.fetch.request": {
        "description": (
            "One resource to retrieve with one HTTP GET. query parameters are encoded "
            "onto url in order, and the built URL is the request its coordinate records. "
            "A credential is named by reference; its material stays in the run's secret "
            "custody, never in this payload."
        ),
        "fields": FETCH_REQUEST_FIELDS,
    },
    "web.fetch.result": _result(
        "One retrieved web resource as Capture wire: the exact body the origin sent, its "
        "HTTP coordinate and the observation time.",
        LOGICAL_NAME,
    ),
    "web.fetch.refusal.credential_unavailable": {
        "description": (
            "Detail of a credential_unavailable refusal: the credential reference the run "
            "named and did not deliver."
        ),
        "fields": {"ref": "string"},
    },
    "web.fetch.refusal.provider_declined": {
        "description": (
            "Detail of a provider_declined refusal: the URL when one was named; for a "
            "weight refusal, the cap and the observed weight class."
        ),
        "fields": _FETCH_DECLINED_FIELDS,
    },
    **_redirect_refusals("web.fetch"),
    **_private_address_refusal("web.fetch"),
    "web.fetch.pages.failure": {
        "description": (
            "One page of a web.fetch.pages call that has no Capture: its 0-based index "
            "and the page count, its built URL and request digest, whether it was "
            "refused or failed, the refusal code or error kind, the message, and the "
            "origin's status when it answered outside 2xx."
        ),
        "fields": {
            "page_index": f"int{{0..{MAX_PAGES - 1}}}",
            "page_count": f"int{{1..{MAX_PAGES}}}",
            "url": "string",
            "request_digest": SHA256,
            "outcome": "enum[failed, refused]",
            "code": "string{min_len: 1}",
            "message": "string",
            "status_code": "int{100..599}?",
        },
    },
    "web.fetch.pages.request": {
        "description": (
            "web.fetch's request, sent as pages: the values of the query parameter "
            "page_param are split, in order, across as few requests as keep each built "
            f"URL within max_url_chars (default {MAX_URL_LENGTH}), and every other "
            "parameter is repeated on each page. The split is a function of the request "
            "alone; every value is sent on exactly one page. The page_param parameter may "
            "have no values: then no request is sent and captures is empty."
        ),
        "fields": {
            **FETCH_REQUEST_FIELDS,
            "page_param": "string{min_len: 1}",
            "max_url_chars": f"int{{1..{MAX_URL_LENGTH}}}?",
        },
    },
    "web.fetch.pages.result": {
        "description": (
            "One Capture per page, in page order: each member is the exact body that "
            "page's origin sent, with web.fetch's HTTP coordinate for that exchange "
            "(url is the page's built URL, request_digest covers it) plus page_index "
            "(0-based) and page_count. A page without a Capture is listed in "
            "failed_pages, and the result is then incomplete (D-R37 a4): absence never "
            "retires on it. A request with no values to page over has no pages: captures "
            "is empty and the result is complete."
        ),
        "fields": {
            "captures": f"list<capture<*>>{{max_items: {MAX_PAGES}}}",
            "failed_pages": f"list<web.fetch.pages.failure@1>{{max_items: {MAX_PAGES}}}?",
        },
    },
    "web.fetch.pages.refusal.credential_unavailable": {
        "description": (
            "Detail of a credential_unavailable refusal: the credential reference the run "
            "named and did not deliver."
        ),
        "fields": {"ref": "string"},
    },
    "web.fetch.pages.refusal.provider_declined": {
        "description": (
            "Detail of a provider_declined refusal: the URL when one was named; for a "
            "single page's weight refusal, the cap and the observed weight class."
        ),
        "fields": _FETCH_DECLINED_FIELDS,
    },
    **_redirect_refusals("web.fetch.pages"),
    **_private_address_refusal("web.fetch.pages"),
    "web.render.request": {
        "description": (
            "A derive over one Capture this run fetched (an HTML document): text is its "
            "main content as Markdown; json is canonical JSON {url, final_url, title?, "
            "text, links}. Pure: it reads only the Capture's bytes and contacts nothing."
        ),
        "fields": {
            "source": "capture<*>",
            "form": "enum[json, text]",
            "logical_source": LOGICAL_NAME + "?",
            "max_bytes": "int{1..33554432}?",
        },
    },
    "web.render.result": _result(
        "One derived web document as Capture wire: the exact bytes the derive produced "
        "(main-content Markdown or the page's canonical JSON), a coordinate naming the "
        "engine and the content digest of the Capture it was made from, and that "
        "Capture's observation time.",
        LOGICAL_NAME,
    ),
    "web.render.refusal.provider_declined": {
        "description": (
            "Detail of a provider_declined refusal: the URL of the cited document when "
            "known; for a weight refusal, the cap and the observed weight class; for an "
            "extraction refusal, the extracting engine."
        ),
        "fields": {
            "url": "string?",
            "cap_bytes": "int{1..}?",
            "observed_weight": WEIGHT + "?",
            "engine": "string?",
        },
    },
    "web.render.dom.request": {
        "description": (
            "One page to assemble in a browser: the browser navigates to url, runs the "
            "page's scripts and pulls its subresources, and the serialized DOM is the "
            "Capture."
        ),
        "fields": {
            "url": HTTP_URL,
            "logical_source": LOGICAL_NAME + "?",
            "max_bytes": "int{1..33554432}?",
        },
    },
    "web.render.dom.result": _result(
        "One browser-assembled page as Capture wire: the exact serialized DOM, an HTTP "
        "coordinate naming the main-frame exchange and the engine that assembled it, "
        "and the observation time.",
        LOGICAL_NAME,
    ),
    "web.render.dom.refusal.engine_unavailable": {
        "description": (
            "Detail of an engine_unavailable refusal: the Python extra the environment "
            "lacks and the engine that needs it."
        ),
        "fields": {"required_extra": 'enum["browser"]', "engine": "string"},
    },
    "web.render.dom.refusal.provider_declined": {
        "description": (
            "Detail of a provider_declined refusal: the page URL when one was named; for "
            "a weight refusal, the cap and the observed weight class; for a navigation "
            "without a response, the engine."
        ),
        "fields": {
            "url": "string?",
            "cap_bytes": "int{1..}?",
            "observed_weight": WEIGHT + "?",
            "engine": "string?",
        },
    },
    **_private_address_refusal("web.render.dom"),
    "search.web.request": {
        "description": (
            "One query to the configured search instance. The instance is run "
            "configuration, not a request field; time_range is the instance's own "
            "recency filter."
        ),
        "fields": {
            "query": NON_BLANK,
            "language": "string{min_len: 1}?",
            "time_range": "enum[day, month, year]?",
        },
    },
    "search.web.result": _result(
        "One search answer as Capture wire: the exact response body the instance sent, "
        "its HTTP coordinate and the observation time.",
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


def render(definitions: dict[str, dict[str, Any]] | None = None) -> dict[str, bytes]:
    """Every Contract's relative path under ``contracts/`` to its canonical bytes.

    ``definitions`` defaults to the web plane's; another package's script passes
    its own (nested Contracts before the Contracts that pin them).
    """

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
    for name, definition in (DEFINITIONS if definitions is None else definitions).items():
        contract = pin_contract_refs(
            Contract.model_validate({"name": name, "version": 1, **definition}), pin
        )
        rendered[name] = contract
        path = contract_path(name, 1)
        files[path.removeprefix("contracts/")] = render_contract(contract)
    return files


def write(
    target: Path,
    files: dict[str, bytes],
    *,
    check: bool,
    script: str,
) -> int:
    """Rewrite ``target`` to exactly ``files`` (or, with ``check``, report drift)."""

    present = {
        str(path.relative_to(target)): path.read_bytes() for path in sorted(target.rglob("*.json"))
    }
    if check:
        if present != files:
            print(f"Contracts drifted under {target}; run {script}", file=sys.stderr)
            return 1
        return 0
    for stale in set(present) - set(files):
        (target / stale).unlink()
        if not any((target / stale).parent.iterdir()):
            (target / stale).parent.rmdir()
    for relative, content in files.items():
        path = target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="exit 1 if the files drifted")
    args = parser.parse_args(argv)
    return write(TARGET, render(), check=args.check, script="scripts/render_web_contracts.py")


if __name__ == "__main__":
    raise SystemExit(main())
