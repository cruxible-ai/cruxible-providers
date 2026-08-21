"""The reference provider's full bind-and-invoke loop, on both backend kinds.

Every assertion here is about a rule the RP-0 contract states, exercised end to
end through a real child process: the provider is imported, run, and read back
across a pipe, with credential material arriving on an inherited descriptor.
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

import cruxible_provider_runtime.binding as binding_module
import pytest
import yaml
from cruxible_provider_noop.provider import CREDENTIAL_REF
from cruxible_provider_runtime.artifact import ProviderArtifactPayload
from cruxible_provider_runtime.backends import ContainerBackend, LocalEnvBackend
from cruxible_provider_runtime.binding import Binding, BindRequest, bind
from cruxible_provider_runtime.digests import implementation_digest
from cruxible_provider_runtime.errors import RefusalCode, RefusalError
from cruxible_provider_runtime.execute import invoke
from cruxible_provider_runtime.manifest import BackendKind
from cruxible_provider_runtime.protocol import Budgets
from cruxible_provider_runtime.registry import StubRegistry
from cruxible_provider_runtime.resolution import ResolvedSet

from .conftest import DISTRIBUTION_SHA256, MARKER_ENVIRONMENT

DUMMY_CREDENTIAL = "dummy-credential-c0ffee-do-not-use"
BUDGETS = Budgets(wall_clock_seconds=30.0, output_bytes=4_000_000)
BACKENDS: tuple[BackendKind, ...] = ("local_env", "container")


def _bind(
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
            provider_id="cruxible-provider-noop",
            interface_id="noop.echo",
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
def binding(
    request: pytest.FixtureRequest,
    registry: StubRegistry,
    manifest_path: Path,
    lock_path: Path,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> Binding:
    backend_kind: BackendKind = getattr(request, "param", "local_env")
    return _bind(backend_kind, registry, manifest_path, lock_path, local_backend, container_backend)


@pytest.mark.parametrize("binding", BACKENDS, indirect=True)
def test_success_path(
    binding: Binding,
    registry: StubRegistry,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> None:
    outcome = invoke(
        binding,
        registry=registry,
        payload={"text": "hello", "mode": "echo"},
        budgets=BUDGETS,
        local_backend=local_backend,
        container_backend=container_backend,
    )
    assert outcome.status == "ok"
    assert outcome.envelope.output == {
        "echo": "hello",
        "input_bucket": "payload_size=tiny;charset=ascii",
    }
    assert outcome.input_bucket == "payload_size=tiny;charset=ascii"
    assert outcome.egress.observed == ()
    assert outcome.receipt_fields()["implementation_digest"] == binding.implementation_digest


@pytest.mark.parametrize("binding", BACKENDS, indirect=True)
def test_typed_refusal_path(
    binding: Binding,
    registry: StubRegistry,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> None:
    outcome = invoke(
        binding,
        registry=registry,
        payload={"text": "hello", "mode": "refuse"},
        budgets=BUDGETS,
        local_backend=local_backend,
        container_backend=container_backend,
    )
    assert outcome.status == "refused"
    assert outcome.envelope.refusal is not None
    assert outcome.envelope.refusal.code is RefusalCode.PROVIDER_DECLINED
    assert outcome.envelope.output is None


@pytest.mark.parametrize("binding", BACKENDS, indirect=True)
def test_error_path_is_reported_not_raised(
    binding: Binding,
    registry: StubRegistry,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> None:
    outcome = invoke(
        binding,
        registry=registry,
        payload={"text": "hello", "mode": "error"},
        budgets=BUDGETS,
        local_backend=local_backend,
        container_backend=container_backend,
    )
    assert outcome.status == "error"
    assert outcome.envelope.error is not None
    assert outcome.envelope.error.kind == "RuntimeError"


@pytest.mark.parametrize("binding", BACKENDS, indirect=True)
def test_credential_arrives_by_ref_over_the_descriptor(
    binding: Binding,
    registry: StubRegistry,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> None:
    outcome = invoke(
        binding,
        registry=registry,
        payload={"text": "hello", "mode": "credential"},
        budgets=BUDGETS,
        secrets={CREDENTIAL_REF: DUMMY_CREDENTIAL},
        local_backend=local_backend,
        container_backend=container_backend,
    )
    assert outcome.status == "ok"
    assert outcome.envelope.output is not None
    assert outcome.envelope.output["credential_length"] == len(DUMMY_CREDENTIAL)


@pytest.mark.parametrize("binding", BACKENDS, indirect=True)
def test_undeclared_egress_refuses_and_names_the_implementation(
    binding: Binding,
    registry: StubRegistry,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> None:
    with pytest.raises(RefusalError) as exc:
        invoke(
            binding,
            registry=registry,
            payload={"text": "hello", "mode": "egress"},
            budgets=BUDGETS,
            local_backend=local_backend,
            container_backend=container_backend,
        )
    assert exc.value.code is RefusalCode.UNDECLARED_EGRESS
    assert exc.value.refusal.detail["implementation_digest"] == binding.implementation_digest


@pytest.mark.parametrize("binding", BACKENDS, indirect=True)
def test_wall_clock_breach_is_a_refusal_not_a_provider_error(
    binding: Binding,
    registry: StubRegistry,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> None:
    with pytest.raises(RefusalError) as exc:
        invoke(
            binding,
            registry=registry,
            payload={"text": "hello", "mode": "slow"},
            budgets=Budgets(wall_clock_seconds=1.0, output_bytes=1_000_000),
            local_backend=local_backend,
            container_backend=container_backend,
        )
    assert exc.value.code is RefusalCode.BUDGET_WALL_CLOCK


@pytest.mark.parametrize("binding", BACKENDS, indirect=True)
def test_output_size_breach_is_a_refusal(
    binding: Binding,
    registry: StubRegistry,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> None:
    with pytest.raises(RefusalError) as exc:
        invoke(
            binding,
            registry=registry,
            payload={"text": "hello", "mode": "loud"},
            budgets=Budgets(wall_clock_seconds=30.0, output_bytes=131_072),
            local_backend=local_backend,
            container_backend=container_backend,
        )
    assert exc.value.code is RefusalCode.BUDGET_OUTPUT_SIZE


@pytest.mark.parametrize("binding", BACKENDS, indirect=True)
def test_unclaimed_bucket_refuses_before_any_process_starts(
    binding: Binding,
    registry: StubRegistry,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
    container_driver: object,
) -> None:
    with pytest.raises(RefusalError) as exc:
        invoke(
            binding,
            registry=registry,
            payload={"text": "x" * 2000, "mode": "echo"},
            budgets=BUDGETS,
            local_backend=local_backend,
            container_backend=container_backend,
        )
    assert exc.value.code is RefusalCode.UNCLAIMED_BUCKET
    assert exc.value.refusal.detail["bucket"] == "payload_size=large;charset=ascii"


@pytest.mark.parametrize("binding", BACKENDS, indirect=True)
def test_unclassifiable_input_refuses(
    binding: Binding,
    registry: StubRegistry,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> None:
    with pytest.raises(RefusalError) as exc:
        invoke(
            binding,
            registry=registry,
            payload={"mode": "echo"},
            budgets=BUDGETS,
            local_backend=local_backend,
            container_backend=container_backend,
        )
    assert exc.value.code is RefusalCode.UNCLASSIFIED_INPUT


def test_unicode_input_classifies_into_its_own_bucket(
    binding: Binding,
    registry: StubRegistry,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> None:
    outcome = invoke(
        binding,
        registry=registry,
        payload={"text": "héllo", "mode": "echo"},
        budgets=BUDGETS,
        local_backend=local_backend,
        container_backend=container_backend,
    )
    assert outcome.input_bucket == "payload_size=tiny;charset=unicode"


def test_implementation_digest_is_identical_across_backends(
    registry: StubRegistry,
    manifest_path: Path,
    lock_path: Path,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> None:
    """A backend switch must never split earned track record."""

    local = _bind("local_env", registry, manifest_path, lock_path, local_backend, container_backend)
    container = _bind(
        "container", registry, manifest_path, lock_path, local_backend, container_backend
    )
    assert local.implementation_digest == container.implementation_digest
    assert local.materialization_digest != container.materialization_digest


def test_implementation_digest_matches_the_documented_preimage(
    binding: Binding, accepted_artifact: ProviderArtifactPayload
) -> None:
    assert binding.implementation_digest == implementation_digest(
        interface_id="noop.echo",
        interface_digest=binding.interface_digest,
        entrypoint="cruxible_provider_noop.provider:NoopEcho",
        distribution_sha256=DISTRIBUTION_SHA256,
    )
    assert accepted_artifact.distribution.sha256 == DISTRIBUTION_SHA256


def test_binding_snapshot_carries_all_three_identity_levels(binding: Binding) -> None:
    snapshot = binding.snapshot()
    assert snapshot["implementation_digest"] == binding.implementation_digest
    assert snapshot["materialization_digest"] == binding.materialization_digest
    assert snapshot["protocol_version"] == "1.0"


def test_the_snapshot_always_states_whether_dev_sources_were_permitted(
    binding: Binding,
) -> None:
    """Present in both directions, never absent-means-false.

    This suite binds with the escape hatch set, because the reference package
    depends on the runtime by path. A consumer that has never heard of the key
    would otherwise read this pin as a production one.
    """

    snapshot = binding.snapshot()
    assert "dev_sources_permitted" in snapshot
    assert snapshot["dev_sources_permitted"] is True


def test_a_production_pin_states_dev_sources_permitted_false(binding: Binding) -> None:
    """The false case is emitted too, which is the half that is easy to forget."""

    production = replace(binding, dev_sources_permitted=False)
    snapshot = production.snapshot()
    assert "dev_sources_permitted" in snapshot
    assert snapshot["dev_sources_permitted"] is False


def test_no_closure_digest_ever_reaches_a_snapshot_or_a_receipt(
    binding: Binding,
    registry: StubRegistry,
    resolved: ResolvedSet,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> None:
    """Only two digests are identity; the third is a packaging-gate instrument.

    The closure digest exists so the one-package-one-change gate can measure
    dependency movement without the root distribution sha256 drowning out the
    signal. It is not an identity and must never be pinned to a track record, so
    its value is asserted absent from everything that gets recorded -- keys and
    values alike.
    """

    from cruxible_provider_runtime.digests import (
        dependency_closure_digest,
        implementation_digest,
        materialization_digest,
    )

    closure = dependency_closure_digest(resolved)
    implementation = implementation_digest(
        interface_id="noop.echo",
        interface_digest=binding.interface_digest,
        entrypoint="cruxible_provider_noop.provider:NoopEcho",
        distribution_sha256=DISTRIBUTION_SHA256,
    )
    materialization = materialization_digest(resolved, distribution_sha256=DISTRIBUTION_SHA256)
    assert len({closure, implementation, materialization}) == 3

    outcome = invoke(
        binding,
        registry=registry,
        payload={"text": "hello", "mode": "echo"},
        budgets=BUDGETS,
        local_backend=local_backend,
        container_backend=container_backend,
    )
    recorded = json.dumps({"snapshot": binding.snapshot(), "receipt": outcome.receipt_fields()})

    assert closure not in recorded
    assert "closure" not in recorded
    assert implementation in recorded
    assert materialization in recorded


def test_the_closure_digest_is_not_on_the_packages_public_surface() -> None:
    """A core executor importing the package sees two digest functions, not three."""

    import cruxible_provider_runtime as runtime

    assert "dependency_closure_digest" not in runtime.__all__
    assert "CLOSURE_DOMAIN_TAG" not in runtime.__all__
    assert not hasattr(runtime, "dependency_closure_digest")
    assert not hasattr(runtime, "CLOSURE_DOMAIN_TAG")
    assert "implementation_digest" in runtime.__all__
    assert "materialization_digest" in runtime.__all__


def test_second_bind_reuses_the_verified_cache_entry(
    registry: StubRegistry,
    manifest_path: Path,
    lock_path: Path,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
    builder: object,
) -> None:
    _bind("local_env", registry, manifest_path, lock_path, local_backend, container_backend)
    _bind("local_env", registry, manifest_path, lock_path, local_backend, container_backend)
    assert len(builder.builds) == 1  # type: ignore[attr-defined]


@pytest.mark.parametrize("binding", BACKENDS, indirect=True)
def test_replacing_the_accepted_artifact_refuses_a_binding_already_made(
    binding: Binding,
    registry: StubRegistry,
    accepted_artifact: ProviderArtifactPayload,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> None:
    """A binding is reusable; the acceptance behind it is not immutable.

    Re-registering the provider against a different index is an ordinary
    governance act. Every later call through a binding made before it must stop,
    or the acceptance a receipt names is not the one the run happened under.
    """

    moved = accepted_artifact.model_copy(
        update={
            "distribution": accepted_artifact.distribution.model_copy(
                update={"index_url": "https://other-index.example/simple"}
            )
        }
    )
    registry.register_provider(moved)

    with pytest.raises(RefusalError) as exc:
        invoke(
            binding,
            registry=registry,
            payload={"text": "hello", "mode": "echo"},
            budgets=BUDGETS,
            local_backend=local_backend,
            container_backend=container_backend,
        )
    assert exc.value.code is RefusalCode.ACCEPTANCE_DIVERGENCE
    assert exc.value.refusal.detail["bound"] == binding.accepted_artifact_digest


@pytest.mark.parametrize("backend_kind", BACKENDS)
def test_editing_the_package_manifest_refuses_a_binding_already_made(
    backend_kind: BackendKind,
    registry: StubRegistry,
    manifest_path: Path,
    lock_path: Path,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
    tmp_path: Path,
) -> None:
    """The contract re-digests the manifest at bind *and* invoke.

    Bound through a copy so the edit lands on a throwaway file. A byte-identical
    copy digests identically, because the digest is over the parsed manifest and
    not over its bytes.
    """

    copied = tmp_path / "manifest.yaml"
    copied.write_bytes(manifest_path.read_bytes())
    binding = _bind(backend_kind, registry, copied, lock_path, local_backend, container_backend)

    document = yaml.safe_load(copied.read_text(encoding="utf-8"))
    document["implementations"][0]["side_effects"] = True
    copied.write_text(yaml.safe_dump(document), encoding="utf-8")

    with pytest.raises(RefusalError) as exc:
        invoke(
            binding,
            registry=registry,
            payload={"text": "hello", "mode": "echo"},
            budgets=BUDGETS,
            local_backend=local_backend,
            container_backend=container_backend,
        )
    assert exc.value.code is RefusalCode.MANIFEST_DIVERGENCE
    assert exc.value.refusal.detail["bound"] == binding.manifest_digest


def test_tampering_with_the_materialized_tree_refuses_a_binding_already_made(
    binding: Binding,
    registry: StubRegistry,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> None:
    """The seal bind computed is held on the binding, outside the tree it covers.

    Whoever rewrites a cache entry can rewrite its seal file along with it, so
    the tree's self-consistency proves nothing on its own. The copy the binding
    kept is what the invoke-time comparison is against.
    """

    assert binding.env_path is not None
    (binding.env_path / "smuggled.py").write_text("raise SystemExit(0)\n", encoding="utf-8")

    with pytest.raises(RefusalError) as exc:
        invoke(
            binding,
            registry=registry,
            payload={"text": "hello", "mode": "echo"},
            budgets=BUDGETS,
            local_backend=local_backend,
            container_backend=container_backend,
        )
    assert exc.value.code is RefusalCode.CACHE_INTEGRITY
    assert exc.value.refusal.detail["sealed"] == binding.sealed_tree_digest


def test_an_untouched_environment_is_not_re_hashed_on_every_invoke(
    binding: Binding,
    registry: StubRegistry,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hashing every file of an environment is seconds, and it is per invocation.

    The full hash stays the authority; what changes is how often it is asked. An
    environment whose (entry count, newest mtime) reading is the one that was
    current when the hash last agreed is not re-hashed.
    """

    calls: list[Path] = []
    real = binding_module.tree_digest

    def counted(root: Path) -> str:
        calls.append(root)
        return real(root)

    monkeypatch.setattr(binding_module, "tree_digest", counted)

    for _ in range(3):
        outcome = invoke(
            binding,
            registry=registry,
            payload={"text": "hello", "mode": "echo"},
            budgets=BUDGETS,
            local_backend=local_backend,
            container_backend=container_backend,
        )
        assert outcome.status == "ok"

    assert len(calls) == 1, "the gate should have skipped the second and third hash"


