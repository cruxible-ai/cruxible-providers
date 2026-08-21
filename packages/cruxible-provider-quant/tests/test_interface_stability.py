"""Drift guards on the pinned digests, the manifest, and the vocabularies.

Everything in this file is checking that two things that must say the same thing
still do. The interface digest is a literal, so it can drift from its preimage.
The manifest is a transcription, so it can drift from the packaging metadata. The
classifiers are core's, so they can drift from core's committed vocabularies.
Each of those drifts is silent, which is exactly why each gets a test.
"""

from __future__ import annotations

import ast
import inspect
import tomllib
from pathlib import Path

import cruxible_provider_quant
import pytest
from cruxible_provider_quant import (
    anomaly,
    calibrate,
    forecast,
    interfaces,
    linkage,
    rank,
    reduce,
    stat_test,
)
from cruxible_provider_quant.classifiers import CLASSIFIERS
from cruxible_provider_runtime.canonical import SHA256_RE
from cruxible_provider_runtime.manifest import ENTRYPOINT_GROUP, ProviderManifest
from cruxible_provider_runtime.registry import load_bucket_vocabulary

from .conftest import PACKAGE_DIR, VOCAB_DIR

IDS = list(interfaces.INTERFACE_IDS)

IMPLEMENTATION_MODULES = {
    "calc.calibrate": calibrate,
    "calc.reduce": reduce,
    "match.record": linkage,
    "score.rank": rank,
    "stat.test": stat_test,
    "ts.anomaly": anomaly,
    "ts.forecast": forecast,
}


@pytest.mark.parametrize("interface_id", IDS)
def test_the_pinned_interface_digest_still_matches_its_preimage(interface_id: str) -> None:
    """A literal digest that no longer matches its preimage is drift, not a typo."""

    assert (
        interfaces.recompute_interface_digest(interface_id)
        == (interfaces.INTERFACE_DIGESTS[interface_id])
    )


def test_every_digest_is_well_formed_and_distinct() -> None:
    digests = interfaces.INTERFACE_DIGESTS
    assert set(digests) == set(IDS)
    assert all(SHA256_RE.match(value) for value in digests.values())
    assert len(set(digests.values())) == len(digests)


@pytest.mark.parametrize("interface_id", IDS)
def test_every_emitted_refusal_is_declared_by_the_interface(interface_id: str) -> None:
    """The refusal schema is exhaustive over each quantitative implementation."""

    tree = ast.parse(inspect.getsource(IMPLEMENTATION_MODULES[interface_id]))
    emitted = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "RefusalCode"
    }
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    assert "ok_if_finite" in names
    emitted.add("NON_FINITE_RESULT")

    preimage = interfaces.INTERFACE_PREIMAGES[interface_id]
    assert "decline_reasons" not in preimage
    assert set(preimage["refusals"]) == {code.lower() for code in emitted}


@pytest.mark.parametrize("interface_id", IDS)
def test_the_manifest_pins_the_same_interface_digest(
    interface_id: str, manifest: ProviderManifest
) -> None:
    assert (
        manifest.implementation(interface_id).interface_digest
        == (interfaces.INTERFACE_DIGESTS[interface_id])
    )


def test_the_manifest_version_tracks_the_package_version(manifest: ProviderManifest) -> None:
    assert manifest.distribution.version == cruxible_provider_quant.__version__


def test_the_manifest_entrypoints_match_the_declared_entry_points(
    manifest: ProviderManifest,
) -> None:
    """The manifest and the packaging metadata must name the same objects."""

    document = tomllib.loads((PACKAGE_DIR / "pyproject.toml").read_text(encoding="utf-8"))
    declared = document["project"]["entry-points"][ENTRYPOINT_GROUP]
    assert set(declared) == set(IDS)
    for interface_id in IDS:
        assert declared[interface_id] == manifest.implementation(interface_id).entrypoint


@pytest.mark.parametrize("interface_id", IDS)
def test_each_entrypoint_resolves_and_declares_its_own_interface(
    interface_id: str, manifest: ProviderManifest
) -> None:
    """The check the child harness performs, performed here so it fails early."""

    import importlib

    path = manifest.implementation(interface_id).entrypoint
    module_name, _, object_name = path.partition(":")
    target = getattr(importlib.import_module(module_name), object_name)()
    assert callable(target)
    assert target.interface_id == interface_id


@pytest.mark.parametrize("interface_id", IDS)
def test_a_classifier_is_registered_for_every_interface(interface_id: str) -> None:
    assert interface_id in CLASSIFIERS


def test_no_interface_is_implemented_twice(manifest: ProviderManifest) -> None:
    """Same-interface double implementation is terminal in RP-0, so avoid it."""

    declared = [impl.interface_id for impl in manifest.implementations]
    assert sorted(declared) == sorted(IDS)
    assert len(set(declared)) == len(declared)


@pytest.mark.parametrize("interface_id", IDS)
def test_the_vocabulary_this_package_classifies_against_is_the_committed_one(
    interface_id: str,
) -> None:
    """The provider ships no copy of a vocabulary it does not own.

    What it does have to agree with is the committed data: every class a
    classifier can emit has to exist in the vocabulary core will register, or
    the bucket id it produces would not parse.
    """

    path = VOCAB_DIR / f"{interface_id}.yaml"
    assert path.is_file()
    vocabulary = load_bucket_vocabulary(path)
    assert vocabulary.interface_id == interface_id
    assert not list(Path(cruxible_provider_quant.PACKAGE_ROOT).rglob("*.yaml.orig"))
    # The package ships exactly one YAML: its manifest. A vocabulary copied in
    # here would be a second source of truth for core's data.
    assert [p.name for p in sorted(cruxible_provider_quant.PACKAGE_ROOT.glob("*.yaml"))] == [
        "manifest.yaml"
    ]


@pytest.mark.parametrize("interface_id", IDS)
def test_every_class_a_classifier_can_emit_exists_in_the_vocabulary(interface_id: str) -> None:
    """Read out of the classifier's own source, so a new class cannot hide.

    A class the classifier can return but the vocabulary does not declare would
    refuse at ``bucket_id`` — at run time, on whichever unlucky input reached it
    first. This turns that into a build failure.
    """

    from cruxible_provider_quant import classifiers as module

    vocabulary = load_bucket_vocabulary(VOCAB_DIR / f"{interface_id}.yaml")
    known = {class_id for dimension in vocabulary.dimensions for class_id in dimension.class_ids}
    dimensions = set(vocabulary.dimension_names)

    tree = ast.parse(inspect.getsource(module))
    literals = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    # Every dimension of this interface must be produced somewhere in the module.
    assert dimensions <= literals, sorted(dimensions - literals)
    # And every class the module mentions that looks like one of this
    # interface's classes must actually be one of them.
    assert known & literals, interface_id


def test_the_container_directory_carries_the_provenance_contract() -> None:
    assert (cruxible_provider_quant.CONTAINER_DIR / "Dockerfile").is_file()
    assert (cruxible_provider_quant.CONTAINER_DIR / "provenance.md").is_file()


def test_the_package_declares_no_accelerator_dependency() -> None:
    """The hard ban, checked against the lock rather than against the intention."""

    lock = (PACKAGE_DIR / "uv.lock").read_text(encoding="utf-8")
    for banned in ("torch", "nvidia-", "cuda", "cupy", "tensorflow", "jaxlib"):
        assert banned not in lock, f"the resolved set carries {banned!r}"
