"""Purity conformance: every ``effect: pure`` operation is a function of its input.

A derive must declare ``effect: pure`` (D-R37 b5), and core takes that as the
provider's word: identical input gives byte-identical output, so a re-run makes
the same Capture and its Claims re-observe instead of revising. The word is
backed here, for every operation any package in this repository declares pure:

* run twice in process on the same input, the two outputs are byte-identical
  after canonical serialisation -- the whole output, a Capture's wire included,
  so an observation time read from the clock fails;
* both runs happen with outbound sockets blocked (``egress.no_network``), so an
  operation that reaches the network fails instead of answering.

A pure operation without a case below fails
``test_every_pure_operation_has_a_case``. Cases are chosen from each package's
own default-lane inputs: an operation whose engine is a heavy extra replays its
packaged recording, as its own suite does.
"""

from __future__ import annotations

import base64
import hashlib
import importlib
import importlib.util
import json
import sys
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from cruxible_provider_runtime.acquisition import external_capture_result
from cruxible_provider_runtime.canonical import canonical_json
from cruxible_provider_runtime.egress import EgressRecorder, no_network
from cruxible_provider_runtime.manifest import load_manifest
from cruxible_provider_runtime.protocol import Budgets
from cruxible_provider_runtime.provider_api import ProviderResult, ProviderRunContext

ROOT = Path(__file__).resolve().parents[1]
BUDGETS = Budgets(wall_clock_seconds=60.0, output_bytes=8_000_000)
REGISTRATIONS = sorted(ROOT.glob("packages/*/src/*/registration.json"))


def _pure_operations() -> dict[str, tuple[str, tuple[str, ...]]]:
    """Every pure operation: its entrypoint and its implementation's declared endpoints.

    Read from each file's ``operations`` directly rather than through
    ``load_registration``: purity is a property of the code, and two packages
    whose registrations are not schema 3 yet (``tests/test_package_registration.py``
    ``PENDING``) still declare pure operations that must hold it.
    """

    found: dict[str, tuple[str, tuple[str, ...]]] = {}
    for descriptor in REGISTRATIONS:
        operations = json.loads(descriptor.read_bytes())["operations"]
        manifest = load_manifest(descriptor.parent / "manifest.yaml")
        for operation in operations:
            if operation["effect"] == "pure":
                entrypoint = operation["entrypoint"]
                declared = manifest.implementation(entrypoint).declared_endpoints
                found[operation["name"]] = (entrypoint, declared)
    return found


PURE = _pure_operations()


def _run(operation: str, payload: Mapping[str, Any]) -> ProviderResult:
    entrypoint, declared = PURE[operation]
    module, _, member = entrypoint.partition(":")
    provider = getattr(importlib.import_module(module), member)()
    return provider(  # type: ignore[no-any-return]
        ProviderRunContext(
            run_id="run-purity",
            operation=operation,
            operation_signature_digest="sha256:" + "51" * 32,
            build_digest="sha256:" + "b1" * 32,
            implementation_digest="sha256:" + "11" * 32,
            input=payload,
            coordinates={},
            budgets=BUDGETS,
            declared_endpoints=declared,
            capture_contract=None,
            secrets={},
            egress=EgressRecorder(),
        )
    )


def _load(path: Path, name: str) -> Any:
    """A package test module by path: package test directories are not packages."""

    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses resolve their module by name
    spec.loader.exec_module(module)
    return module


def _web_source() -> dict[str, Any]:
    """A fetched web Capture as core hands it to a derive: its wire."""

    from cruxible_provider_web.recordings import load_recordings

    recording = load_recordings()["article-static"]
    return external_capture_result(
        recording.response.body.encode("utf-8"),
        source_identity="web.response",
        coordinate_type="http-response-v1",
        coordinate={
            "url": recording.request_url,
            "final_url": recording.request_url,
            "status": 200,
            "content_type": recording.response.headers["content-type"],
            "request_digest": "sha256:"
            + hashlib.sha256(recording.request_url.encode("utf-8")).hexdigest(),
            "source": "packaged-recording",
        },
        observed_at=datetime(2026, 11, 4, 8, 30, tzinfo=UTC),
        selector_type="whole-response-v1",
    )


def _docs_inputs() -> list[tuple[str, str, dict[str, Any]]]:
    csv = b"reach,nitrate_mg_l\nUpper,4.1\nLower,9.2\n"
    from cruxible_provider_docs.recordings import load_recordings

    scan = load_recordings()["scan-clean"].document
    return [
        (
            "doc.to_markdown",
            "inline-csv",
            {
                "source": {
                    "kind": "inline",
                    "filename": "reach-readings.csv",
                    "media_type": "text/csv",
                    "content_base64": base64.b64encode(csv).decode("ascii"),
                },
                "layout": "tabular",
            },
        ),
        (
            "ocr.extract",
            "scan-clean",
            {
                "source": {
                    "kind": "packaged_fixture",
                    "id": "scan-clean",
                    "filename": scan.filename,
                    "media_type": scan.media_type,
                },
                "page_count": 1,
            },
        ),
    ]


def _cases() -> dict[str, list[tuple[str, Callable[[], dict[str, Any]]]]]:
    cases: dict[str, list[tuple[str, Callable[[], dict[str, Any]]]]] = {
        "noop.echo": [("echo", lambda: {"mode": "echo", "text": "tide"})],
        "noop.captures": [("two-files", lambda: {"files": ["gamma.txt", "alpha.json"]})],
    }
    if importlib.util.find_spec("trafilatura") is not None:
        cases["web.render"] = [
            (form, lambda form=form: {"form": form, "source": _web_source()})
            for form in ("json", "text")
        ]
    for operation, case_id, payload in _docs_inputs():
        cases.setdefault(operation, []).append((case_id, lambda payload=payload: payload))
    quant = _load(ROOT / "packages/cruxible-provider-quant/tests/fixtures.py", "quant_fixtures")
    for fixture in quant.FIXTURES:
        cases.setdefault(fixture.operation, []).append(
            (fixture.fixture_id, lambda fixture=fixture: dict(fixture.payload))
        )
    return cases


CASES = _cases()


def test_every_pure_operation_has_a_case() -> None:
    missing = sorted(set(PURE) - set(CASES))
    if missing == ["web.render"] and importlib.util.find_spec("trafilatura") is None:
        pytest.skip("web.render's cases need trafilatura, a base dependency")
    assert not missing, f"pure operations without a purity case: {missing}"
    assert set(CASES) <= set(PURE), sorted(set(CASES) - set(PURE))


@pytest.mark.parametrize(
    ("operation", "payload"),
    [(name, payload) for name in sorted(CASES) for _id, payload in CASES[name]],
    ids=[f"{name}:{case_id}" for name in sorted(CASES) for case_id, _payload in CASES[name]],
)
def test_a_pure_operation_is_byte_identical_with_the_network_blocked(
    operation: str, payload: Callable[[], dict[str, Any]]
) -> None:
    with no_network():
        first = _run(operation, payload())
        second = _run(operation, payload())
    assert first.status == "ok", first.refusal or first.error
    assert second.status == "ok", second.refusal or second.error
    assert canonical_json(first.output) == canonical_json(second.output)
