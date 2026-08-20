"""Bind and invoke, end to end, on both backend kinds, for both interfaces.

Every run here goes through a real child process with **no engine installed
anywhere**, which is the heavy-engine split working: an implementation binds an
environment that declares an engine, and the paths that do not need one — the
already-linear conversion, the replayed fixture — run to completion, while a path
that does need one refuses with a typed refusal instead of an ImportError.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from cruxible_provider_runtime.backends import ContainerBackend, LocalEnvBackend
from cruxible_provider_runtime.binding import Binding, BindRequest, bind
from cruxible_provider_runtime.errors import RefusalCode, RefusalError
from cruxible_provider_runtime.execute import invoke
from cruxible_provider_runtime.manifest import BackendKind
from cruxible_provider_runtime.protocol import Budgets
from cruxible_provider_runtime.registry import StubRegistry

from .conftest import MARKDOWN_EXTRAS, MARKER_ENVIRONMENT, OCR_EXTRAS, PROVIDER_ID

BUDGETS = Budgets(wall_clock_seconds=60.0, output_bytes=4_000_000)
BACKENDS: tuple[BackendKind, ...] = ("local_env", "container")

CSV = b"reach,nitrate_mg_l\nUpper,4.1\nLower,9.2\n"
PLAIN_TEXT_INPUT = {
    "source": {
        "kind": "inline",
        "filename": "reach-readings.csv",
        "media_type": "text/csv",
        "content_base64": base64.b64encode(CSV).decode("ascii"),
    },
    "layout": "tabular",
}
PACKAGED_PDF = {
    "kind": "packaged_fixture",
    "id": "pdf-single",
    "filename": "tide-gauge-report.pdf",
    "media_type": "application/pdf",
}
PACKAGED_PDF_INPUT = {"source": PACKAGED_PDF, "page_count": 1}


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
def markdown_binding(
    request: pytest.FixtureRequest,
    registry: StubRegistry,
    manifest_path: Path,
    lock_path: Path,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> Binding:
    backend_kind: BackendKind = getattr(request, "param", "local_env")
    return _bind(
        "doc.to_markdown",
        backend_kind,
        registry,
        manifest_path,
        lock_path,
        local_backend,
        container_backend,
    )


@pytest.fixture()
def ocr_binding(
    request: pytest.FixtureRequest,
    registry: StubRegistry,
    manifest_path: Path,
    lock_path: Path,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> Binding:
    backend_kind: BackendKind = getattr(request, "param", "local_env")
    return _bind(
        "ocr.extract",
        backend_kind,
        registry,
        manifest_path,
        lock_path,
        local_backend,
        container_backend,
    )


@pytest.mark.parametrize("markdown_binding", BACKENDS, indirect=True)
def test_the_engine_free_path_succeeds_with_no_engine_installed(
    markdown_binding: Binding,
    registry: StubRegistry,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> None:
    outcome = invoke(
        markdown_binding,
        registry=registry,
        payload=PLAIN_TEXT_INPUT,
        budgets=BUDGETS,
        local_backend=local_backend,
        container_backend=container_backend,
    )
    assert outcome.status == "ok"
    assert outcome.input_bucket == (
        "format=plain_text;scanned=born_digital;page_count=single;layout=tabular"
    )
    assert outcome.envelope.output is not None
    derived = outcome.envelope.output["derived"]
    assert derived["engine"] == "plain-text"
    assert "| reach | nitrate_mg_l |" in derived["text"]
    assert outcome.envelope.output["document"]["origin"] == "inline"
    # Nothing is contacted by this plane, in any bucket.
    assert outcome.egress.observed == ()
    assert outcome.egress.dynamic_forms == ()


@pytest.mark.parametrize("markdown_binding", BACKENDS, indirect=True)
def test_a_packaged_fixture_replays_and_says_that_it_did(
    markdown_binding: Binding,
    registry: StubRegistry,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> None:
    outcome = invoke(
        markdown_binding,
        registry=registry,
        payload=PACKAGED_PDF_INPUT,
        budgets=BUDGETS,
        local_backend=local_backend,
        container_backend=container_backend,
    )
    assert outcome.status == "ok"
    assert outcome.envelope.output is not None
    assert outcome.envelope.output["derived"]["engine"] == "recorded:pdf-single"
    assert outcome.envelope.output["document"]["origin"] == "packaged-recording"
    # The label is not only in the output: the trace says no engine ran.
    events = outcome.envelope.trace.events
    assert any(event["kind"] == "recorded_engine_response" for event in events)


@pytest.mark.parametrize("markdown_binding", BACKENDS, indirect=True)
def test_an_inline_document_needing_the_engine_refuses_when_it_is_absent(
    markdown_binding: Binding,
    registry: StubRegistry,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> None:
    """A missing engine is environment divergence, not a failed answer.

    This is the fail-closed half of the heavy-engine split: the base install has
    no Docling, so a run that genuinely needs it must say so in the taxonomy's
    words rather than surface an ImportError as a provider error.
    """

    outcome = invoke(
        markdown_binding,
        registry=registry,
        payload={
            "source": {
                "kind": "inline",
                "filename": "supplied.pdf",
                "media_type": "application/pdf",
                "content_base64": base64.b64encode(b"%PDF-1.4 not really").decode("ascii"),
            },
            "page_count": 1,
        },
        budgets=BUDGETS,
        local_backend=local_backend,
        container_backend=container_backend,
    )
    assert outcome.status == "refused"
    assert outcome.envelope.refusal is not None
    assert outcome.envelope.refusal.code is RefusalCode.ENVIRONMENT_DIVERGENCE
    assert outcome.envelope.refusal.detail["required_extra"] == "docling"


@pytest.mark.parametrize("markdown_binding", BACKENDS, indirect=True)
def test_a_typed_refusal_path(
    markdown_binding: Binding,
    registry: StubRegistry,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> None:
    outcome = invoke(
        markdown_binding,
        registry=registry,
        payload={
            "source": {
                "kind": "packaged_fixture",
                "id": "not-a-fixture",
                "filename": "missing.pdf",
                "media_type": "application/pdf",
            },
            "page_count": 1,
        },
        budgets=BUDGETS,
        local_backend=local_backend,
        container_backend=container_backend,
    )
    assert outcome.status == "refused"
    assert outcome.envelope.refusal is not None
    assert outcome.envelope.refusal.code is RefusalCode.PROVIDER_DECLINED


@pytest.mark.parametrize("markdown_binding", BACKENDS, indirect=True)
def test_a_declared_page_count_that_the_document_contradicts_refuses(
    markdown_binding: Binding,
    registry: StubRegistry,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> None:
    """The bucket is derived from a declaration, so the declaration is checked.

    The three-page document is submitted as a single-page one. Admission cannot
    know better — nobody has opened the file yet — so the adapter refuses once it
    has, rather than returning a conversion attributed to the wrong bucket.
    """

    outcome = invoke(
        markdown_binding,
        registry=registry,
        payload={
            "source": {
                "kind": "packaged_fixture",
                "id": "pdf-short",
                "filename": "water-quality-report.pdf",
                "media_type": "application/pdf",
            },
            "page_count": 1,
        },
        budgets=BUDGETS,
        local_backend=local_backend,
        container_backend=container_backend,
    )
    assert outcome.status == "refused"
    assert outcome.envelope.refusal is not None
    assert outcome.envelope.refusal.detail["observed_page_count"] == 3
    assert outcome.envelope.refusal.detail["declared_page_count"] == 1


def test_an_unclaimed_bucket_refuses_before_any_process_starts(
    markdown_binding: Binding,
    registry: StubRegistry,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> None:
    """A scanned document is ocr.extract's work, and this implementation says so."""

    with pytest.raises(RefusalError) as exc:
        invoke(
            markdown_binding,
            registry=registry,
            payload={
                "source": PACKAGED_PDF,
                "page_count": 1,
                "scanned": "scanned",
            },
            budgets=BUDGETS,
            local_backend=local_backend,
            container_backend=container_backend,
        )
    assert exc.value.code is RefusalCode.UNCLAIMED_BUCKET


