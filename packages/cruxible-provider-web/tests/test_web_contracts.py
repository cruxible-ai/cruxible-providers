"""The Contracts this package ships are the ones its operations pin, and they hold.

``registration.json`` names every Contract by ``name@N``; the package ships each
body under ``contracts/`` as the exact member Core accepts. Three things are
held here, in the default lane and without Core:

* the set: every Contract the registration names is shipped, every nested pin
  resolves to a shipped file by its exact digest, and nothing else is shipped;
* the outputs: what ``web.fetch``, ``search.web``, ``web.render`` and
  ``web.render.dom`` actually return, on the packaged recordings, is exactly
  their result Contract (a ``capture<K>`` input is checked in the digest form
  the Contract types; the operation receives the Capture wire Core builds);
* the refusals: every refusal each operation declares, produced by driving the
  adapter into it, carries a detail that is exactly its detail Contract.

The checker below is a deliberately small reading of Core's Contract grammar,
covering the kinds these files use and failing on any other. Core's own
validator runs over the same files and values in Core's installer test and in
``tests/test_package_registration.py`` (with ``CRUXIBLE_CORE_PYTHON``).
"""

from __future__ import annotations

import base64
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
from cruxible_provider_web.dom import WebRenderDom
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
from cruxible_provider_web.render import WebRender
from cruxible_provider_web.search import SearxngSearch

from .test_web_secrets import _context

CONTRACT_DIGEST_DOMAIN = "cruxible-contract-v1"
ARTICLE_URL = "https://fixture.invalid/articles/tide-gauge-recalibration"
MEASUREMENTS_URL = "https://fixture.invalid/api/v1/measurements.json"
DASHBOARD_URL = "https://fixture.invalid/dashboard"
FIXTURE_INSTANCE = {"instance_url": "https://fixture.invalid"}


def _has_trafilatura() -> bool:
    import importlib.util

    return importlib.util.find_spec("trafilatura") is not None


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


def _pins(field_type: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Every Contract pin a field type carries, a list's item included."""

    if field_type["kind"] == "contract":
        return [field_type["contract"]]
    if field_type["kind"] == "list":
        return _pins(field_type["item"])
    return []


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
    if kind == "capture":
        # A Capture input or output is typed by its digest (Core's check_value);
        # the bytes behind it travel as Capture wire, outside the Contract.
        if not isinstance(value, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None:
            return [f"{at}: not a Capture digest"]
        return []
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
    assert len(named) == 21
    pins = [
        ref
        for document in CONTRACTS.values()
        for field in document["fields"].values()
        for ref in _pins(field["type"])
    ]
    nested = {(ref["name"], ref["version"]) for ref in pins}
    assert named <= set(CONTRACTS)
    assert set(CONTRACTS) == named | nested
    # Every nested pin is the exact digest of the shipped body it names.
    for ref in pins:
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
        {"url": DASHBOARD_URL},
        {
            "url": MEASUREMENTS_URL,
            "query": [
                {"name": "station", "values": ["newlyn"]},
                {"name": "cve", "values": ["CVE-2021-44228", "CVE-2023-4863"], "join": "comma"},
            ],
        },
    ],
    ids=["static", "structured", "unassembled", "query"],
)
def test_fetch_outputs_are_exactly_the_result_contract(payload: dict[str, Any]) -> None:
    _conforms(payload, _operation("web.fetch").in_.spelling)
    result = WebFetch()(_context("web.fetch", input=payload))
    assert result.status == "ok", result
    assert "source_effective_time" not in result.output
    _conforms(result.output, _operation("web.fetch").out.spelling)


def _fetched_wire(url: str) -> dict[str, Any]:
    """The recorded fetch of ``url``, as the Capture wire Core hands a derive."""

    fetched = WebFetch()(_context("web.fetch", input={"url": url}))
    assert fetched.status == "ok", fetched
    return dict(fetched.output)


def _render_payload(form: str, url: str) -> dict[str, Any]:
    """A derive over the recorded fetch of ``url``: its Capture wire is the source."""

    return {"form": form, "source": _fetched_wire(url)}