def test_a_vanished_tree_reading_always_forces_the_full_hash(
    binding: Binding,
    registry: StubRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A partial stat walk is never a reusable reading of the environment."""

    assert binding.env_path is not None
    calls: list[Path] = []
    real = binding_module.tree_digest

    monkeypatch.setattr(binding_module, "_tree_fingerprint", lambda root: (-1, -1.0))
    monkeypatch.setattr(
        binding_module, "tree_digest", lambda root: (calls.append(root), real(root))[1]
    )

    binding.revalidate(registry)
    binding.revalidate(registry)

    assert calls == [binding.env_path, binding.env_path]
    assert str(binding.env_path) not in binding.tree_watch


def test_a_touched_environment_is_re_hashed_and_still_refuses(
    binding: Binding,
    registry: StubRegistry,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate misses on a changed tree, so the authority runs and refuses."""

    assert binding.env_path is not None
    invoke(
        binding,
        registry=registry,
        payload={"text": "hello", "mode": "echo"},
        budgets=BUDGETS,
        local_backend=local_backend,
        container_backend=container_backend,
    )

    calls: list[Path] = []
    real = binding_module.tree_digest
    monkeypatch.setattr(
        binding_module, "tree_digest", lambda root: (calls.append(root), real(root))[1]
    )

    (binding.env_path / "smuggled.py").write_text("raise SystemExit(0)\n", encoding="utf-8")

    with pytest.raises(RefusalError) as exc:
        invoke(
            binding,
            registry=registry,
            payload={"text": "hello", "mode": "echo"},
            budgets=BUDGETS,
            local_backend=local_backend,
            container_backend=container_backend,
        )
    assert exc.value.code is RefusalCode.CACHE_INTEGRITY
    assert len(calls) == 1, "a changed tree must reach the full hash"


def test_the_staleness_gate_is_defeated_by_a_preserved_fingerprint(
    binding: Binding,
    registry: StubRegistry,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> None:
    """The documented hole, asserted so that closing it cannot happen silently.

    An adversary who edits a file in place and restores its mtime leaves the
    (entry count, newest mtime) reading identical, and the gate skips the hash
    that would have caught them. This is written down rather than hoped about:
    it needs write access to a 0700 cache the operator owns, and a local provider
    environment is dependency isolation, NOT a security boundary -- somebody who
    can write there can already run code as the operator without touching the
    cache. If this test ever starts failing, the gate got stronger and
    Binding.revalidate's docstring is the thing to update.
    """

    assert binding.env_path is not None
    invoke(
        binding,
        registry=registry,
        payload={"text": "hello", "mode": "echo"},
        budgets=BUDGETS,
        local_backend=local_backend,
        container_backend=container_backend,
    )

    victim = next(binding.env_path.rglob("*.dist-info/METADATA"))
    before = victim.stat()
    victim.write_text("Name: substituted\nVersion: 0.0.0\n", encoding="utf-8")
    os.utime(victim, (before.st_atime, before.st_mtime))

    outcome = invoke(
        binding,
        registry=registry,
        payload={"text": "hello", "mode": "echo"},
        budgets=BUDGETS,
        local_backend=local_backend,
        container_backend=container_backend,
    )
    assert outcome.status == "ok"

    # And the authority itself is unfooled: it is only the gate that was.
    assert binding_module.tree_digest(binding.env_path) != binding.sealed_tree_digest


@pytest.mark.parametrize("binding", BACKENDS, indirect=True)
def test_without_the_lane_guard_a_socket_fails_for_a_different_reason(
    binding: Binding,
    registry: StubRegistry,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> None:
    """Keeps the egress lane's assertion from passing vacuously.

    Outside the conformance lane there is no injected guard, so ``connect`` mode
    fails on name resolution instead. If this test ever started reporting the
    lane's guard message, the lane would be proving nothing.
    """

    outcome = invoke(
        binding,
        registry=registry,
        payload={"text": "hello", "mode": "connect"},
        budgets=BUDGETS,
        local_backend=local_backend,
        container_backend=container_backend,
    )
    assert outcome.status == "error"
    assert outcome.envelope.error is not None
    assert "egress-conformance lane" not in outcome.envelope.error.message


@pytest.mark.parametrize("binding", BACKENDS, indirect=True)
def test_a_chatty_provider_does_not_corrupt_the_result_envelope(
    binding: Binding,
    registry: StubRegistry,
    local_backend: LocalEnvBackend,
    container_backend: ContainerBackend,
) -> None:
    """Standard output is reserved for the envelope, and reserved from the start.

    Real engines print. A record-linkage library announces its blocking time, a
    document converter reports its pipeline, a browser driver logs on the way
    down — none of it under this repository's control, and any one line of it
    would make the envelope unparseable and be reported as a protocol violation
    by a provider that did nothing wrong.

    The provider here prints three ways, including straight at file descriptor 1
    the way a C extension does, and the envelope still arrives intact. The noise
    is not swallowed either: it lands in stderr, where trace material belongs and
    where the executor's output-size budget still measures it.
    """

    outcome = invoke(
        binding,
        registry=registry,
        payload={"text": "hello", "mode": "chatty"},
        budgets=BUDGETS,
        local_backend=local_backend,
        container_backend=container_backend,
    )
    assert outcome.status == "ok"
    assert outcome.envelope.output == {
        "echo": "chatty",
        "input_bucket": "payload_size=tiny;charset=ascii",
    }
    assert "Blocking time" in outcome.stderr
    assert "native library says hello" in outcome.stderr
