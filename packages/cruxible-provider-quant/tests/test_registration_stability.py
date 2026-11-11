"""Drift guards on the registration, the manifest, and the packaging metadata.

Everything in this file is checking that two things that must say the same thing
still do. The registration declares each operation and the entrypoint serving
it; the entrypoint declares the operations it serves; the packaging metadata
names the same objects; and each operation declares exactly the refusals its
implementation can return. Each of those drifts is silent, which is exactly why
each gets a test.
"""

from __future__ import annotations

import importlib
import re
import tomllib
from pathlib import Path

import cruxible_provider_quant
import pytest
from cruxible_provider_quant import OPERATIONS
from cruxible_provider_quant import refusals as refusals_module
from cruxible_provider_runtime.manifest import ENTRYPOINT_GROUP, ProviderManifest
from cruxible_provider_runtime.registration import load_registration

from .conftest import PACKAGE_DIR

BUNDLE = load_registration(cruxible_provider_quant.PACKAGE_ROOT)
IDS = sorted(OPERATIONS)

SHARED_REFUSALS = frozenset({"invalid_parameter", "non_finite_result"})
"""What every implementation can return through the shared helpers.

``numeric.canonical_call`` declines an input whose canonical decimals do not
decode (``invalid_parameter``) and an output it cannot encode
(``non_finite_result``); ``outputs.ok_if_finite`` declines a non-finite reading.
Every implementation runs under both.
"""


def test_the_registration_declares_exactly_these_operations() -> None:
    assert [item.name for item in BUNDLE.operations] == IDS
    assert BUNDLE.entry_points() == OPERATIONS


def test_the_manifest_version_tracks_the_package_version(manifest: ProviderManifest) -> None:
    assert manifest.distribution.version == cruxible_provider_quant.__version__


def test_the_entry_points_are_the_registered_operations() -> None:
    """The registration and the packaging metadata must name the same objects."""

    document = tomllib.loads((PACKAGE_DIR / "pyproject.toml").read_text(encoding="utf-8"))
    declared = document["project"]["entry-points"][ENTRYPOINT_GROUP]
    assert declared == BUNDLE.entry_points()


@pytest.mark.parametrize("operation", IDS)
def test_each_entrypoint_declares_the_operation_it_serves(
    operation: str, manifest: ProviderManifest
) -> None:
    """The check the child harness performs, performed here so it fails early."""

    path = BUNDLE.operation(operation).entrypoint
    assert manifest.implementation(path).entrypoint == path
    module_name, _, object_name = path.partition(":")
    target = getattr(importlib.import_module(module_name), object_name)()
    assert callable(target)
    assert target.operations == (operation,)


@pytest.mark.parametrize("operation", IDS)
def test_each_operation_is_pure(operation: str) -> None:
    assert BUNDLE.operation(operation).effect == "pure"


@pytest.mark.parametrize("operation", IDS)
def test_each_operation_declares_exactly_the_refusals_its_implementation_returns(
    operation: str,
) -> None:
    """Read out of the implementation's own source, so a new raise site cannot hide.

    The executor refuses a code an operation does not declare, and a declared
    code nothing returns is a declaration that has drifted from the code. The
    plane's decline constants are module-level names in ``refusals``, so a
    ``decline(NAME, ...)`` call names its code.
    """

    module_name = BUNDLE.operation(operation).entrypoint.partition(":")[0]
    source = Path(importlib.import_module(module_name).__file__ or "").read_text(encoding="utf-8")
    named = set(re.findall(r"decline\(\s*([A-Z_]+)", source))
    named |= set(re.findall(r"ProviderResult\.refused\(\s*([A-Z_]+)", source))
    returned = {getattr(refusals_module, name) for name in named} | SHARED_REFUSALS
    declared = BUNDLE.operation(operation).refusals
    assert set(declared) == returned
    assert set(declared) <= set(refusals_module.QUANT_DECLINES) | set(
        refusals_module.INTEGRITY_REFUSALS
    )


ABSORBABLE = frozenset(refusals_module.QUANT_DECLINES) - {"non_finite_result"}
"""Every decline here means "this input cannot be answered", except one.

``non_finite_result`` is a well-formed input the declared method has no answer
for: the computation, not the input, is what failed, so a Line has nothing to
omit.
"""


@pytest.mark.parametrize("operation", IDS)
def test_input_declines_are_absorbable_and_a_failed_computation_is_not(operation: str) -> None:
    for code, refusal in BUNDLE.operation(operation).refusals.items():
        assert refusal.absorbable is (code in ABSORBABLE), code
        assert refusal.contract.spelling == f"{operation}.refusal.{code}@1"


@pytest.mark.parametrize("operation", IDS)
def test_each_operation_names_its_contracts(operation: str) -> None:
    declared = BUNDLE.operation(operation)
    assert declared.in_.spelling == f"{operation}.request@1"
    assert declared.out.spelling == f"{operation}.result@1"


def test_the_container_directory_carries_the_provenance_contract() -> None:
    assert (cruxible_provider_quant.CONTAINER_DIR / "Dockerfile").is_file()
    assert (cruxible_provider_quant.CONTAINER_DIR / "provenance.md").is_file()


def test_the_package_declares_no_accelerator_dependency() -> None:
    """The hard ban, checked against the lock rather than against the intention."""

    lock = (PACKAGE_DIR / "uv.lock").read_text(encoding="utf-8")
    for banned in ("torch", "nvidia-", "cuda", "cupy", "tensorflow", "jaxlib"):
        assert banned not in lock, f"the resolved set carries {banned!r}"