def _as_typed(payload: dict[str, Any]) -> dict[str, Any]:
    """The payload as its Contract types it: a Capture input is its digest."""

    return {**payload, "source": payload["source"]["bytes_digest"]}


@pytest.mark.skipif(not _has_trafilatura(), reason="web.render needs trafilatura")
@pytest.mark.parametrize("form", ["json", "text"])
def test_render_output_is_exactly_the_result_contract(form: str) -> None:
    payload = _render_payload(form, ARTICLE_URL)
    _conforms(_as_typed(payload), _operation("web.render").in_.spelling)
    result = WebRender()(_context("web.render", input=payload))
    assert result.status == "ok", result
    assert "source_effective_time" not in result.output
    _conforms(result.output, _operation("web.render").out.spelling)


def test_dom_output_is_exactly_the_result_contract() -> None:
    payload = {"url": DASHBOARD_URL}
    _conforms(payload, _operation("web.render.dom").in_.spelling)
    result = WebRenderDom()(_context("web.render.dom", input=payload))
    assert result.status == "ok", result
    assert "source_effective_time" not in result.output
    _conforms(result.output, _operation("web.render.dom").out.spelling)


def test_search_output_is_exactly_the_result_contract() -> None:
    payload = {"query": "tide gauge recalibration", "time_range": "year"}
    _conforms(payload, _operation("search.web").in_.spelling)
    result = SearxngSearch()(
        _context(
            "search.web",
            input=payload,
            coordinates=FIXTURE_INSTANCE,
            declared_endpoints=("dynamic:target-from-configuration",),
        )
    )
    assert result.status == "ok", result
    assert "source_effective_time" not in result.output
    _conforms(result.output, _operation("search.web").out.spelling)


def test_the_request_contracts_refuse_what_the_adapters_decline() -> None:
    fetch_in = CONTRACTS[_spelling(_operation("web.fetch").in_.spelling)]
    search_in = CONTRACTS[_spelling(_operation("search.web").in_.spelling)]
    render_in = CONTRACTS[_spelling(_operation("web.render").in_.spelling)]
    dom_in = CONTRACTS[_spelling(_operation("web.render.dom").in_.spelling)]
    for payload in (
        {},
        {"url": "ftp://fixture.invalid/x"},
        {"url": ARTICLE_URL, "max_bytes": 0},
        {"url": ARTICLE_URL, "max_bytes": MAX_RESPONSE_BYTES + 1},
        {"url": ARTICLE_URL, "expected_format": "xml"},
        {"url": ARTICLE_URL, "render": True},
        {"url": ARTICLE_URL, "extract": True},
        {"url": ARTICLE_URL, "credential_header": "bad header"},
        {"url": ARTICLE_URL, "credential_ref": None},
        {"url": ARTICLE_URL, "logical_source": "https://not-a-name"},
        {"url": ARTICLE_URL, "query": {"cve": ["CVE-2021-44228"]}},
        {"url": ARTICLE_URL, "query": [{"name": "cve", "values": "CVE-2021-44228"}]},
        {"url": ARTICLE_URL, "query": [{"name": "", "values": ["x"]}]},
        {"url": ARTICLE_URL, "query": [{"name": "cve", "values": ["x"], "join": "pipe"}]},
        {"url": ARTICLE_URL, "query": [{"name": "cve", "value": "x"}]},
    ):
        assert _problems(payload, fetch_in), payload
    for payload in (
        {},
        {"query": "   "},
        {"query": "x", "limit": 10},
        {"query": "x", "max_age_hours": 24},
        {"query": "x", "time_range": "week"},
        {"query": "x", "language": ""},
        {"query": "x", "instance_url": "https://fixture.invalid"},
    ):
        assert _problems(payload, search_in), payload
    digest = "sha256:" + "a" * 64
    for payload in (
        {},
        {"form": "text"},
        {"form": "dom", "source": digest},
        {"form": "pdf", "source": digest},
        {"form": "text", "source": {"content_base64": "", "coordinate": {}}},
        {"form": "json", "source": "not-a-digest"},
    ):
        assert _problems(payload, render_in), payload
    assert _problems({"form": "json", "source": digest}, render_in) == []
    for payload in (
        {},
        {"url": "ftp://fixture.invalid/x"},
        {"url": ARTICLE_URL, "form": "dom"},
        {"url": ARTICLE_URL, "source": digest},
        {"url": ARTICLE_URL, "max_bytes": 0},
    ):
        assert _problems(payload, dom_in), payload


