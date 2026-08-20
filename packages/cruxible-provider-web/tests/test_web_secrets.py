"""Credential handling for the web plane.

The runtime's suite proves the delivery channel and the redactor. What is
plane-specific, and therefore tested here, is what the *adapter* does with the
material once it has it: put it on a request header, and nowhere else — not in
the output, not in the trace, not in the parameters it records, and not in the
query string, where it would end up in the instance's access log.
"""

from __future__ import annotations

from typing import Any

import pytest
from cruxible_provider_runtime.egress import EgressRecorder
from cruxible_provider_runtime.errors import RefusalCode
from cruxible_provider_runtime.protocol import Budgets
from cruxible_provider_runtime.provider_api import ProviderRunContext
from cruxible_provider_web.fetch import WebFetch
from cruxible_provider_web.http import HttpResponse, RecordingClient
from cruxible_provider_web.search import CREDENTIAL_REF, SearxngSearch

BUDGETS = Budgets(wall_clock_seconds=30.0, output_bytes=1_000_000)
CREDENTIAL = "dummy-instance-token-c0ffee-do-not-use"
INSTANCE_ANSWER = '{"query": "x", "number_of_results": 0, "results": []}'


class _CapturingClient:
    """Records the headers and URL it was given, and answers from a canned body."""

    def __init__(self, body: str, content_type: str) -> None:
        self.body = body
        self.content_type = content_type
        self.urls: list[str] = []
        self.headers: list[dict[str, str]] = []

    def __enter__(self) -> _CapturingClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def get(self, url: str, *, headers: Any = None, cap_bytes: int) -> HttpResponse:
        del cap_bytes
        self.urls.append(url)
        self.headers.append(dict(headers or {}))
        return HttpResponse(
            status_code=200,
            headers={"content-type": self.content_type},
            body=self.body.encode("utf-8"),
            final_url=url,
        )


def _context(interface_id: str, **overrides: Any) -> ProviderRunContext:
    fields: dict[str, Any] = {
        "run_id": "run-secrets",
        "interface_id": interface_id,
        "interface_digest": "sha256:" + "aa" * 32,
        "implementation_digest": "sha256:" + "bb" * 32,
        "input_bucket": "query_form=keyword;recency=any_time;result_depth=shallow",
        "input": {},
        "coordinates": {},
        "budgets": BUDGETS,
        "declared_endpoints": ("https://instance.example",),
        "capture_contract": None,
        "secrets": {},
        "egress": EgressRecorder(),
    }
    fields.update(overrides)
    return ProviderRunContext(**fields)


def test_the_instance_credential_travels_as_a_header_and_not_in_the_query() -> None:
    client = _CapturingClient(INSTANCE_ANSWER, "application/json")
    provider = SearxngSearch(client_factory=lambda recorder, *, url, timeout_seconds: client)  # type: ignore[arg-type,return-value]
    result = provider(
        _context(
            "search.web",
            input={"query": "tide gauge"},
            coordinates={"instance_url": "https://instance.example"},
            secrets={CREDENTIAL_REF: CREDENTIAL},
        )
    )
    assert result.status == "ok"
    assert client.headers[0]["authorization"] == CREDENTIAL
    # A credential in a query string is a credential in somebody's access log.
    assert CREDENTIAL not in client.urls[0]


def test_a_run_served_by_a_real_transport_is_not_labelled_as_a_replay() -> None:
    """The negative half of the replay label, on a path that actually succeeds.

    The full-loop suite asserts the label is present in the output *and* on the
    trace whenever a packaged recording served a run. This is the other
    direction, and it needs a non-replayed run that still reaches ``ok``: the
    capturing client is one, because it answers from a canned body without being
    a packaged recording. A label emitted unconditionally rather than tracking
    the actual source would show up right here.
    """

    client = _CapturingClient(INSTANCE_ANSWER, "application/json")
    provider = SearxngSearch(client_factory=lambda recorder, *, url, timeout_seconds: client)  # type: ignore[arg-type,return-value]
    result = provider(
        _context(
            "search.web",
            input={"query": "tide gauge"},
            coordinates={"instance_url": "https://instance.example"},
        )
    )
    assert result.status == "ok"
    assert result.output is not None
    assert result.output["retrieved"]["source"] == "network"
    assert not [event for event in result.events if event.get("kind") == "packaged_recording"]


def test_the_credential_reaches_neither_the_output_nor_the_trace() -> None:
    client = _CapturingClient(INSTANCE_ANSWER, "application/json")
    provider = SearxngSearch(client_factory=lambda recorder, *, url, timeout_seconds: client)  # type: ignore[arg-type,return-value]
    result = provider(
        _context(
            "search.web",
            input={"query": "tide gauge"},
            coordinates={"instance_url": "https://instance.example"},
            secrets={CREDENTIAL_REF: CREDENTIAL},
        )
    )
    rendered = repr(result.output) + repr(result.events) + repr(result.metrics)
    assert CREDENTIAL not in rendered


def test_the_recorded_parameters_are_the_ones_actually_submitted() -> None:
    """The receipt's parameter block must not be a hopeful reconstruction."""

    client = _CapturingClient(INSTANCE_ANSWER, "application/json")
    provider = SearxngSearch(client_factory=lambda recorder, *, url, timeout_seconds: client)  # type: ignore[arg-type,return-value]
    result = provider(
        _context(
            "search.web",
            input={"query": "tide gauge", "max_age_hours": 6},
            coordinates={"instance_url": "https://instance.example"},
        )
    )
    assert result.output is not None
    parameters = result.output["retrieved"]["parameters"]
    for key, value in parameters.items():
        assert f"{key}={value}".replace(" ", "+") in client.urls[0].replace("%20", "+")


def test_a_fetch_naming_an_undelivered_credential_refuses() -> None:
    """A run that says it is authenticated does not silently go out anonymous."""

    result = WebFetch()(
        _context(
            "web.fetch",
            input={"url": "https://fixture.invalid/page", "credential_ref": "web.token"},
            input_bucket="source_kind=static_html;access=authenticated;page_weight=light",
        )
    )
    assert result.status == "refused"
    assert result.refusal is not None
    assert result.refusal.code is RefusalCode.UNRESOLVED_SECRET_REF


def test_the_client_never_puts_credentials_in_its_own_repr() -> None:
    """A client that prints its headers prints them into every traceback."""

    client = RecordingClient(EgressRecorder(), timeout_seconds=1.0)
    try:
        assert CREDENTIAL not in repr(client)
    finally:
        client.close()


@pytest.mark.parametrize("interface_id", ["web.fetch", "search.web"])
def test_no_credential_material_is_named_in_the_manifest(interface_id: str, manifest: Any) -> None:
    """Secret-refs name credentials; manifests carry neither refs' values nor keys."""

    implementation = manifest.implementation(interface_id)
    rendered = implementation.model_dump_json()
    assert "token" not in rendered.lower()
    assert "password" not in rendered.lower()
