"""Structural purity: the adapter's modules import nothing that reaches the world.

The runtime half of this claim lives in ``test_workspace_adapter.py`` (an
``open`` that raises for the duration of an invocation) and in the egress lane
(outbound sockets blocked in both processes). This is the static half: the
modules that run inside the child may import only from an allowlist, may not
name ``open``, and may not reach a module that touches the filesystem, the
network, the clock, or a subprocess.
"""

from __future__ import annotations

import ast
import inspect
from types import ModuleType

import pytest
from cruxible_provider_workspace import file as file_module
from cruxible_provider_workspace import interface as interface_module

ADAPTER_MODULES = [file_module, interface_module]

ALLOWED_IMPORTS = {
    "__future__",
    "base64",
    "hashlib",
    "collections.abc",
    "typing",
    "cruxible_provider_runtime.buckets",
    "cruxible_provider_runtime.canonical",
    "cruxible_provider_runtime.errors",
    "cruxible_provider_runtime.provider_api",
    "cruxible_provider_runtime.registry",
    "cruxible_provider_workspace.interface",
}

FORBIDDEN_NAMES = {
    "open",
    "exec",
    "eval",
    "__import__",
    "compile",
    "input",
    "breakpoint",
}


def _imports(module: ModuleType) -> set[str]:
    tree = ast.parse(inspect.getsource(module))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.module is not None
            if node.level:
                names.add(f"cruxible_provider_workspace.{node.module}")
            else:
                names.add(node.module)
    return names


@pytest.mark.parametrize("module", ADAPTER_MODULES, ids=lambda module: module.__name__)
def test_every_import_is_on_the_allowlist(module: ModuleType) -> None:
    assert _imports(module) <= ALLOWED_IMPORTS, _imports(module) - ALLOWED_IMPORTS


@pytest.mark.parametrize("module", ADAPTER_MODULES, ids=lambda module: module.__name__)
def test_no_import_is_deferred_into_a_function(module: ModuleType) -> None:
    """A function-local import is an import the allowlist above would not see."""

    tree = ast.parse(inspect.getsource(module))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            for inner in ast.walk(node):
                assert not isinstance(inner, ast.Import | ast.ImportFrom), (
                    f"{module.__name__}.{node.name} imports inside its body"
                )


@pytest.mark.parametrize("module", ADAPTER_MODULES, ids=lambda module: module.__name__)
def test_no_forbidden_builtin_is_named(module: ModuleType) -> None:
    tree = ast.parse(inspect.getsource(module))
    named = {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES
    }
    assert not named, named


def test_the_allowlist_itself_names_nothing_that_reaches_the_world() -> None:
    """The allowlist is the claim; this keeps a later edit to it honest."""

    world = {
        "os",
        "io",
        "pathlib",
        "socket",
        "subprocess",
        "time",
        "datetime",
        "shutil",
        "tempfile",
    }
    for name in ALLOWED_IMPORTS:
        assert name.split(".")[0] not in world, name
        assert name not in world, name


def test_the_entrypoint_object_carries_no_state_between_calls() -> None:
    """A fresh instance per child is the harness's doing; the class keeps nothing either."""

    provider = file_module.WorkspaceFile()
    assert not vars(provider)
