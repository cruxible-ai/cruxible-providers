"""Bind and invoke, end to end, on both backend kinds, for every operation.

Every run here goes through a real child process: the entrypoint is imported in
the child, the run context crosses a pipe, credential material arrives on an
inherited descriptor, and the result envelope comes back over stdout. What is
replaced is the socket, and only the socket — the recordings are served through
the real httpx client, so the request pipeline, the egress hook and the size cap
all execute.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cruxible_provider_runtime.acquisition import external_capture_result
from cruxible_provider_runtime.backends import ContainerBackend, LocalEnvBackend
from cruxible_provider_runtime.binding import Binding, BindRequest, bind
from cruxible_provider_runtime.digests import implementation_digest
from cruxible_provider_runtime.egress import (
    DYNAMIC_TARGET_FROM_CONFIGURATION,
    DYNAMIC_TARGET_FROM_RUN_INPUT,
)
from cruxible_provider_runtime.execute import invoke
from cruxible_provider_runtime.manifest import BackendKind
from cruxible_provider_runtime.protocol import Budgets
from cruxible_provider_runtime.registry import StubRegistry
from cruxible_provider_web.operations import PROVIDER_DECLINED
from cruxible_provider_web.recordings import load_recordings
from cruxible_provider_web.search import CREDENTIAL_REF

from .conftest import DISTRIBUTION_SHA256, MARKER_ENVIRONMENT, PROVIDER_ID

BUDGETS = Budgets(wall_clock_seconds=60.0, output_bytes=4_000_000)
BACKENDS: tuple[BackendKind, ...] = ("local_env", "container")
INSTANCE = {"instance_url": "https://fixture.invalid"}
ARTICLE_URL = "https://fixture.invalid/articles/tide-gauge-recalibration"
DUMMY_CREDENTIAL = "dummy-instance-token-c0ffee-do-not-use"


def _bind(
    operation: str,
    backend_kind: BackendKind,
    registry: StubRegistry,
    manifest_path: Path,
    lock_path: Path,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> Binding:
    return bind(
        registry,
        BindRequest(
            provider_id=PROVIDER_ID,
            operation=operation,
            backend_kind=backend_kind,
            manifest_path=manifest_path,
            lock_path=lock_path,
            marker_environment=MARKER_ENVIRONMENT,
            allow_editable_dev_sources=True,
        ),
        local_backend=local_backend,
        container_backend=container_backend,
    )


@pytest.fixture()
def fetch_binding(
    request: pytest.FixtureRequest,
    registry: StubRegistry,
    manifest_path: Path,
    lock_path: Path,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> Binding:
    backend_kind: BackendKind = getattr(request, "param", "local_env")
    return _bind(
        "web.fetch",
        backend_kind,
        registry,
        manifest_path,
        lock_path,
        local_backend,
        container_backend,
    )


@pytest.fixture()
def pages_binding(
    request: pytest.FixtureRequest,
    registry: StubRegistry,
    manifest_path: Path,
    lock_path: Path,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> Binding:
    backend_kind: BackendKind = getattr(request, "param", "local_env")
    return _bind(
        "web.fetch.pages",
        backend_kind,
        registry,
        manifest_path,
        lock_path,
        local_backend,
        container_backend,
    )


@pytest.fixture()
def render_binding(
    request: pytest.FixtureRequest,
    registry: StubRegistry,
    manifest_path: Path,
    lock_path: Path,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> Binding:
    backend_kind: BackendKind = getattr(request, "param", "local_env")
    return _bind(
        "web.render",
        backend_kind,
        registry,
        manifest_path,
        lock_path,
        local_backend,
        container_backend,
    )


@pytest.fixture()
def dom_binding(
    request: pytest.FixtureRequest,
    registry: StubRegistry,
    manifest_path: Path,
    lock_path: Path,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> Binding:
    backend_kind: BackendKind = getattr(request, "param", "local_env")
    return _bind(
        "web.render.dom",
        backend_kind,
        registry,
        manifest_path,
        lock_path,
        local_backend,
        container_backend,
    )


@pytest.fixture()
def search_binding(
    request: pytest.FixtureRequest,
    registry: StubRegistry,
    manifest_path: Path,
    lock_path: Path,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> Binding:
    backend_kind: BackendKind = getattr(request, "param", "local_env")
    return _bind(
        "search.web",
        backend_kind,
        registry,
        manifest_path,
        lock_path,
        local_backend,
        container_backend,
    )


# -- web.fetch.pages -------------------------------------------------------


@pytest.mark.parametrize("pages_binding", BACKENDS, indirect=True)
def test_pages_success_path(
    pages_binding: Binding,
    registry: StubRegistry,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> None:
    """Through the child harness: one verbatim Capture per page, in page order."""

    values = [f"station-{index:02d}" for index in range(10)]
    outcome = invoke(
        pages_binding,
        registry=registry,
        payload={
            "url": ARTICLE_URL,
            "query": [{"name": "station", "values": values, "join": "comma"}],
            "page_param": "station",
            "max_url_chars": len(ARTICLE_URL) + 40,
        },
        budgets=BUDGETS,
        local_backend=local_backend,
        container_backend=container_backend,
    )
    assert outcome.status == "ok", outcome.envelope.error
    assert outcome.receipt_fields()["operation"] == "web.fetch.pages"
    assert outcome.envelope.incomplete_captures == []
    output = outcome.envelope.output
    assert output is not None and "failed_pages" not in output
    recorded = load_recordings()["article-static"].response.body.encode("utf-8")
    sent: list[str] = []
    for index, member in enumerate(output["captures"]):
        assert base64.b64decode(member["content_base64"]) == recorded
        coordinate = member["coordinate"]
        assert coordinate["page_index"] == index
        assert coordinate["page_count"] == len(output["captures"])
        assert coordinate["source"] == "packaged-recording"
        sent += coordinate["url"].partition("?station=")[2].split(",")
    assert len(output["captures"]) > 1
    assert sent == values
    events = outcome.envelope.trace.events
    assert [event["kind"] for event in events] == ["packaged_recording"] * len(output["captures"])


# -- web.fetch -------------------------------------------------------------


@pytest.mark.parametrize("fetch_binding", BACKENDS, indirect=True)
def test_fetch_success_path(
    fetch_binding: Binding,
    registry: StubRegistry,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> None:
    outcome = invoke(
        fetch_binding,
        registry=registry,
        payload={"url": ARTICLE_URL},
        budgets=BUDGETS,
        local_backend=local_backend,
        container_backend=container_backend,
    )
    assert outcome.status == "ok"
    assert outcome.receipt_fields()["operation"] == "web.fetch"
    assert outcome.envelope.output is not None
    # The Capture is the result's one field. The adapter states no validity
    # window: the optional key is absent, never null.
    output = outcome.envelope.output["capture"]
    assert "source_effective_time" not in output
    # The Capture is the body the origin sent, verbatim; how it was obtained is
    # the coordinate, never inside the bytes.
    body = base64.b64decode(output["content_base64"])
    recorded = load_recordings()["article-static"].response
    assert body == recorded.body.encode("utf-8")
    assert output["bytes_digest"] == "sha256:" + hashlib.sha256(body).hexdigest()
    coordinate = output["coordinate"]
    assert coordinate["status"] == 200
    assert coordinate["final_url"] == ARTICLE_URL
    assert coordinate["content_type"] == recorded.headers["content-type"]
    assert coordinate["source"] == "packaged-recording"
    assert "renderer" not in coordinate and "derived_from" not in coordinate

    # The replay is labelled in the COORDINATE and on the TRACE, and both halves
    # are asserted: a Capture is built from the output, but an exhaust reader sees
    # the trace, and a label that reached only one of them would let a replayed
    # run read as a fetch on the surface that happened not to carry it.
    events = outcome.envelope.trace.events
    assert [event["kind"] for event in events] == ["packaged_recording"]
    assert events[0]["url"] == ARTICLE_URL
    assert "no origin was contacted" in events[0]["note"]


@pytest.mark.parametrize("fetch_binding", BACKENDS, indirect=True)
def test_fetch_records_the_endpoint_it_requested(
    fetch_binding: Binding,
    registry: StubRegistry,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> None:
    """A dynamic declaration is governed by its recording, so the recording must exist."""

    outcome = invoke(
        fetch_binding,
        registry=registry,
        payload={"url": ARTICLE_URL},
        budgets=BUDGETS,
        local_backend=local_backend,
        container_backend=container_backend,
    )
    assert outcome.egress.observed == ("https://fixture.invalid",)
    assert outcome.egress.dynamic_forms == (DYNAMIC_TARGET_FROM_RUN_INPUT,)
    receipt = outcome.receipt_fields()
    assert receipt["endpoints_contacted"] == ["https://fixture.invalid"]
    # The receipt says the declaration was dynamic, so nobody reads the empty
    # undeclared set as an allowlist that held.
    assert receipt["dynamic_endpoint_forms"] == [DYNAMIC_TARGET_FROM_RUN_INPUT]


@pytest.mark.parametrize("dom_binding", BACKENDS, indirect=True)
def test_dom_fetch_captures_the_assembled_page(
    dom_binding: Binding,
    registry: StubRegistry,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> None:
    """The expectation names text that exists only after client-side assembly."""

    recording = load_recordings()["dashboard-rendered"]
    assert recording.rendered_body is not None
    outcome = invoke(
        dom_binding,
        registry=registry,
        payload={"url": "https://fixture.invalid/dashboard"},
        budgets=BUDGETS,
        local_backend=local_backend,
        container_backend=container_backend,
    )
    assert outcome.status == "ok", outcome.envelope
    assert outcome.envelope.output is not None
    assembled = base64.b64decode(outcome.envelope.output["capture"]["content_base64"])
    assert assembled == recording.rendered_body.encode("utf-8")
    assert b"3.214" in assembled
    coordinate = outcome.envelope.output["capture"]["coordinate"]
    assert coordinate["renderer"] == "recorded:dashboard-rendered"
    assert "derived_from" not in coordinate
    # A recorded render is a replay too, and carries a label on the trace.
    assert [event["kind"] for event in outcome.envelope.trace.events] == ["recorded_render"]
    assert outcome.receipt_fields()["endpoints_contacted"] == ["https://fixture.invalid"]


@pytest.mark.skipif(
    importlib.util.find_spec("trafilatura") is None, reason="web.render needs trafilatura"
)
@pytest.mark.parametrize("render_binding", BACKENDS, indirect=True)
def test_render_derives_a_fetched_capture_and_contacts_nothing(
    render_binding: Binding,
    registry: StubRegistry,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> None:
    recording = load_recordings()["article-static"]
    fetched = recording.response.body.encode("utf-8")
    source = external_capture_result(
        fetched,
        source_identity="web.response",
        coordinate_type="http-response-v1",
        coordinate={
            "url": recording.request_url,
            "final_url": recording.request_url,
            "status": 200,
            "content_type": recording.response.headers["content-type"],
            "request_digest": "sha256:" + "0" * 64,
            "source": "packaged-recording",
        },
        observed_at=datetime(2026, 11, 4, tzinfo=UTC),
        selector_type="whole-response-v1",
    )
    outcome = invoke(
        render_binding,
        registry=registry,
        payload={"form": "json", "source": source},
        budgets=BUDGETS,
        local_backend=local_backend,
        container_backend=container_backend,
    )
    assert outcome.status == "ok", outcome.envelope
    assert outcome.envelope.output is not None
    derived = outcome.envelope.output["capture"]
    document = json.loads(base64.b64decode(derived["content_base64"]))
    assert document["url"] == recording.request_url
    assert document["title"] == "Recalibrating the Newlyn tide gauge"
    coordinate = derived["coordinate"]
    assert coordinate["derived_from"] == "sha256:" + hashlib.sha256(fetched).hexdigest()
    assert derived["observed_at"] == "2026-11-04T00:00:00Z"
    assert outcome.receipt_fields()["endpoints_contacted"] == []


@pytest.mark.parametrize("fetch_binding", BACKENDS, indirect=True)
def test_fetch_error_path_is_reported_not_raised(
    fetch_binding: Binding,
    registry: StubRegistry,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> None:
    """A URL with no recording behind it fails inside the client, in the child.

    This is also the negative half of the replay label. No recording matches, so
    the real transport is selected and the run must NOT be labelled as one served
    from a packaged recording — the branch that would break if the event were
    emitted unconditionally rather than tracking the actual source.
    """

    outcome = invoke(
        fetch_binding,
        registry=registry,
        payload={"url": "https://fixture.invalid/nothing-recorded-here"},
        budgets=BUDGETS,
        local_backend=local_backend,
        container_backend=container_backend,
    )
    assert outcome.status == "error"
    assert outcome.envelope.error is not None
    assert not [
        event
        for event in outcome.envelope.trace.events
        if event.get("kind") == "packaged_recording"
    ]


@pytest.mark.parametrize("fetch_binding", BACKENDS, indirect=True)
def test_fetch_typed_refusal_path(
    fetch_binding: Binding,
    registry: StubRegistry,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> None:
    outcome = invoke(
        fetch_binding,
        registry=registry,
        payload={"url": "ftp://fixture.invalid/articles/tide-gauge-recalibration"},
        budgets=BUDGETS,
        local_backend=local_backend,
        container_backend=container_backend,
    )
    assert outcome.status == "refused"
    assert outcome.envelope.refusal is not None
    assert outcome.envelope.refusal.code == PROVIDER_DECLINED


@pytest.mark.parametrize("fetch_binding", BACKENDS, indirect=True)
def test_a_response_heavier_than_the_declared_cap_refuses(
    fetch_binding: Binding,
    registry: StubRegistry,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> None:
    """The weight declaration is checked against the response, not trusted.

    Weight is not knowable before retrieval, so the caller declares a cap. That
    makes the cap a claim, and this is where the claim is tested: a body that
    outgrows it refuses rather than being truncated into a Capture that looks
    complete.
    """

    outcome = invoke(
        fetch_binding,
        registry=registry,
        payload={"url": ARTICLE_URL, "max_bytes": 128},
        budgets=BUDGETS,
        local_backend=local_backend,
        container_backend=container_backend,
    )
    assert outcome.status == "refused"
    assert outcome.envelope.refusal is not None
    assert outcome.envelope.refusal.code == PROVIDER_DECLINED
    assert outcome.envelope.refusal.detail["cap_bytes"] == 128


@pytest.mark.parametrize("fetch_binding", BACKENDS, indirect=True)
def test_an_input_naming_no_url_is_declined_by_the_adapter(
    fetch_binding: Binding,
    registry: StubRegistry,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> None:
    """No classifier stands in front of the adapter; it judges its own input."""

    outcome = invoke(
        fetch_binding,
        registry=registry,
        payload={"max_bytes": 64},
        budgets=BUDGETS,
        local_backend=local_backend,
        container_backend=container_backend,
    )
    assert outcome.status == "refused"
    assert outcome.envelope.refusal is not None
    assert outcome.envelope.refusal.code == PROVIDER_DECLINED


# -- search.web ------------------------------------------------------------


@pytest.mark.parametrize("search_binding", BACKENDS, indirect=True)
def test_search_success_path(
    search_binding: Binding,
    registry: StubRegistry,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> None:
    outcome = invoke(
        search_binding,
        registry=registry,
        payload={"query": "tide gauge recalibration"},
        coordinates=INSTANCE,
        budgets=BUDGETS,
        local_backend=local_backend,
        container_backend=container_backend,
    )
    assert outcome.status == "ok"
    assert outcome.receipt_fields()["operation"] == "search.web"
    assert outcome.envelope.output is not None
    response = outcome.envelope.output["response"]
    assert "source_effective_time" not in response
    # The Capture is the instance's answer, verbatim: no ranking is derived here.
    answer = base64.b64decode(response["content_base64"])
    assert answer == load_recordings()["searxng-answer"].response.body.encode("utf-8")
    assert response["coordinate"]["final_url"].startswith("https://fixture.invalid/search?")
    # A configuration-derived target is recorded, not mistaken for an allowlist.
    assert outcome.egress.observed == ("https://fixture.invalid",)
    assert outcome.egress.dynamic_forms == (DYNAMIC_TARGET_FROM_CONFIGURATION,)
    assert outcome.egress.conformant


@pytest.mark.parametrize("search_binding", BACKENDS, indirect=True)
def test_search_requires_an_explicit_instance_binding(
    search_binding: Binding,
    registry: StubRegistry,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> None:
    """A reusable search package does not invent a default search service."""

    outcome = invoke(
        search_binding,
        registry=registry,
        payload={"query": "tide gauge recalibration"},
        coordinates={},
        budgets=BUDGETS,
        local_backend=local_backend,
        container_backend=container_backend,
    )
    assert outcome.status == "refused"
    assert outcome.envelope.refusal is not None
    assert outcome.envelope.refusal.code == PROVIDER_DECLINED
    assert outcome.egress.observed == ()


@pytest.mark.parametrize("search_binding", BACKENDS, indirect=True)
def test_search_credential_arrives_by_ref_over_the_descriptor(
    search_binding: Binding,
    registry: StubRegistry,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> None:
    outcome = invoke(
        search_binding,
        registry=registry,
        payload={"query": "tide gauge recalibration"},
        coordinates=INSTANCE,
        secrets={CREDENTIAL_REF: DUMMY_CREDENTIAL},
        budgets=BUDGETS,
        local_backend=local_backend,
        container_backend=container_backend,
    )
    assert outcome.status == "ok"
    # The credential was used as a header and appears nowhere in the result.
    assert DUMMY_CREDENTIAL not in outcome.envelope.model_dump_json()


# -- identity --------------------------------------------------------------


def test_two_entrypoints_in_one_package_have_different_implementation_digests(
    registry: StubRegistry,
    manifest_path: Path,
    lock_path: Path,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> None:
    fetch = _bind(
        "web.fetch",
        "local_env",
        registry,
        manifest_path,
        lock_path,
        local_backend,
        container_backend,
    )
    search = _bind(
        "search.web",
        "local_env",
        registry,
        manifest_path,
        lock_path,
        local_backend,
        container_backend,
    )
    assert fetch.implementation_digest != search.implementation_digest
    assert fetch.operation_signature_digest != search.operation_signature_digest
    assert fetch.build_digest == search.build_digest
    assert fetch.implementation_digest == implementation_digest(
        entrypoint="cruxible_provider_web.fetch:WebFetch",
        distribution_sha256=DISTRIBUTION_SHA256,
    )


def test_a_backend_switch_does_not_split_track_record(
    registry: StubRegistry,
    manifest_path: Path,
    lock_path: Path,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> None:
    local = _bind(
        "web.fetch",
        "local_env",
        registry,
        manifest_path,
        lock_path,
        local_backend,
        container_backend,
    )
    container = _bind(
        "web.fetch",
        "container",
        registry,
        manifest_path,
        lock_path,
        local_backend,
        container_backend,
    )
    assert local.implementation_digest == container.implementation_digest
    assert local.materialization_digest != container.materialization_digest
