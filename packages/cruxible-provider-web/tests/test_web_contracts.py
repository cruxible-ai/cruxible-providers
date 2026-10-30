"""The Contracts this package ships are the ones its operations pin, and they hold.

``registration.json`` names every Contract by ``name@N``; the package ships each
body under ``contracts/`` as the exact member Core accepts. Three things are
held here, in the default lane and without Core:

* the set: every Contract the registration names is shipped, every nested pin
  resolves to a shipped file by its exact digest, and nothing else is shipped;
* the outputs: what ``web.fetch`` and ``search.web`` actually return, on the
  packaged recordings, is exactly their result Contract;
* the refusals: every refusal each operation declares, produced by driving the
  adapter into it, carries a detail that is exactly its detail Contract.

The checker below is a deliberately small reading of Core's Contract grammar,
covering the kinds these files use and failing on any other. Core's own
validator runs over the same files and values in Core's installer test and in
``tests/test_package_registration.py`` (with ``CRUXIBLE_CORE_PYTHON``).
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from cruxible_provider_runtime.canonical import canonical_json
from cruxible_provider_runtime.errors import Declined
from cruxible_provider_runtime.provider_api import ProviderResult
from cruxible_provider_runtime.registration import load_registration
from cruxible_provider_web import CONTRACTS_DIR, PACKAGE_ROOT, contract_members
from cruxible_provider_web.fetch import MAX_RESPONSE_BYTES, WebFetch
from cruxible_provider_web.http import RecordingClient
from cruxible_provider_web.operations import (
    CREDENTIAL_UNAVAILABLE,
    CROSS_ORIGIN_CREDENTIALED_REDIRECT,
    ENGINE_UNAVAILABLE,
    PROVIDER_DECLINED,
    REDIRECT_LIMIT,
    UNDECLARED_INSTANCE,
    UNSUPPORTED_REDIRECT_SCHEME,
)
from cruxible_provider_web.search import SearxngSearch

from .test_web_secrets import _CapturingClient, _context

CONTRACT_DIGEST_DOMAIN = "cruxible-contract-v1"
ARTICLE_URL = "https://fixture.invalid/articles/tide-gauge-recalibration"
MEASUREMENTS_URL = "https://fixture.invalid/api/v1/measurements.json"
DASHBOARD_URL = "https://fixture.invalid/dashboard"
FIXTURE_INSTANCE = {"instance_url": "https://fixture.invalid"}


def _contracts() -> dict[tuple[str, int], dict[str, Any]]:
    found = {}
    for path, content in contract_members().items():
        document = json.loads(content)
        assert path == f"contracts/{document['name']}/v{document['version']}.json"
        found[(document["name"], document["version"])] = document
    return found


CONTRACTS = _contracts()


def _digest(document: Mapping[str, Any]) -> str:
    """Core's Contract digest: the domain tag beside the document, canonical JSON."""

    payload = canonical_json({"tag": CONTRACT_DIGEST_DOMAIN, **document})
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _spelling(name: str) -> tuple[str, int]:
    contract, _, version = name.partition("@")
    return contract, int(version)


def _type_problems(value: Any, field_type: Mapping[str, Any], at: str) -> list[str]:
    kind = field_type["kind"]
    if kind == "string":
        if not isinstance(value, str):
            return [f"{at}: not a string"]
        pattern = field_type.get("pattern")
        if pattern is not None and re.fullmatch(pattern, value) is None:
            return [f"{at}: {value!r} does not match {pattern}"]
        if len(value) < field_type.get("min_len", 0):
            return [f"{at}: shorter than min_len"]
        if "max_len" in field_type and len(value) > field_type["max_len"]:
            return [f"{at}: longer than max_len"]
        return []
    if kind == "int":
        if not isinstance(value, int) or isinstance(value, bool):
            return [f"{at}: not an int"]
        if "min" in field_type and value < field_type["min"]:
            return [f"{at}: {value} below {field_type['min']}"]
        if "max" in field_type and value > field_type["max"]:
            return [f"{at}: {value} above {field_type['max']}"]
        return []
    if kind == "bool":
        return [] if isinstance(value, bool) else [f"{at}: not a bool"]
    if kind == "datetime":
        if not isinstance(value, str):
            return [f"{at}: not a datetime string"]
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if moment.utcoffset() is None:
            return [f"{at}: a datetime without an offset"]
        return []
    if kind == "enum":
        return [] if value in field_type["members"] else [f"{at}: {value!r} is not a member"]
    if kind == "list":
        if not isinstance(value, list):
            return [f"{at}: not a list"]
        if len(value) > field_type["max_items"]:
            return [f"{at}: more than {field_type['max_items']} items"]
        problems = []
        for index, item in enumerate(value):
            problems += _type_problems(item, field_type["item"], f"{at}[{index}]")
        return problems
    if kind == "contract":
        ref = field_type["contract"]
        nested = CONTRACTS.get((ref["name"], ref["version"]))
        assert nested is not None and _digest(nested) == ref["digest"], ref
        return _problems(value, nested, at)
    raise AssertionError(f"{at}: kind {kind!r} is outside what this checker reads")