# -- the refusals ----------------------------------------------------------


def _declared(operation: str, code: str) -> str:
    refusal = _operation(operation).refusals[code]
    return refusal.contract.spelling


def test_every_declared_refusal_has_a_case_below() -> None:
    declared = {
        (operation, code)
        for operation in ("web.fetch", "search.web", "web.render", "web.render.dom")
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
    """Every input decline and the weight decline."""

    return [
        _fetch({}),
        _fetch({"url": "ftp://fixture.invalid/x"}),
        _fetch({"url": 7}),
        _fetch({"url": ARTICLE_URL, "max_bytes": 0}),
        _fetch({"url": ARTICLE_URL, "expected_format": "xml"}),
        _fetch({"url": ARTICLE_URL, "logical_source": "Not A Name"}),
        _fetch({"url": ARTICLE_URL, "paced": "yes"}),
        _fetch({"url": ARTICLE_URL, "max_bytes": 16}),
        _fetch(
            {"url": ARTICLE_URL, "credential_ref": "api", "credential_header": "bad header"},
            secrets={"api": "token"},
        ),
    ]


def _render(payload: dict[str, Any], **kwargs: Any) -> Any:
    return _called(WebRender(**kwargs), _context("web.render", input=payload))


def _dom(payload: dict[str, Any]) -> Any:
    return _called(WebRenderDom(), _context("web.render.dom", input=payload))


def _render_declined(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Input declines, the weight decline, and an extraction that finds nothing."""

    empty_page = {
        **_fetched_wire(ARTICLE_URL),
    }
    body = b"<html><body></body></html>"
    empty_page.update(
        content_base64=base64.b64encode(body).decode("ascii"),
        byte_length=len(body),
        bytes_digest="sha256:" + hashlib.sha256(body).hexdigest(),
    )
    declined = [
        _render({}),
        _render({"form": "dom", "source": _fetched_wire(ARTICLE_URL)}),
        _render({"form": "text", "source": {"content_base64": "eA=="}}),
        _render(_render_payload("text", MEASUREMENTS_URL)),
        _render(_render_payload("json", MEASUREMENTS_URL)),
    ]
    if _has_trafilatura():
        declined += [
            _render({**_render_payload("text", ARTICLE_URL), "logical_source": "Not A Name"}),
            _render({**_render_payload("text", ARTICLE_URL), "max_bytes": 16}),
            _render({"form": "text", "source": empty_page}),
        ]
    return declined


def _dom_declined(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Input declines, the weight decline, and a recording with no rendered document."""

    return [
        _dom({}),
        _dom({"url": "ftp://fixture.invalid/x"}),
        _dom({"url": DASHBOARD_URL, "logical_source": "Not A Name"}),
        _dom({"url": DASHBOARD_URL, "max_bytes": 16}),
        _dom({"url": ARTICLE_URL}),
    ]


def _dom_engine_unavailable(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    # Importing a module set to None raises ImportError: the environment
    # without the browser extra, whatever this one happens to carry.
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    return [_dom({"url": "https://example.test/page"})]


def _search_declined(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Input and configuration declines; none carries a detail."""

    return [
        _search(coordinates={}),
        _search({"query": " "}),
        _search({"query": "x", "time_range": "week"}),
        _search({"query": "x", "language": ""}),
    ]


REFUSAL_CASES: dict[tuple[str, str], Any] = {
    ("web.fetch", PROVIDER_DECLINED): _fetch_declined,
    ("web.fetch", CREDENTIAL_UNAVAILABLE): lambda monkeypatch: [
        _fetch({"url": ARTICLE_URL, "credential_ref": "web.token"})
    ],
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
    ("web.render", PROVIDER_DECLINED): _render_declined,
    ("web.render.dom", PROVIDER_DECLINED): _dom_declined,
    ("web.render.dom", ENGINE_UNAVAILABLE): _dom_engine_unavailable,
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
