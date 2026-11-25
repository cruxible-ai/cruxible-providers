"""``@derive``: a function as a pure operation, run directly and through the child harness."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import pytest
from cruxible_provider_runtime import (
    CaptureInputError,
    CaptureView,
    DeriveDefinitionError,
    DeriveInputError,
    DeriveOperation,
    DeriveOutputError,
    Provider,
    ProviderResult,
    decline,
    derive,
    derived_capture,
)
from cruxible_provider_runtime.acquisition import external_capture_result
from cruxible_provider_runtime.backends import CHILD_MODULE
from cruxible_provider_runtime.canonical import canonical_json
from cruxible_provider_runtime.egress import EgressRecorder, no_network
from cruxible_provider_runtime.operations import OperationSource, ProviderOperation
from cruxible_provider_runtime.protocol import (
    PROTOCOL_VERSION,
    Budgets,
    ResultEnvelope,
    RunContext,
    parse_result_envelope,
)
from cruxible_provider_runtime.provider_api import ProviderRunContext

RUNTIME_SRC = Path(__file__).resolve().parents[1] / "src"
OBSERVED = datetime(2026, 11, 4, 8, 30, tzinfo=UTC)
BUDGETS = Budgets(wall_clock_seconds=30.0, output_bytes=1_000_000)
CONTRACTS = {"input_contract": "triage.input@1", "output_contract": "triage.output@1"}


def wire(content: bytes, n: int = 0) -> dict[str, Any]:
    return external_capture_result(
        content,
        source_identity="web.response",
        coordinate_type="http-response-v1",
        coordinate={"url": f"https://example.test/{n}"},
        observed_at=OBSERVED,
        selector_type="whole-response-v1",
    )


ADVISORY = b'{"findings": ["a", "b", "c", "d"]}'


@derive(
    "triage.summarize",
    serves=["triage.summarize", "triage.digest", "triage.summarize"],
    refusals={
        "too_long": "triage.refusal.too_long@1",
        "empty": {"contract": "triage.refusal.empty@1", "absorbable": True},
    },
    **CONTRACTS,
)
def summarize(source: CaptureView, max_items: int = 2) -> dict[str, Any]:
    """Summarizes the advisory's top findings
    into one derived document.

    Everything after the first paragraph is commentary, not the description.
    """

    findings = source.json()["findings"]
    if not findings:
        raise decline("empty", "the advisory lists no findings")
    if max_items > 10:
        raise decline("too_long", "at most ten findings", limit=10)
    return {"summary": source.derive({"findings": findings[:max_items]}), "count": len(findings)}


@derive("triage.count", serves="triage.count", **CONTRACTS)
def count(
    pages: list[CaptureView],
    extra: Sequence[CaptureView] = (),
    pinned: tuple[CaptureView, ...] = (),
    maybe: CaptureView | None = None,
    labelled: Annotated[CaptureView, "a note"] | None = None,
) -> dict[str, Any]:
    """Counts the bytes of every Capture it is given."""

    assert isinstance(pages, list) and isinstance(pinned, tuple)
    every = [
        *pages,
        *extra,
        *pinned,
        *([maybe] if maybe else []),
        *([labelled] if labelled else []),
    ]
    return {
        "bytes": sum(view.byte_length for view in every),
        "kinds": [type(item).__name__ for item in (pages, extra, pinned)],
        "maybe": maybe is None,
    }


@derive("triage.untyped", serves="triage.untyped", capture_inputs=("doc", "docs"), **CONTRACTS)
def untyped(doc, docs, note="-"):  # type: ignore[no-untyped-def]
    """Reads Captures it has no annotations for."""

    return {"doc": doc.text, "docs": [item.text for item in docs], "note": note}


@derive("triage.fields", serves="triage.fields", capture_inputs="source", **CONTRACTS)
def fields(mode: str, **rest: Any) -> dict[str, Any]:
    """Takes every field it does not name through **rest."""

    return {"mode": mode, "rest": sorted(rest), "source": rest["source"].text}


@derive("triage.passthrough", serves="triage.passthrough", **CONTRACTS)
def passthrough(source: CaptureView) -> dict[str, Any]:
    """Returns its input Capture and a ProviderResult-free nested structure."""

    return {"capture": source, "nested": [{"again": source}], "pair": (1, 2)}


@derive("triage.result", serves="triage.result", **CONTRACTS)
def returns_result(text: str) -> ProviderResult:
    """Returns a ProviderResult of its own, with metrics."""

    return ProviderResult.ok({"text": text}, metrics={"length": float(len(text))})


@derive("triage.broken", serves="triage.broken", **CONTRACTS)
def broken(text: str) -> Any:
    """Returns something that is not an output."""

    return [text]


@derive("triage.later", serves="triage.later", **CONTRACTS)
def uses_later(value: Later, source: CaptureView) -> dict[str, Any]:
    """Names a type defined after it."""

    return {"value": value, "bytes": source.byte_length}


class Later:
    pass


@derive("triage.unresolvable", serves="triage.unresolvable", **CONTRACTS)
def unresolvable(value: NowhereDefined) -> dict[str, Any]:  # type: ignore[name-defined]  # noqa: F821
    """Names a type defined nowhere."""

    return {}


def context(operation: str, payload: dict[str, Any]) -> ProviderRunContext:
    return ProviderRunContext(
        run_id="run-derive",
        operation=operation,
        operation_signature_digest="sha256:" + "51" * 32,
        build_digest="sha256:" + "b1" * 32,
        implementation_digest="sha256:" + "11" * 32,
        input=payload,
        coordinates={},
        budgets=BUDGETS,
        declared_endpoints=(),
        capture_contract=None,
        secrets={},
        egress=EgressRecorder(),
    )


# -- the declaration ---------------------------------------------------------------------------


def test_a_derive_is_an_entrypoint_serving_one_pure_operation() -> None:
    assert isinstance(summarize, DeriveOperation)
    assert isinstance(summarize, Provider)
    assert summarize.operations == ("triage.summarize",)
    assert summarize.name == "triage.summarize"
    assert summarize.effect == "pure"
    assert summarize.declared_endpoints == ()
    assert summarize.serves == ("triage.digest", "triage.summarize")
    assert summarize.input_contract == "triage.input@1"
    assert summarize.output_contract == "triage.output@1"
    assert summarize.may_truncate is False
    assert summarize.description == (
        "Summarizes the advisory's top findings into one derived document."
    )
    assert summarize.refusals == {
        "empty": {"contract": "triage.refusal.empty@1", "absorbable": True},
        "too_long": {"contract": "triage.refusal.too_long@1", "absorbable": False},
    }
    assert summarize.capture_fields() == {"source": "one"}
    assert count.capture_fields() == {
        "pages": "list",
        "extra": "list",
        "pinned": "tuple",
        "maybe": "one",
        "labelled": "one",
    }
    assert untyped.capture_fields() == {"doc": "value", "docs": "value"}
    assert fields.capture_fields() == {"source": "value"}


def test_the_decorated_name_keeps_the_function() -> None:
    assert summarize.__name__ == "summarize"
    assert summarize.__wrapped__ is summarize.fn  # type: ignore[attr-defined]
    assert summarize.__module__ == __name__
    assert "commentary" in (summarize.__doc__ or "")
    assert repr(summarize) == f"<derive 'triage.summarize' {__name__}.summarize>"


def test_the_registration_operation_is_the_declaration_at_its_entrypoint() -> None:
    operation = summarize.operation(entrypoint="triage_provider:summarize", source="triage.py")
    assert isinstance(operation, ProviderOperation)
    assert operation.wire() == {
        "name": "triage.summarize",
        "description": "Summarizes the advisory's top findings into one derived document.",
        "source": {"path": "triage.py"},
        "serves": ["triage.digest", "triage.summarize"],
        "in": {"name": "triage.input", "version": 1},
        "out": {"name": "triage.output", "version": 1},
        "effect": "pure",
        "refusals": {
            "empty": {
                "contract": {"name": "triage.refusal.empty", "version": 1},
                "absorbable": True,
            },
            "too_long": {
                "contract": {"name": "triage.refusal.too_long", "version": 1},
                "absorbable": False,
            },
        },
        "entrypoint": "triage_provider:summarize",
    }
    located = OperationSource(
        path="src/triage.py", repository="https://example.test/repo", revision="a" * 40
    )
    assert summarize.operation(entrypoint="m:summarize", source=located).source == located
    with pytest.raises(DeriveDefinitionError, match="entrypoint"):
        summarize.operation(entrypoint="not an entrypoint", source="triage.py")


def test_may_truncate_and_an_explicit_description() -> None:
    @derive(
        "listing.page",
        serves="listing.page",
        description="Lists one page.",
        may_truncate=True,
        **CONTRACTS,
    )
    def page(cursor: str = "") -> dict[str, Any]:
        """Not the description."""

        return {"items": []}

    assert page.description == "Lists one page."
    assert page.may_truncate is True
    assert page.operation(entrypoint="m:page", source="m.py").wire()["may_truncate"] is True


def _plain(value: str = "") -> dict[str, Any]:
    """Does a thing."""

    return {}


def _undocumented() -> dict[str, Any]:
    return {}


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"name": "Not A Name"}, "operation name"),
        ({"serves": "workspace.file"}, "served by core"),
        ({"serves": []}, "serves"),
        ({"serves": "Upper"}, "capability name"),
        ({"input_contract": "triage.input"}, "input_contract"),
        ({"output_contract": "triage.output@x"}, "output_contract"),
        ({"refusals": {"provider_protocol_violation": "x.y@1"}}, "runtime"),
        ({"refusals": {"declined": "not-a-pin"}}, "refusals"),
        ({"description": "x" * 2049}, "description"),
    ],
)
def test_a_declaration_that_is_not_an_operation_is_refused_when_defined(
    arguments: dict[str, Any], message: str
) -> None:
    declared: dict[str, Any] = {"name": "triage.ok", "serves": "triage.ok", **CONTRACTS}
    declared.update(arguments)
    name = declared.pop("name")
    with pytest.raises(DeriveDefinitionError, match=message):
        derive(name, **declared)(_plain)


def test_a_derive_needs_a_description() -> None:
    with pytest.raises(DeriveDefinitionError, match=r"^@derive _undocumented: give the operation"):
        derive("triage.ok", serves="triage.ok", **CONTRACTS)(_undocumented)


def _positional_only(source: CaptureView, /) -> dict[str, Any]:
    """Doc."""
    return {}


def _star_args(*sources: CaptureView) -> dict[str, Any]:
    """Doc."""
    return {}


def _mapping_of_views(sources: dict[str, CaptureView]) -> dict[str, Any]:
    """Doc."""
    return {}


def _view_or_text(source: CaptureView | str) -> dict[str, Any]:
    """Doc."""
    return {}


def _iterable_of_views(sources: list[CaptureView] | tuple[CaptureView]) -> dict[str, Any]:
    """Doc."""
    return {}


async def _coroutine() -> dict[str, Any]:
    """Doc."""
    return {}


def _generator() -> Any:
    """Doc."""
    yield {}


class _NotAFunction:
    """Doc."""


@pytest.mark.parametrize(
    ("target", "capture_inputs", "message"),
    [
        (_positional_only, (), "positional-only"),
        (_star_args, (), "positional-only or \\*args"),
        (_plain, ("missing",), "not parameters"),
        (_mapping_of_views, (), "a Capture parameter is CaptureView"),
        (_view_or_text, (), "a Capture parameter is CaptureView"),
        (_iterable_of_views, (), "a Capture parameter is CaptureView"),
        (_coroutine, (), "not a coroutine"),
        (_generator, (), "not a coroutine or a generator"),
        (_NotAFunction, (), "decorates a function"),
        (summarize, (), "already a derive"),
        (_plain, ("not a field",), "not a field"),
    ],
)
def test_a_signature_that_is_not_a_derive_is_refused_when_defined(
    target: Any, capture_inputs: tuple[str, ...], message: str
) -> None:
    with pytest.raises(DeriveDefinitionError, match=message):
        derive("triage.ok", serves="triage.ok", capture_inputs=capture_inputs, **CONTRACTS)(target)


def test_an_annotation_defined_later_resolves_when_first_used() -> None:
    result = uses_later.run({"value": 1, "source": wire(b"abc")})
    assert result.output == {"value": 1, "bytes": 3}
    assert uses_later.capture_fields() == {"source": "one"}


def test_an_annotation_that_never_resolves_is_reported() -> None:
    with pytest.raises(DeriveDefinitionError, match="do not resolve"):
        unresolvable.check()
    with pytest.raises(DeriveDefinitionError, match="do not resolve"):
        unresolvable.run({"value": 1})


# -- running in plain Python -------------------------------------------------------------------


def test_the_function_itself_is_plain_python() -> None:
    output = summarize.fn(source=CaptureView(wire(ADVISORY)), max_items=1)
    assert output["count"] == 4
    assert CaptureView(output["summary"]).json() == {"findings": ["a"]}


def test_run_hydrates_a_capture_and_returns_the_result() -> None:
    result = summarize.run({"source": wire(ADVISORY)})
    assert result.status == "ok"
    assert result.output is not None
    summary = CaptureView(result.output["summary"])
    assert summary.json() == {"findings": ["a", "b"]}
    assert summary.observed_at == OBSERVED
    assert result.output["count"] == 4
    assert summarize.run({"source": CaptureView(wire(ADVISORY))}) == result


def test_calling_with_a_run_context_is_the_harness_path() -> None:
    result = summarize(context("triage.summarize", {"source": wire(ADVISORY), "max_items": 3}))
    assert result.status == "ok"
    assert result.output is not None
    assert CaptureView(result.output["summary"]).json() == {"findings": ["a", "b", "c"]}


def test_lists_tuples_sequences_and_optionals_of_captures() -> None:
    result = count.run({"pages": [wire(b"ab", 1), wire(b"cde", 2)]})
    assert result.output == {"bytes": 5, "kinds": ["list", "tuple", "tuple"], "maybe": True}
    result = count.run(
        {
            "pages": [],
            "extra": [wire(b"x")],
            "pinned": [wire(b"yy")],
            "maybe": wire(b"zzz"),
            "labelled": None,
        }
    )
    assert result.output == {"bytes": 6, "kinds": ["list", "list", "tuple"], "maybe": False}


def test_capture_inputs_reads_unannotated_fields_by_their_shape() -> None:
    result = untyped.run({"doc": wire(b"one"), "docs": [wire(b"two"), wire(b"three", 3)]})
    assert result.output == {"doc": "one", "docs": ["two", "three"], "note": "-"}


def test_a_fields_parameter_takes_every_field_no_other_parameter_names() -> None:
    result = fields.run({"mode": "m", "source": wire(b"body"), "flag": True})
    assert result.output == {"mode": "m", "rest": ["flag", "source"], "source": "body"}


def test_fields_a_function_does_not_name_are_not_passed() -> None:
    result = summarize.run({"source": wire(ADVISORY), "unrelated": "ignored"})
    assert result.status == "ok"


def test_a_view_in_the_output_is_written_back_as_its_wire() -> None:
    source = wire(b"through")
    result = passthrough.run({"source": source})
    assert result.output == {"capture": source, "nested": [{"again": source}], "pair": [1, 2]}


def test_a_returned_provider_result_is_returned_as_it_is() -> None:
    result = returns_result.run({"text": "abc"})
    assert result == ProviderResult.ok({"text": "abc"}, metrics={"length": 3.0})


def test_a_decline_is_a_refused_result() -> None:
    result = summarize.run({"source": wire(b'{"findings": []}')})
    assert result.status == "refused"
    assert result.refusal is not None
    assert (result.refusal.code, result.refusal.message) == (
        "empty",
        "the advisory lists no findings",
    )
    refused = summarize.run({"source": wire(ADVISORY), "max_items": 11})
    assert refused.refusal is not None and refused.refusal.detail == {"limit": 10}


@pytest.mark.parametrize(
    ("operation", "payload", "error", "message"),
    [
        (summarize, {}, DeriveInputError, "needs input field 'source'"),
        (summarize, {"source": "text"}, DeriveInputError, "not a Capture"),
        (summarize, {"source": None}, DeriveInputError, "is null"),
        (count, {"pages": wire(b"x")}, DeriveInputError, "not a list of Captures"),
        (count, {"pages": [wire(b"x"), 3]}, DeriveInputError, r"pages\[1\]"),
        (
            summarize,
            {"source": {**wire(b"x"), "byte_length": 9}},
            CaptureInputError,
            "input field 'source'.*byte_length",
        ),
        (untyped, {"doc": 5, "docs": []}, DeriveInputError, "not a Capture"),
        (broken, {"text": "x"}, DeriveOutputError, "returned list"),
    ],
)
def test_an_input_or_output_that_does_not_fit_raises(
    operation: DeriveOperation, payload: dict[str, Any], error: type[Exception], message: str
) -> None:
    with pytest.raises(error, match=message):
        operation.run(payload)


def test_run_takes_a_mapping() -> None:
    with pytest.raises(DeriveInputError, match="mapping"):
        summarize.run(["source"])  # type: ignore[arg-type]


def test_a_derive_is_byte_identical_with_the_network_blocked() -> None:
    payload = {"source": wire(ADVISORY), "max_items": 3}
    with no_network():
        first = summarize.run(payload)
        second = summarize.run(json.loads(json.dumps(payload)))
    assert first.status == second.status == "ok"
    assert canonical_json(first.output) == canonical_json(second.output)


def test_a_compute_with_no_capture_input_states_its_observation_time() -> None:
    @derive("triage.compute", serves="triage.compute", **CONTRACTS)
    def compute(watched: list[str], as_of: str) -> dict[str, Any]:
        """Lists the watched names as one document."""

        return {
            "capture": derived_capture(
                {"watched": sorted(watched)}, observed_at=datetime.fromisoformat(as_of)
            )
        }

    result = compute.run({"watched": ["b", "a"], "as_of": "2026-11-04T08:30:00Z"})
    assert result.output is not None
    view = CaptureView(result.output["capture"])
    assert view.json() == {"watched": ["a", "b"]}
    assert view.observed_at == OBSERVED


# -- through the child harness -----------------------------------------------------------------

_MODULE = '''
"""A single-file provider, as an author writes it."""

from cruxible_provider_runtime import CaptureView, decline, derive


@derive(
    "triage.summarize",
    serves="triage.summarize",
    input_contract="triage.input@1",
    output_contract="triage.output@1",
    refusals={"empty": "triage.refusal.empty@1"},
)
def summarize(source: CaptureView, max_items: int = 2) -> dict:
    """Summarizes the advisory's top findings."""
    print("chatter a provider library might print")
    findings = source.json()["findings"]
    if not findings:
        raise decline("empty", "the advisory lists no findings")
    return {"summary": source.derive({"findings": findings[:max_items]})}