def _problems(value: Any, contract: Mapping[str, Any], at: str = "") -> list[str]:
    """Every way ``value`` is not exactly ``contract``: closed, required, typed."""

    if not isinstance(value, Mapping):
        return [f"{at or '/'}: not a record"]
    fields: Mapping[str, Any] = contract["fields"]
    problems = [f"{at}/{key}: undeclared field" for key in value if key not in fields]
    for name, declared in fields.items():
        if name not in value:
            if not declared.get("optional", False):
                problems.append(f"{at}/{name}: absent_required")
            continue
        problems += _type_problems(value[name], declared["type"], f"{at}/{name}")
    return problems


def _conforms(value: Any, spelling: str) -> None:
    problems = _problems(value, CONTRACTS[_spelling(spelling)])
    assert not problems, (spelling, problems)


def _operation(name: str) -> Any:
    registration = load_registration(PACKAGE_ROOT)
    return next(item for item in registration.operations if item.name == name)


def _refusal_detail(result: Any, code: str) -> dict[str, Any]:
    assert result.status == "refused", result
    assert result.refusal is not None and result.refusal.code == code, result.refusal
    return dict(result.refusal.detail)


def _routed(routes: Mapping[str, httpx.Response]) -> Any:
    """A client factory whose RecordingClient answers from ``routes`` by host."""

    def handle(request: httpx.Request) -> httpx.Response:
        answer = routes[request.url.host]
        return httpx.Response(
            status_code=answer.status_code,
            headers=answer.headers,
            content=answer.content,
            request=request,
        )

    def factory(recorder: Any, *, url: str, timeout_seconds: float) -> RecordingClient:
        del url
        return RecordingClient(
            recorder, timeout_seconds=timeout_seconds, transport=httpx.MockTransport(handle)
        )

    return factory


def _redirect(location: str) -> httpx.Response:
    return httpx.Response(status_code=302, headers={"location": location})


# -- the set ---------------------------------------------------------------


def test_the_package_ships_exactly_the_contracts_its_registration_pins() -> None:
    registration = load_registration(PACKAGE_ROOT)
    named = {
        (ref.name, ref.version)
        for operation in registration.operations
        for ref in operation.contract_refs()
    }
    assert len(named) == 15
    nested = {
        (field["type"]["contract"]["name"], field["type"]["contract"]["version"])
        for document in CONTRACTS.values()
        for field in document["fields"].values()
        if field["type"]["kind"] == "contract"
    }
    assert named <= set(CONTRACTS)
    assert set(CONTRACTS) == named | nested
    # Every nested pin is the exact digest of the shipped body it names.
    for document in CONTRACTS.values():
        for field in document["fields"].values():
            if field["type"]["kind"] == "contract":
                ref = field["type"]["contract"]
                assert ref["digest"] == _digest(CONTRACTS[(ref["name"], ref["version"])])


def test_every_shipped_file_is_canonical_and_lives_at_its_tree_path() -> None:
    for path in CONTRACTS_DIR.rglob("*.json"):
        content = path.read_bytes()
        document = json.loads(content)
        assert path.relative_to(CONTRACTS_DIR) == Path(
            document["name"], f"v{document['version']}.json"
        )
        assert document["artifact_format"] == "cruxible-contract-v1"
        assert document["version"] == 1 and "predecessor_digest" not in document
        rendered = json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        assert content == rendered.encode("utf-8")


def test_the_empty_selector_and_detail_are_zero_field_contracts() -> None:
    assert CONTRACTS[("web.capture.selector", 1)]["fields"] == {}
    assert CONTRACTS[("search.web.refusal.provider_declined", 1)]["fields"] == {}
    assert _problems({}, CONTRACTS[("web.capture.selector", 1)]) == []
    assert _problems({"part": "body"}, CONTRACTS[("web.capture.selector", 1)]) == [
        "/part: undeclared field"
    ]


