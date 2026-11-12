"""``noop.captures``: the reference list-of-Captures operation (D-R37 a).

One call returns one Capture per recorded file the run names, each its own
member of the ``list<capture<*>>`` out field. Held here: every member's stored
bytes are the recorded file's exact bytes, no member is a wrapper, every member
has its own coordinate (its identity, a2), the order is the run's, two runs are
byte-identical, and the whole loop runs through a real child on both backends.
"""

from __future__ import annotations

from pathlib import Path

import cruxible_provider_noop
import pytest
from cruxible_provider_noop.captures import (
    OPERATION,
    PROVIDER_DECLINED,
    RECORDINGS_DIR,
    NoopCaptures,
    recorded_files,
)
from cruxible_provider_runtime.backends import ContainerBackend, LocalEnvBackend
from cruxible_provider_runtime.binding import BindRequest, bind
from cruxible_provider_runtime.egress import EgressRecorder, no_network
from cruxible_provider_runtime.execute import invoke
from cruxible_provider_runtime.manifest import BackendKind
from cruxible_provider_runtime.operations import ContractRef
from cruxible_provider_runtime.protocol import Budgets
from cruxible_provider_runtime.provider_api import ProviderResult, ProviderRunContext
from cruxible_provider_runtime.registration import load_registration
from cruxible_provider_runtime.registry import StubRegistry
from cruxible_provider_runtime.testing import assert_payload_verbatim, captured_members

from .conftest import MARKER_ENVIRONMENT

BUDGETS = Budgets(wall_clock_seconds=30.0, output_bytes=4_000_000)
FILES = ["gamma.txt", "alpha.json", "beta.csv"]
OUT = (
    load_registration(cruxible_provider_noop.PACKAGE_ROOT)
    .contract(ContractRef.parse("noop.captures.result@1"))
    .model_dump(mode="json")
)


def _context(files: object) -> ProviderRunContext:
    return ProviderRunContext(
        run_id="run-captures",
        operation=OPERATION,
        operation_signature_digest="sha256:" + "aa" * 32,
        build_digest="sha256:" + "cc" * 32,
        implementation_digest="sha256:" + "bb" * 32,
        input={"files": files},
        coordinates={},
        budgets=BUDGETS,
        declared_endpoints=(),
        capture_contract=None,
        secrets={},
        egress=EgressRecorder(),
    )


def _call(files: object) -> ProviderResult:
    return NoopCaptures()(_context(files))


def test_the_registration_declares_a_capture_list_out_field() -> None:
    operation = load_registration(cruxible_provider_noop.PACKAGE_ROOT).operation(OPERATION)
    assert operation.effect == "pure"
    assert operation.out.spelling == "noop.captures.result@1"
    assert OUT["fields"]["captures"]["type"]["item"] == {"kind": "capture", "capture_kind": "*"}


def test_each_named_file_is_its_own_capture_verbatim_in_order() -> None:
    result = _call(FILES)
    assert result.status == "ok", result
    members = captured_members(result.output, OUT)
    assert_payload_verbatim(members)
    assert [member.at for member in members] == ["captures/0", "captures/1", "captures/2"]
    for name, member in zip(FILES, members, strict=True):
        assert member.stored == (RECORDINGS_DIR / name).read_bytes()
        assert member.coordinate["file"] == name
    assert len({member.coordinate["request_digest"] for member in members}) == len(FILES)
    assert result.metrics == {"capture_count": 3.0}


def test_no_file_named_is_an_empty_list() -> None:
    result = _call([])
    assert result.status == "ok"
    assert result.output == {"captures": []}


def test_two_runs_are_byte_identical_with_the_network_blocked() -> None:
    with no_network():
        first, second = _call(FILES), _call(list(FILES))
    assert first.output == second.output


@pytest.mark.parametrize(
    ("files", "detail"),
    [
        ("alpha.json", {}),
        ([1], {}),
        (["missing.bin"], {"file": "missing.bin", "known": sorted(recorded_files())}),
        (["alpha.json", "alpha.json"], {}),
        (["alpha.json"] * 17, {}),
    ],
    ids=["not-a-list", "not-names", "unknown", "named-twice", "over-max-items"],
)
def test_a_list_it_cannot_return_is_declined(files: object, detail: dict[str, object]) -> None:
    result = _call(files)
    assert result.status == "refused" and result.refusal is not None
    assert result.refusal.code == PROVIDER_DECLINED
    assert dict(result.refusal.detail) == detail


@pytest.mark.parametrize("backend_kind", ("local_env", "container"))
def test_the_list_crosses_the_child_boundary_whole(
    backend_kind: BackendKind,
    registry: StubRegistry,
    manifest_path: Path,
    lock_path: Path,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> None:
    binding = bind(
        registry,
        BindRequest(
            provider_id="cruxible-provider-noop",
            operation=OPERATION,
            backend_kind=backend_kind,
            manifest_path=manifest_path,
            lock_path=lock_path,
            marker_environment=MARKER_ENVIRONMENT,
            allow_editable_dev_sources=True,
        ),
        local_backend=local_backend,
        container_backend=container_backend,
    )
    outcome = invoke(
        binding,
        registry=registry,
        payload={"files": FILES},
        budgets=BUDGETS,
        local_backend=local_backend,
        container_backend=container_backend,
    )
    assert outcome.status == "ok", outcome.envelope
    assert outcome.envelope.output == _call(FILES).output
    assert outcome.egress.observed == ()
