"""Structural agreement between provider emissions and interface refusal schemas."""

from __future__ import annotations

import ast
import inspect
from dataclasses import dataclass
from types import ModuleType
from typing import Any

import cruxible_provider_quant.interfaces as quant_interfaces
import cruxible_provider_quant.outputs as quant_outputs
import pytest
from cruxible_provider_docs import documents as docs_documents
from cruxible_provider_docs import engines as docs_engines
from cruxible_provider_docs import interfaces as docs_interfaces
from cruxible_provider_docs import ocr, to_markdown
from cruxible_provider_noop import interface as noop_interface
from cruxible_provider_noop import provider as noop_provider
from cruxible_provider_quant import (
    anomaly,
    calibrate,
    forecast,
    linkage,
    rank,
    reduce,
    stat_test,
)
from cruxible_provider_web import engines as web_engines
from cruxible_provider_web import fetch, http, search
from cruxible_provider_web import interfaces as web_interfaces


@dataclass(frozen=True)
class InterfaceCase:
    interface_id: str
    preimage: dict[str, Any]
    implementation: ModuleType
    shared_modules: tuple[ModuleType, ...] = ()
    required_helpers: tuple[str, ...] = ()


# This is deliberately the provider implementation closure only. Admission,
# budgets, secrets, protocol, backend, and executor egress refusals belong to
# the runtime vocabulary and do not enter a per-interface refusal schema merely
# because the runtime can raise them around any implementation.
INTERFACES = [
    InterfaceCase("noop.echo", noop_interface.INTERFACE_PREIMAGE, noop_provider),
    InterfaceCase(
        "web.fetch",
        web_interfaces.FETCH_PREIMAGE,
        fetch,
        (web_engines, http),
    ),
    InterfaceCase("search.web", web_interfaces.SEARCH_PREIMAGE, search),
    InterfaceCase(
        "doc.to_markdown",
        docs_interfaces.MARKDOWN_PREIMAGE,
        to_markdown,
        (docs_documents, docs_engines),
    ),
    InterfaceCase(
        "ocr.extract",
        docs_interfaces.OCR_PREIMAGE,
        ocr,
        (docs_documents, docs_engines),
    ),
    InterfaceCase(
        "calc.reduce",
        quant_interfaces.INTERFACE_PREIMAGES["calc.reduce"],
        reduce,
        (quant_outputs,),
        ("ok_if_finite",),
    ),
    InterfaceCase(
        "ts.anomaly",
        quant_interfaces.INTERFACE_PREIMAGES["ts.anomaly"],
        anomaly,
        (quant_outputs,),
        ("ok_if_finite",),
    ),
    InterfaceCase(
        "ts.forecast",
        quant_interfaces.INTERFACE_PREIMAGES["ts.forecast"],
        forecast,
        (quant_outputs,),
        ("ok_if_finite",),
    ),
    InterfaceCase(
        "stat.test",
        quant_interfaces.INTERFACE_PREIMAGES["stat.test"],
        stat_test,
        (quant_outputs,),
        ("ok_if_finite",),
    ),
    InterfaceCase(
        "score.rank",
        quant_interfaces.INTERFACE_PREIMAGES["score.rank"],
        rank,
        (quant_outputs,),
        ("ok_if_finite",),
    ),
    InterfaceCase(
        "match.record",
        quant_interfaces.INTERFACE_PREIMAGES["match.record"],
        linkage,
        (quant_outputs,),
        ("ok_if_finite",),
    ),
    InterfaceCase(
        "calc.calibrate",
        quant_interfaces.INTERFACE_PREIMAGES["calc.calibrate"],
        calibrate,
        (quant_outputs,),
        ("ok_if_finite",),
    ),
]


def _literal_refusal_codes(source: str) -> set[str]:
    """Read literal ``RefusalCode.MEMBER`` references or reject the source shape."""

    tree = ast.parse(source)
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}

    aliases = [
        alias.asname
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
        if alias.name == "RefusalCode" and alias.asname is not None
    ]
    assert not aliases, f"aliased RefusalCode imports are outside the structural guard: {aliases}"

    qualified = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "RefusalCode"
    ]
    assert not qualified, "module-qualified RefusalCode references are outside the structural guard"

    codes: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Name) or node.id != "RefusalCode":
            continue
        parent = parents.get(node)
        assert isinstance(parent, ast.Attribute) and parent.value is node, (
            "RefusalCode must appear only as a plain RefusalCode.MEMBER attribute"
        )
        codes.add(parent.attr.lower())
    return codes


@pytest.mark.parametrize("case", INTERFACES, ids=lambda case: case.interface_id)
def test_every_implementation_refusal_is_declared_by_its_interface(case: InterfaceCase) -> None:
    """Each schema is exhaustive over the literal emissions in its implementation closure."""

    implementation_source = inspect.getsource(case.implementation)
    calls = {
        node.func.id
        for node in ast.walk(ast.parse(implementation_source))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert set(case.required_helpers) <= calls

    emitted: set[str] = set()
    for module in (case.implementation, *case.shared_modules):
        emitted.update(_literal_refusal_codes(inspect.getsource(module)))

    assert "decline_reasons" not in case.preimage
    assert set(case.preimage["refusals"]) == emitted


UNSUPPORTED_REFERENCE_FORMS = [
    pytest.param(
        "from somewhere import RefusalCode as RC\ncode = RC.PROVIDER_DECLINED\n",
        "aliased RefusalCode",
        id="aliased-import",
    ),
    pytest.param(
        "code = getattr(RefusalCode, 'PROVIDER_DECLINED')\n",
        "plain RefusalCode.MEMBER",
        id="getattr",
    ),
    pytest.param(
        "CODES = {'refuse': 'provider_declined'}\ncode = RefusalCode(CODES['refuse'])\n",
        "plain RefusalCode.MEMBER",
        id="dict-lookup",
    ),
    pytest.param(
        "import errors as _errmod\ncode = _errmod.RefusalCode.PROVIDER_DECLINED\n",
        "module-qualified RefusalCode",
        id="module-attribute",
    ),
]


@pytest.mark.parametrize(("source", "message"), UNSUPPORTED_REFERENCE_FORMS)
def test_the_refusal_scanner_rejects_non_literal_reference_forms(source: str, message: str) -> None:
    """Unsupported reference forms must break the guard instead of hiding emissions."""

    with pytest.raises(AssertionError, match=message):
        _literal_refusal_codes(source)