# -- the outputs -----------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"url": ARTICLE_URL},
        {"url": MEASUREMENTS_URL, "expected_format": "json", "logical_source": "tide.readings"},
        {"url": DASHBOARD_URL, "render": True},
    ],
    ids=["static", "structured", "recorded-render"],
)
def test_fetch_outputs_are_exactly_the_result_contract(payload: dict[str, Any]) -> None:
    _conforms(payload, _operation("web.fetch").in_.spelling)
    result = WebFetch()(_context("web.fetch", input=payload))
    assert result.status == "ok", result
    assert "source_effective_time" not in result.output
    _conforms(result.output, _operation("web.fetch").out.spelling)


def test_search_output_is_exactly_the_result_contract() -> None:
    payload = {"query": "tide gauge recalibration", "limit": 2, "max_age_hours": 24 * 365}
    _conforms(payload, _operation("search.web").in_.spelling)
    result = SearxngSearch()(
        _context(
            "search.web",
            input=payload,
            coordinates={**FIXTURE_INSTANCE, "as_of": "2026-06-01T00:00:00Z"},
            declared_endpoints=("dynamic:target-from-configuration",),
        )
    )
    assert result.status == "ok", result
    assert "source_effective_time" not in result.output
    _conforms(result.output, _operation("search.web").out.spelling)


def test_the_request_contracts_refuse_what_the_adapters_decline() -> None:
    fetch_in = CONTRACTS[_spelling(_operation("web.fetch").in_.spelling)]
    search_in = CONTRACTS[_spelling(_operation("search.web").in_.spelling)]
    for payload in (
        {},
        {"url": "ftp://fixture.invalid/x"},
        {"url": ARTICLE_URL, "max_bytes": 0},
        {"url": ARTICLE_URL, "max_bytes": MAX_RESPONSE_BYTES + 1},
        {"url": ARTICLE_URL, "expected_format": "xml"},
        {"url": ARTICLE_URL, "render": "yes"},
        {"url": ARTICLE_URL, "credential_header": "bad header"},
        {"url": ARTICLE_URL, "credential_ref": None},
        {"url": ARTICLE_URL, "logical_source": "https://not-a-name"},
    ):
        assert _problems(payload, fetch_in), payload
    for payload in (
        {},
        {"query": "   "},
        {"query": "x", "limit": 0},
        {"query": "x", "max_age_hours": 0},
        {"query": "x", "language": ""},
        {"query": "x", "instance_url": "https://fixture.invalid"},
    ):
        assert _problems(payload, search_in), payload


# -- the refusals ----------------------------------------------------------


def _declared(operation: str, code: str) -> str:
    refusal = _operation(operation).refusals[code]
    return refusal.contract.spelling


def test_every_declared_refusal_has_a_case_below() -> None:
    declared = {
        (operation, code)
        for operation in ("web.fetch", "search.web")
        for code in _operation(operation).refusals
    }
    assert set(REFUSAL_CASES) == declared


def _called(adapter: Any, context: Any) -> Any:
    """The adapter's result, with a raised refusal taken as the harness takes it.

    Provider code may raise ``Declined`` from deep in a call (an extractor, the
    redirect walk); the child harness returns it as the refusal it carries.
    """

    try:
        return adapter(context)
    except Declined as exc:
        return ProviderResult(status="refused", refusal=exc.refusal)


def _fetch(payload: dict[str, Any], *, secrets: Any = None, client_factory: Any = None) -> Any:
    adapter = WebFetch() if client_factory is None else WebFetch(client_factory=client_factory)
    return _called(adapter, _context("web.fetch", input=payload, secrets=secrets or {}))


def _search(
    payload: dict[str, Any] | None = None,
    *,
    coordinates: dict[str, Any] | None = None,
    secrets: Any = None,
    client_factory: Any = None,
) -> Any:
    adapter = (
        SearxngSearch() if client_factory is None else SearxngSearch(client_factory=client_factory)
    )
    return _called(
        adapter,
        _context(
            "search.web",
            input={"query": "tide gauge"} if payload is None else payload,
            coordinates=FIXTURE_INSTANCE if coordinates is None else coordinates,
            declared_endpoints=("https://fixture.invalid",),
            secrets=secrets or {},
        ),
    )