@derive(
    "triage.count",
    serves="triage.count",
    input_contract="triage.pages@1",
    output_contract="triage.count@1",
)
def count(pages: list[CaptureView]) -> dict:
    """Counts the bytes of each page."""
    return {"bytes": [page.byte_length for page in pages], "first": pages[0].text}


@derive(
    "triage.broken",
    serves="triage.broken",
    input_contract="triage.input@1",
    output_contract="triage.output@1",
)
def broken(source: CaptureView) -> dict:
    """Returns something that is not an output."""
    return ["not", "a", "mapping"]
'''


@pytest.fixture(scope="module")
def provider_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("derive-provider")
    (root / "derive_sample_provider.py").write_text(textwrap.dedent(_MODULE), encoding="utf-8")
    return root


def run_child(
    root: Path, operation: str, attribute: str, payload: dict[str, Any]
) -> ResultEnvelope:
    entrypoint = f"derive_sample_provider:{attribute}"
    document = RunContext(
        protocol_version=PROTOCOL_VERSION.render(),
        run_id="run-derive-child",
        operation=operation,
        operation_signature_digest="sha256:" + "51" * 32,
        build_digest="sha256:" + "b1" * 32,
        implementation_digest="sha256:" + "1a" * 32,
        entrypoint=entrypoint,
        input=payload,
        budgets=BUDGETS,
    ).model_dump(mode="json")
    completed = subprocess.run(
        [sys.executable, "-m", CHILD_MODULE, "--entrypoint", entrypoint],
        input=json.dumps(document).encode("utf-8"),
        capture_output=True,
        env={**os.environ, "PYTHONPATH": os.pathsep.join((str(RUNTIME_SRC), str(root)))},
        check=True,
        timeout=60,
    )
    return parse_result_envelope(completed.stdout)


def test_the_harness_runs_a_derive_and_returns_its_derived_capture(provider_root: Path) -> None:
    envelope = run_child(provider_root, "triage.summarize", "summarize", {"source": wire(ADVISORY)})
    assert envelope.status == "ok", envelope
    assert envelope.output is not None
    summary = CaptureView(envelope.output["summary"])
    assert summary.json() == {"findings": ["a", "b"]}
    assert summary.observed_at == OBSERVED
    assert envelope.trace.endpoints_contacted == []
    # Across the pipe, byte for byte the wire the same derivation makes in process.
    expected = CaptureView(wire(ADVISORY)).derive({"findings": ["a", "b"]})
    assert envelope.output == {"summary": expected}


def test_the_harness_hydrates_a_list_of_captures(provider_root: Path) -> None:
    envelope = run_child(
        provider_root, "triage.count", "count", {"pages": [wire(b"first", 1), wire(b"xy", 2)]}
    )
    assert envelope.status == "ok", envelope
    assert envelope.output == {"bytes": [5, 2], "first": "first"}


def test_the_harness_returns_a_decline_as_a_refusal(provider_root: Path) -> None:
    envelope = run_child(
        provider_root, "triage.summarize", "summarize", {"source": wire(b'{"findings": []}')}
    )
    assert envelope.status == "refused"
    assert envelope.refusal is not None
    assert envelope.refusal.code == "empty"


@pytest.mark.parametrize(
    ("attribute", "operation", "payload", "kind"),
    [
        ("broken", "triage.broken", {"source": wire(b"x")}, "DeriveOutputError"),
        ("summarize", "triage.summarize", {}, "DeriveInputError"),
        (
            "summarize",
            "triage.summarize",
            {"source": {**wire(b"x"), "bytes_digest": "sha256:" + "0" * 64}},
            "CaptureInputError",
        ),
    ],
)
def test_the_harness_reports_a_misfit_as_a_provider_error(
    provider_root: Path, attribute: str, operation: str, payload: dict[str, Any], kind: str
) -> None:
    envelope = run_child(provider_root, operation, attribute, payload)
    assert envelope.status == "error"
    assert envelope.error is not None
    assert envelope.error.kind == kind


def test_the_harness_refuses_an_operation_the_derive_does_not_serve(provider_root: Path) -> None:
    envelope = run_child(provider_root, "triage.count", "summarize", {"source": wire(ADVISORY)})
    assert envelope.status == "refused"
    assert envelope.refusal is not None
    assert envelope.refusal.code == "undeclared_operation"
