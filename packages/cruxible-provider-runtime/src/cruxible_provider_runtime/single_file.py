"""A provider from one Python file: discover its derives and build its package metadata.

A single-file provider is a module whose operations are all ``@derive``
functions (:mod:`.derive`). Packaging one needs two things this module owns:

* :func:`discover_derives` reads a loaded module and returns its operations,
  sorted by name, refusing a module with none, an operation defined twice, or
  one defined in another module (it would not be in the package);
* :func:`build_single_file_package` turns them into the package's
  ``registration.json`` (schema 3) and runtime manifest, each operation's
  entrypoint ``<module_name>:<attribute>`` and its own implementation row
  (``effect: pure``, no endpoint, deterministic). ``declares`` is exactly the
  closure the operations reach out of the Contracts and CaptureKinds given. The
  result is read back with :func:`.registration.load_registration` -- the
  reader every install uses -- before it is returned, so a package it builds is
  one the runtime reads.

The files sit together in one directory of the distribution, beside the module
the entrypoints name (the registration names the manifest by relative path),
and the wheel's ``cruxible.providers`` entry points are
:attr:`SingleFilePackage.entry_points`. The module name is the packaged one,
which need not be the name the file was loaded under: :func:`load_single_file_module`
loads a file by path under whatever name it is given.

Core does not import this runtime, so a publisher in core drives it in an
interpreter that has it installed, through the command line::

    python -m cruxible_provider_runtime.single_file describe FILE [--module-name NAME]
    python -m cruxible_provider_runtime.single_file build FILE \\
        --distribution NAME --version VERSION --declares DECLARES.json --out DIR \\
        [--module-name NAME] [--source-path PATH] [--repository URL] [--revision SHA] \\
        [--backend local_env|container ...]

Each prints one JSON document on standard output and exits 0, or prints
``{"error": {"kind": ..., "message": ...}}`` and exits 1. Whatever the file
prints while it is imported goes to standard error. ``DECLARES.json`` is a
``declares`` block -- ``{"contracts": [...], "capture_kinds": [...],
"built_against": [...]}`` -- holding at least every shape the operations reach.
Loading the file runs it: a publisher imports only a file its author asked to
publish.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import keyword
import os
import sys
import tempfile
import traceback
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version
from pydantic import ValidationError

from .derive import DeriveDefinitionError, DeriveOperation
from .manifest import BackendKind, DistributionRef, ImplementationManifest, ProviderManifest
from .operations import OperationSource, ProviderOperation
from .protocol import PROTOCOL_VERSION
from .registration import (
    REGISTRATION_FILE,
    BuiltAgainstCapability,
    BuiltAgainstCaptureKind,
    CarriedCaptureKind,
    CarriedContract,
    PackageRegistration,
    ResourceRef,
    declared_closure,
    load_registration,
    render_registration,
)

__all__ = [
    "MANIFEST_FILE",
    "RUNTIME_DISTRIBUTION",
    "DiscoveredDerive",
    "SingleFileError",
    "SingleFilePackage",
    "build_single_file_package",
    "discover_derives",
    "load_single_file_module",
    "main",
    "run_cli",
]

MANIFEST_FILE = "manifest.json"
"""The runtime manifest's file name, beside ``registration.json``. JSON, which YAML readers read."""

RUNTIME_DISTRIBUTION = "cruxible-provider-runtime"


class SingleFileError(ValueError):
    """A module, or a packaging request, that cannot become a single-file provider."""


@dataclass(frozen=True)
class DiscoveredDerive:
    """One operation a module serves: the attribute it is bound to, and the derive."""

    attribute: str
    operation: DeriveOperation

    def entrypoint(self, module_name: str) -> str:
        return f"{module_name}:{self.attribute}"


def _check_module_name(name: str) -> str:
    if not name.isidentifier() or keyword.iskeyword(name):
        raise SingleFileError(
            f"module name {name!r} is not a Python identifier; a single file is packaged as one "
            "top-level module, imported by that name"
        )
    if name in sys.stdlib_module_names:
        raise SingleFileError(
            f"module name {name!r} is a standard-library module's; the packaged module would "
            "never be the one imported"
        )
    return name


