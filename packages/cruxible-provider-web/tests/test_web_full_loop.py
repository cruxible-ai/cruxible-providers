"""Bind and invoke, end to end, on both backend kinds, for both interfaces.

Every run here goes through a real child process: the entrypoint is imported in
the child, the run context crosses a pipe, credential material arrives on an
inherited descriptor, and the result envelope comes back over stdout. What is
replaced is the socket, and only the socket — the recordings are served through
the real httpx client, so the request pipeline, the egress hook and the size cap
all execute.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from cruxible_provider_runtime.backends import ContainerBackend, LocalEnvBackend
from cruxible_provider_runtime.binding import Binding, BindRequest, bind
from cruxible_provider_runtime.digests import implementation_digest
from cruxible_provider_runtime.egress import DYNAMIC_TARGET_FROM_RUN_INPUT
from cruxible_provider_runtime.errors import RefusalCode, RefusalError
from cruxible_provider_runtime.execute import invoke
from cruxible_provider_runtime.manifest import BackendKind
from cruxible_provider_runtime.protocol import Budgets
from cruxible_provider_runtime.registry import StubRegistry
from cruxible_provider_web.search import CREDENTIAL_REF

from .conftest import DISTRIBUTION_SHA256, MARKER_ENVIRONMENT, PROVIDER_ID

BUDGETS = Budgets(wall_clock_seconds=60.0, output_bytes=4_000_000)
BACKENDS: tuple[BackendKind, ...] = ("local_env", "container")
INSTANCE = {"instance_url": "https://fixture.invalid"}
ARTICLE_URL = "https://fixture.invalid/articles/tide-gauge-recalibration"
DUMMY_CREDENTIAL = "dummy-instance-token-c0ffee-do-not-use"


def _bind(
    interface_id: str,
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
            interface_id=interface_id,
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
    assert outcome.input_bucket == "source_kind=static_html;access=public;page_weight=light"
    assert outcome.envelope.output is not None
    retrieved = outcome.envelope.output["retrieved"]
    derived = outcome.envelope.output["derived"]

    # The split is the contract: an exchange that happened, and a reading of it.
    assert retrieved["status_code"] == 200
    assert retrieved["source"] == "packaged-recording"
    assert retrieved["body_sha256"].startswith("sha256:")
    assert derived["engine"] == "trafilatura"
    assert derived["kind"] == "markdown"
    assert "Newlyn" in derived["text"]
    assert "Subscribe to our newsletter" not in derived["text"]


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


@pytest.mark.parametrize("fetch_binding", BACKENDS, indirect=True)
def test_fetch_renders_when_the_run_asks_for_a_browser(
    fetch_binding: Binding,
    registry: StubRegistry,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> None:
    """The expectation names text that exists only after client-side assembly."""

    outcome = invoke(
        fetch_binding,
        registry=registry,
        payload={"url": "https://fixture.invalid/dashboard", "render": True},
        budgets=BUDGETS,
        local_backend=local_backend,
        container_backend=container_backend,
    )
    assert outcome.status == "ok"
    assert outcome.input_bucket == "source_kind=js_rendered;access=public;page_weight=light"
    assert outcome.envelope.output is not None
    assert outcome.envelope.output["retrieved"]["renderer"] == "recorded:dashboard-rendered"
    assert "3.214" in outcome.envelope.output["derived"]["text"]


@pytest.mark.parametrize("fetch_binding", BACKENDS, indirect=True)
def test_fetch_error_path_is_reported_not_raised(
    fetch_binding: Binding,
    registry: StubRegistry,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> None:
    """A URL with no recording behind it fails inside the client, in the child."""

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
    assert outcome.envelope.refusal.code is RefusalCode.PROVIDER_DECLINED


@pytest.mark.parametrize("fetch_binding", BACKENDS, indirect=True)
def test_a_response_heavier_than_the_admitted_bucket_refuses(
    fetch_binding: Binding,
    registry: StubRegistry,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> None:
    """The weight declaration is checked against the response, not trusted.

    Weight is not knowable before retrieval, so the bucket is derived from the
    cap the caller declared. That makes the cap a claim, and this is where the
    claim is tested: a body that outgrows it refuses rather than being truncated
    into a Capture that looks complete.
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
    assert outcome.envelope.refusal.code is RefusalCode.PROVIDER_DECLINED
    assert outcome.envelope.refusal.detail["cap_bytes"] == 128


def test_an_unclaimed_bucket_refuses_before_any_process_starts(
    fetch_binding: Binding,
    registry: StubRegistry,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> None:
    """A PDF is the document plane's work, and this implementation says so."""

    with pytest.raises(RefusalError) as exc:
        invoke(
            fetch_binding,
            registry=registry,
            payload={"url": "https://fixture.invalid/reports/annual.pdf"},
            budgets=BUDGETS,
            local_backend=local_backend,
            container_backend=container_backend,
        )
    assert exc.value.code is RefusalCode.UNCLAIMED_BUCKET
    assert exc.value.refusal.detail["bucket"].startswith("source_kind=binary")


def test_an_unclassifiable_input_refuses(
    fetch_binding: Binding,
    registry: StubRegistry,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> None:
    with pytest.raises(RefusalError) as exc:
        invoke(
            fetch_binding,
            registry=registry,
            payload={"render": True},
            budgets=BUDGETS,
            local_backend=local_backend,
            container_backend=container_backend,
        )
    assert exc.value.code is RefusalCode.UNCLASSIFIED_INPUT


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
        payload={"query": "tide gauge recalibration", "limit": 10},
        coordinates=INSTANCE,
        budgets=BUDGETS,
        local_backend=local_backend,
        container_backend=container_backend,
    )
    assert outcome.status == "ok"
    assert outcome.input_bucket == "query_form=keyword;recency=any_time;result_depth=shallow"
    assert outcome.envelope.output is not None
    assert outcome.envelope.output["retrieved"]["result_count"] == 3
    assert outcome.envelope.output["derived"]["kind"] == "recency_filtered_ranking"
    # A concrete declaration, satisfied: observed is inside declared, and no
    # dynamic form was in force.
    assert outcome.egress.observed == ("https://fixture.invalid",)
    assert outcome.egress.dynamic_forms == ()
    assert outcome.egress.conformant


@pytest.mark.parametrize("search_binding", BACKENDS, indirect=True)
def test_search_refuses_an_instance_the_declaration_does_not_carry(
    search_binding: Binding,
    registry: StubRegistry,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> None:
    """Which instance a run queries is governed, so an undeclared one refuses.

    And it refuses *before* the request, which is the point: a check performed
    after the fact would be a report of a violation rather than a refusal of one.
    """

    outcome = invoke(
        search_binding,
        registry=registry,
        payload={"query": "tide gauge recalibration"},
        coordinates={"instance_url": "https://someone-elses-instance.example"},
        budgets=BUDGETS,
        local_backend=local_backend,
        container_backend=container_backend,
    )
    assert outcome.status == "refused"
    assert outcome.envelope.refusal is not None
    assert outcome.envelope.refusal.code is RefusalCode.UNDECLARED_EGRESS
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


def test_two_interfaces_in_one_package_have_different_implementation_digests(
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
    assert fetch.implementation_digest == implementation_digest(
        interface_id="web.fetch",
        interface_digest=fetch.interface_digest,
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