def _fetch_declined(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Every input decline, the weight and extraction declines, and a recording
    that carries no rendered document."""

    empty_page = _CapturingClient("<html><body></body></html>", "text/html")
    return [
        _fetch({}),
        _fetch({"url": "ftp://fixture.invalid/x"}),
        _fetch({"url": 7}),
        _fetch({"url": ARTICLE_URL, "max_bytes": 0}),
        _fetch({"url": ARTICLE_URL, "expected_format": "xml"}),
        _fetch({"url": ARTICLE_URL, "logical_source": "Not A Name"}),
        _fetch({"url": ARTICLE_URL, "render": "yes"}),
        _fetch({"url": ARTICLE_URL, "max_bytes": 16}),
        _fetch({"url": MEASUREMENTS_URL, "render": True}),
        _fetch(
            {"url": "https://example.test/page"},
            client_factory=lambda recorder, **kwargs: empty_page,
        ),
        _fetch(
            {"url": ARTICLE_URL, "credential_ref": "api", "render": True},
            secrets={"api": "token"},
        ),
    ]


def _fetch_engine_unavailable(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    # Importing a module set to None raises ImportError: the environment
    # without the browser extra, whatever this one happens to carry.
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    return [_fetch({"url": "https://example.test/page", "render": True})]


def _search_declined(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Input and configuration declines; none carries a detail."""

    return [
        _search(coordinates={}),
        _search({"query": " "}),
        _search({"query": "x", "limit": 0}),
        _search({"query": "x", "max_age_hours": -1}),
        _search({"query": "x", "language": ""}),
        _search(coordinates={**FIXTURE_INSTANCE, "as_of": "yesterday"}),
    ]


REFUSAL_CASES: dict[tuple[str, str], Any] = {
    ("web.fetch", PROVIDER_DECLINED): _fetch_declined,
    ("web.fetch", CREDENTIAL_UNAVAILABLE): lambda monkeypatch: [
        _fetch({"url": ARTICLE_URL, "credential_ref": "web.token"})
    ],
    ("web.fetch", ENGINE_UNAVAILABLE): _fetch_engine_unavailable,
    ("web.fetch", CROSS_ORIGIN_CREDENTIALED_REDIRECT): lambda monkeypatch: [
        _fetch(
            {"url": "https://a.example/doc", "credential_ref": "api"},
            secrets={"api": "token"},
            client_factory=_routed({"a.example": _redirect("https://b.example/")}),
        )
    ],
    ("web.fetch", REDIRECT_LIMIT): lambda monkeypatch: [
        _fetch(
            {"url": "https://a.example/doc"},
            client_factory=_routed({"a.example": _redirect("https://a.example/doc")}),
        )
    ],
    ("web.fetch", UNSUPPORTED_REDIRECT_SCHEME): lambda monkeypatch: [
        _fetch(
            {"url": "https://a.example/doc"},
            client_factory=_routed({"a.example": _redirect("ftp://a.example/doc")}),
        )
    ],
    ("search.web", PROVIDER_DECLINED): _search_declined,
    ("search.web", UNDECLARED_INSTANCE): lambda monkeypatch: [
        _search(coordinates={"instance_url": "https://other.example"})
    ],
    ("search.web", CROSS_ORIGIN_CREDENTIALED_REDIRECT): lambda monkeypatch: [
        _search(
            secrets={"search.web.instance_credential": "token"},
            client_factory=_routed({"fixture.invalid": _redirect("https://elsewhere.example/")}),
        )
    ],
    ("search.web", REDIRECT_LIMIT): lambda monkeypatch: [
        _search(
            client_factory=_routed({"fixture.invalid": _redirect("https://fixture.invalid/search")})
        )
    ],
    ("search.web", UNSUPPORTED_REDIRECT_SCHEME): lambda monkeypatch: [
        _search(client_factory=_routed({"fixture.invalid": _redirect("ftp://fixture.invalid/")}))
    ],
}


@pytest.mark.parametrize(
    ("operation", "code"), list(REFUSAL_CASES), ids=[":".join(key) for key in REFUSAL_CASES]
)
def test_every_refusal_detail_is_exactly_its_declared_contract(
    operation: str, code: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    results = REFUSAL_CASES[(operation, code)](monkeypatch)
    assert results
    for result in results:
        _conforms(_refusal_detail(result, code), _declared(operation, code))