def load_single_file_module(
    path: str | os.PathLike[str], *, module_name: str | None = None
) -> ModuleType:
    """Import the Python file at ``path`` as module ``module_name`` (default: its stem).

    The module is registered in ``sys.modules`` while its body runs (so classes
    and dataclasses it defines resolve their module) and removed afterwards, so
    loading a file leaves no import behind. A name another importable module
    already has is refused: in an environment with both, one would shadow the
    other. Importing runs the file.
    """

    file = Path(path)
    if file.suffix != ".py" or not file.is_file():
        raise SingleFileError(f"{file} is not a Python file")
    name = _check_module_name(module_name or file.stem)
    existing = importlib.util.find_spec(name) if name not in sys.modules else None
    if name in sys.modules or (
        existing is not None
        and (existing.origin is None or Path(existing.origin).resolve() != file.resolve())
    ):
        raise SingleFileError(
            f"module name {name!r} is already an importable module here; give the provider a "
            "module name nothing else uses"
        )
    spec = importlib.util.spec_from_file_location(name, file)
    if spec is None or spec.loader is None:
        raise SingleFileError(f"{file} cannot be loaded as a module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(name, None)
    return module


def discover_derives(module: ModuleType) -> tuple[DiscoveredDerive, ...]:
    """Every ``@derive`` operation ``module`` defines, sorted by operation name.

    Refused with :class:`SingleFileError`: a module with none; an operation
    bound to two attributes, or two derives with one name; and a derive defined
    in another module (imported into this one), which a package built from this
    file would not contain. Each signature is resolved here, so an annotation
    that does not resolve is reported before anything is built.
    """

    found: list[DiscoveredDerive] = []
    for attribute, value in vars(module).items():
        if not isinstance(value, DeriveOperation):
            continue
        if value.fn.__module__ != module.__name__:
            raise SingleFileError(
                f"{attribute!r} is the derive {value.name!r} defined in module "
                f"{value.fn.__module__!r}, not in this file; a single-file provider defines "
                "every operation it serves"
            )
        value.check()
        found.append(DiscoveredDerive(attribute=attribute, operation=value))
    if not found:
        raise SingleFileError(
            f"module {module.__name__!r} defines no @derive operation; decorate each function it "
            "serves with @derive(name, serves=..., input_contract=..., output_contract=...)"
        )
    attributes: dict[str, list[str]] = {}
    for item in found:
        attributes.setdefault(item.operation.name, []).append(item.attribute)
    repeated = {name: names for name, names in attributes.items() if len(names) > 1}
    if repeated:
        detail = "; ".join(
            f"{name!r} at {', '.join(sorted(names))}" for name, names in sorted(repeated.items())
        )
        raise SingleFileError(f"each operation is defined once; defined more than once: {detail}")
    return tuple(sorted(found, key=lambda item: item.operation.name.encode("utf-8")))


@dataclass(frozen=True)
class SingleFilePackage:
    """The metadata of a provider package built from one module, verified.

    ``files()`` are written into the directory that holds the module;
    ``entry_points`` are the wheel's ``cruxible.providers`` group.
    """

    module_name: str
    registration: PackageRegistration
    manifest: ProviderManifest
    registration_bytes: bytes
    manifest_bytes: bytes

    @property
    def distribution(self) -> str:
        return self.manifest.distribution.name

    @property
    def version(self) -> str:
        return self.manifest.distribution.version

    @property
    def operations(self) -> tuple[ProviderOperation, ...]:
        return self.registration.operations

    @property
    def entry_points(self) -> dict[str, str]:
        """``{operation: entrypoint}``: exactly the ``cruxible.providers`` entry points."""

        return {item.name: item.entrypoint for item in self.registration.operations}

    @property
    def runtime_requirement(self) -> str:
        """The dependency the distribution declares on this runtime (the one that built it)."""

        from . import __version__

        return f"{RUNTIME_DISTRIBUTION}>={__version__}"

    def files(self) -> dict[str, bytes]:
        """The package files by package-relative path: the registration and its manifest."""

        return {REGISTRATION_FILE: self.registration_bytes, MANIFEST_FILE: self.manifest_bytes}

    def write(self, directory: str | os.PathLike[str]) -> dict[str, Path]:
        """Write :meth:`files` into ``directory`` (created if missing); return their paths."""

        root = Path(directory)
        root.mkdir(parents=True, exist_ok=True)
        written: dict[str, Path] = {}
        for name, data in self.files().items():
            target = root / name
            target.write_bytes(data)
            written[name] = target
        return written

    def summary(self) -> dict[str, Any]:
        """What was built, as JSON: identity, entry points, file digests and the operations."""

        return {
            "distribution": self.distribution,
            "version": self.version,
            "module_name": self.module_name,
            "entry_points": self.entry_points,
            "runtime_requirement": self.runtime_requirement,
            "files": {
                name: "sha256:" + hashlib.sha256(data).hexdigest()
                for name, data in self.files().items()
            },
            "operations": json.loads(self.registration_bytes)["operations"],
        }


def _manifest_bytes(manifest: ProviderManifest) -> bytes:
    return (json.dumps(manifest.canonical_payload(), indent=2, ensure_ascii=False) + "\n").encode(
        "utf-8"
    )


def _source(
    module: ModuleType,
    module_name: str,
    source_path: str | None,
    repository: str | None,
    revision: str | None,
) -> OperationSource:
    if source_path is None:
        filename = getattr(module, "__file__", None)
        source_path = Path(filename).name if filename else f"{module_name}.py"
    try:
        return OperationSource(path=source_path, repository=repository, revision=revision)
    except ValidationError as exc:
        problems = "; ".join(str(error["msg"]) for error in exc.errors())
        raise SingleFileError(f"operation source: {problems}") from None


def build_single_file_package(
    module: ModuleType,
    *,
    distribution: str,
    version: str,
    contracts: Iterable[CarriedContract | Mapping[str, Any]],
    capture_kinds: Iterable[CarriedCaptureKind | Mapping[str, Any]] = (),
    built_against: Iterable[
        BuiltAgainstCapability | BuiltAgainstCaptureKind | Mapping[str, Any]
    ] = (),
    module_name: str | None = None,
    source_path: str | None = None,
    repository: str | None = None,
    revision: str | None = None,
    backends: Sequence[BackendKind] = ("local_env",),
) -> SingleFilePackage:
    """The schema-3 registration and runtime manifest for ``module``'s derives.

    ``distribution`` is the package's canonical distribution name (and its
    ``provider_id``); ``version`` its normalized version. ``module_name`` is the
    name the module is packaged and imported under (default: the name it was
    loaded with), and every entrypoint is ``<module_name>:<attribute>``.
    ``contracts``, ``capture_kinds`` and ``built_against`` are a pool of shapes
    as a ``declares`` block spells them; the registration carries exactly what
    the operations reach out of it, and a shape they reach that the pool lacks
    is refused. ``source_path`` (default: the file's name), ``repository`` and
    ``revision`` say where the operations' code is. ``backends`` are the backend
    kinds each implementation runs under: ``local_env`` alone unless the
    publisher also builds an image.

    Raises :class:`SingleFileError` (or :class:`~.derive.DeriveDefinitionError`
    for a derive whose signature does not resolve).
    """

    name = _check_module_name(module_name or module.__name__)
    if canonicalize_name(distribution) != distribution:
        raise SingleFileError(
            f"distribution {distribution!r} is not a canonical distribution name; use "
            f"{canonicalize_name(distribution)!r}"
        )
    try:
        normalized = str(Version(version))
    except InvalidVersion:
        raise SingleFileError(f"version {version!r} is not a valid version") from None
    if normalized != version:
        raise SingleFileError(f"version {version!r} is not normalized; use {normalized!r}")
    source = _source(module, name, source_path, repository, revision)
    discovered = discover_derives(module)
    operations = tuple(
        item.operation.operation(entrypoint=item.entrypoint(name), source=source)
        for item in discovered
    )
    try:
        manifest = ProviderManifest(
            provider_id=distribution,
            distribution=DistributionRef(name=distribution, version=version),
            supported_protocol_majors=(PROTOCOL_VERSION.major,),
            implementations=tuple(
                ImplementationManifest(
                    entrypoint=item.entrypoint,
                    backends=tuple(backends),
                    declared_endpoints=(),
                    requires_extras=(),
                    deterministic=True,
                )
                for item in operations
            ),
        )
    except ValidationError as exc:
        problems = "; ".join(str(error["msg"]) for error in exc.errors())
        raise SingleFileError(f"runtime manifest: {problems}") from None
    manifest_bytes = _manifest_bytes(manifest)
    try:
        declares = declared_closure(operations, contracts, capture_kinds, built_against)
    except (ValidationError, ValueError) as exc:
        raise SingleFileError(f"declares: {exc}") from None
    registration = PackageRegistration(
        schema_version=3,
        manifest=ResourceRef(
            path=MANIFEST_FILE, digest="sha256:" + hashlib.sha256(manifest_bytes).hexdigest()
        ),
        operations=operations,
        declares=declares,
    )
    package = SingleFilePackage(
        module_name=name,
        registration=registration,
        manifest=manifest,
        registration_bytes=render_registration(registration),
        manifest_bytes=manifest_bytes,
    )
    # Read back exactly as an install reads a package directory: the descriptor,
    # its manifest by path and digest, the closure, and their cross-links.
    with tempfile.TemporaryDirectory(prefix="cruxible-single-file-") as scratch:
        package.write(scratch)
        try:
            load_registration(Path(scratch))
        except (ValidationError, ValueError) as exc:  # pragma: no cover - a builder bug
            raise SingleFileError(f"the built package does not read back: {exc}") from exc
    return package


# -- the command line -------------------------------------------------------------------------


def _spelled(operation: ProviderOperation) -> dict[str, Any]:
    """The operation as ``registration.json`` spells it: unpinned Contracts as ``name@N``."""

    document = operation.wire()
    document["in"] = operation.in_.spelling
    document["out"] = operation.out.spelling
    for code, refusal in operation.refusals.items():
        document["refusals"][code]["contract"] = refusal.contract.spelling
    return document


def _describe(module: ModuleType, module_name: str, source_path: str | None) -> dict[str, Any]:
    source = _source(module, module_name, source_path, None, None)
    return {
        "module_name": module_name,
        "operations": [
            {
                **_spelled(
                    item.operation.operation(entrypoint=item.entrypoint(module_name), source=source)
                ),
                "attribute": item.attribute,
                "capture_fields": item.operation.capture_fields(),
            }
            for item in discover_derives(module)
        ],
    }


def _declares_pool(path: str) -> dict[str, Any]:
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SingleFileError(f"--declares {path}: not a readable JSON document ({exc})") from None
    if not isinstance(document, dict) or not isinstance(document.get("contracts"), list):
        raise SingleFileError(
            f"--declares {path}: a declares block, {{'contracts': [...], 'capture_kinds': [...], "
            "'built_against': [...]}}"
        )
    unknown = sorted(set(document) - {"contracts", "capture_kinds", "built_against"})
    if unknown:
        raise SingleFileError(f"--declares {path}: unknown keys {unknown}")
    return document


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m cruxible_provider_runtime.single_file",
        description="Describe or package a single-file provider of @derive operations.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    describe = commands.add_parser("describe", help="list the operations a file defines")
    build = commands.add_parser("build", help="write registration.json and manifest.json")
    for command in (describe, build):
        command.add_argument("file", help="the provider's Python file")
        command.add_argument("--module-name", help="the packaged module name (default: stem)")
        command.add_argument("--source-path", help="where the code is (default: file name)")
    build.add_argument("--distribution", required=True, help="canonical distribution name")
    build.add_argument("--version", required=True, help="normalized distribution version")
    build.add_argument("--declares", required=True, help="a JSON declares block (shape pool)")
    build.add_argument("--out", required=True, help="directory to write the files into")
    build.add_argument("--repository", help="https URL of the repository the code is in")
    build.add_argument("--revision", help="full commit id the build was made from")
    build.add_argument(
        "--backend",
        action="append",
        choices=("local_env", "container"),
        help="backend kind each implementation runs under (default: local_env)",
    )
    return parser


def _error(kind: str, message: str) -> tuple[int, dict[str, Any]]:
    return 1, {"error": {"kind": kind, "message": message}}


def run_cli(argv: Sequence[str]) -> tuple[int, dict[str, Any]]:
    """Run one command; return ``(exit status, JSON document)``. Writes nothing to stdout."""

    arguments = _parser().parse_args(list(argv))
    try:
        module_name = _check_module_name(arguments.module_name or Path(arguments.file).stem)
        try:
            module = load_single_file_module(arguments.file, module_name=module_name)
        except SingleFileError:
            raise
        except Exception as exc:
            traceback.print_exc(file=sys.stderr)
            return _error("import_failed", f"{type(exc).__name__}: {exc}")
        if arguments.command == "describe":
            return 0, _describe(module, module_name, arguments.source_path)
        pool = _declares_pool(arguments.declares)
        package = build_single_file_package(
            module,
            distribution=arguments.distribution,
            version=arguments.version,
            contracts=pool["contracts"],
            capture_kinds=pool.get("capture_kinds", ()),
            built_against=pool.get("built_against", ()),
            module_name=module_name,
            source_path=arguments.source_path,
            repository=arguments.repository,
            revision=arguments.revision,
            backends=tuple(arguments.backend or ("local_env",)),
        )
        written = package.write(arguments.out)
        return 0, {
            **package.summary(),
            "written": {name: str(path) for name, path in written.items()},
        }
    except DeriveDefinitionError as exc:
        return _error("invalid_derive", str(exc))
    except SingleFileError as exc:
        return _error("invalid_package", str(exc))


def main(argv: Sequence[str] | None = None) -> int:
    """The command line: the JSON document on stdout, everything else on stderr."""

    from .child import reserve_stdout

    # Taken before the file is imported, so that whatever it prints cannot
    # corrupt the one document a publisher parses.
    out = reserve_stdout()
    status, document = run_cli(sys.argv[1:] if argv is None else argv)
    with os.fdopen(out, "wb", closefd=True) as stream:
        stream.write(json.dumps(document, indent=2, sort_keys=True).encode("utf-8") + b"\n")
    return status


if __name__ == "__main__":  # pragma: no cover - process entry
    sys.exit(main())