@pytest.mark.parametrize("ocr_binding", BACKENDS, indirect=True)
def test_the_ocr_replay_reads_the_shipped_image(
    ocr_binding: Binding,
    registry: StubRegistry,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> None:
    outcome = invoke(
        ocr_binding,
        registry=registry,
        payload={
            "source": {
                "kind": "packaged_fixture",
                "id": "scan-clean",
                "filename": "scan-clean.png",
                "media_type": "image/png",
            },
            "page_count": 1,
        },
        budgets=BUDGETS,
        local_backend=local_backend,
        container_backend=container_backend,
    )
    assert outcome.status == "ok"
    assert outcome.envelope.output is not None
    derived = outcome.envelope.output["derived"]
    assert derived["engine"] == "recorded:scan-clean"
    assert "NEWLYN TIDE GAUGE" in derived["text"]
    # The engine's own number, under the engine's name. Never a grade.
    assert derived["pages"][0]["engine_mean_confidence"] == pytest.approx(0.98)


@pytest.mark.parametrize("ocr_binding", BACKENDS, indirect=True)
def test_an_ocr_run_needing_the_engine_refuses_when_it_is_absent(
    ocr_binding: Binding,
    registry: StubRegistry,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> None:
    outcome = invoke(
        ocr_binding,
        registry=registry,
        payload={
            "source": {
                "kind": "inline",
                "filename": "page.png",
                "media_type": "image/png",
                "content_base64": base64.b64encode(b"\x89PNG\r\n\x1a\n").decode("ascii"),
            },
            "page_count": 1,
        },
        budgets=BUDGETS,
        local_backend=local_backend,
        container_backend=container_backend,
    )
    assert outcome.status == "refused"
    assert outcome.envelope.refusal is not None
    assert outcome.envelope.refusal.code is RefusalCode.ENVIRONMENT_DIVERGENCE
    assert outcome.envelope.refusal.detail["required_extra"] == "paddleocr"


def test_the_two_implementations_bind_two_different_environments(
    registry: StubRegistry,
    manifest_path: Path,
    lock_path: Path,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> None:
    """One lock, two engines, two environments — and two pins in the artifact."""

    markdown = _bind(
        "doc.to_markdown",
        "local_env",
        registry,
        manifest_path,
        lock_path,
        local_backend,
        container_backend,
    )
    ocr = _bind(
        "ocr.extract",
        "local_env",
        registry,
        manifest_path,
        lock_path,
        local_backend,
        container_backend,
    )
    assert markdown.extras == MARKDOWN_EXTRAS
    assert ocr.extras == OCR_EXTRAS
    assert markdown.materialization_digest != ocr.materialization_digest
    assert markdown.implementation_digest != ocr.implementation_digest
    assert markdown.snapshot()["extras"] == list(MARKDOWN_EXTRAS)
    assert ocr.snapshot()["extras"] == list(OCR_EXTRAS)


def test_a_backend_switch_does_not_split_track_record(
    registry: StubRegistry,
    manifest_path: Path,
    lock_path: Path,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> None:
    local = _bind(
        "doc.to_markdown",
        "local_env",
        registry,
        manifest_path,
        lock_path,
        local_backend,
        container_backend,
    )
    container = _bind(
        "doc.to_markdown",
        "container",
        registry,
        manifest_path,
        lock_path,
        local_backend,
        container_backend,
    )
    assert local.implementation_digest == container.implementation_digest
    assert local.materialization_digest != container.materialization_digest
    # The extras travel on both snapshots, so the two are comparable.
    assert local.snapshot()["extras"] == container.snapshot()["extras"]
