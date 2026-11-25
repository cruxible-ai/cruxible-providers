"""``@derive``: a plain Python function as a pure provider operation.

A derive computes a document from its input, and nothing else: it declares
``effect: pure``, contacts no endpoint, reads no secret and no clock, and gives
byte-identical output for identical input. That is the whole of what a
single-file provider needs, so the decorator asks only for what the operation
declares in ``registration.json``::

    from cruxible_provider_runtime import CaptureView, derive

    @derive(
        "triage.summarize",
        serves="triage.summarize",
        input_contract="triage.input@1",
        output_contract="triage.output@1",
    )
    def summarize(source: CaptureView, max_items: int = 5) -> dict:
        \"\"\"Summarizes the advisory's top findings.\"\"\"
        findings = source.json()["findings"][:max_items]
        return {"summary": source.derive({"findings": findings})}

The decorated name is a :class:`DeriveOperation`: the entrypoint object the
child harness resolves from ``module:summarize`` with no special case. It
declares ``operations = ("triage.summarize",)`` and is called with the run's
:class:`~.provider_api.ProviderRunContext`, like every provider.

**The calling convention.** The function's parameters are the input's fields,
by name: each named parameter receives the field of the same name, a parameter
with a default may be absent from the input, and a ``**fields`` parameter
receives every field no other parameter names (fields nothing names are
otherwise not passed). There is no positional or ``*args`` form, and no run
context: a pure operation depends on its input alone, never on the run id.

**Capture inputs.** A field is read as a Capture when its parameter is
annotated with one of ``CaptureView``, ``list[CaptureView]``,
``Sequence[CaptureView]`` or ``tuple[CaptureView, ...]`` (each optionally
``| None``), or when its name is in ``capture_inputs``. Each such field's wire
is checked and handed over as a :class:`~.capture_view.CaptureView` (a list of
them for a list). Any other annotation mentioning ``CaptureView`` --
``dict[str, CaptureView]``, ``CaptureView | str`` -- is refused when the
operation is defined, rather than guessed at. ``capture_inputs`` is for a
function without annotations, or a field reached through ``**fields``; with no
annotation saying which, it reads a mapping as one Capture and a list as one per
member.

**The return value.** A mapping is the operation's output (a ``CaptureView``
inside it is written back as its wire; :func:`~.capture_view.derived_capture`
and :meth:`~.capture_view.CaptureView.derive` build the wire for a new
document). A :class:`~.provider_api.ProviderResult` is returned as it is, for
metrics or ``incomplete_captures``. ``raise decline(code, message)`` refuses
under a code the operation declares in ``refusals``. Anything else, and an input
that does not fit the signature, raises: the harness reports it as the
provider error it is.

**Testing in plain Python.** ``summarize.fn`` is the undecorated function
(``summarize.fn(source=CaptureView(wire))``); ``summarize.run(payload)`` runs
the operation over an input mapping exactly as the harness does and returns its
``ProviderResult``.
"""

from __future__ import annotations

import collections.abc
import functools
import inspect
import types
import typing
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal

from pydantic import ValidationError

from .acquisition import CaptureInputError
from .capture_view import CaptureView
from .errors import Declined
from .operations import (
    OperationEffect,
    OperationSource,
    ProviderOperation,
)
from .provider_api import ProviderResult, ProviderRunContext

__all__ = [
    "DeriveDefinitionError",
    "DeriveInputError",
    "DeriveOperation",
    "DeriveOutputError",
    "derive",
]

CaptureShape = Literal["one", "list", "tuple", "value"]
"""How a capture field arrives: one view, a list or tuple of views, or by its value's shape."""

_PLACEHOLDER_ENTRYPOINT = "derive:placeholder"
_PLACEHOLDER_SOURCE = OperationSource(path="derive.py")
_FIELD_LABELS = {"in": "input_contract", "out": "output_contract"}


class DeriveDefinitionError(ValueError):
    """A ``@derive`` declaration that cannot become an operation. Raised when it is defined."""


class DeriveInputError(ValueError):
    """An input that does not fit the derive's signature: a field missing, or not a Capture."""


class DeriveOutputError(TypeError):
    """A derive returned something other than a mapping or a ``ProviderResult``."""


@dataclass(frozen=True)
class _Parameter:
    name: str
    required: bool
    capture: CaptureShape | None
    nullable: bool


@dataclass(frozen=True)
class _Plan:
    parameters: tuple[_Parameter, ...]
    fields: str | None
    """The ``**fields`` parameter's name, if the function has one."""


def _mentions_capture_view(annotation: object) -> bool:
    if annotation is CaptureView:
        return True
    return any(_mentions_capture_view(item) for item in typing.get_args(annotation))


def _capture_shape(annotation: object, where: str) -> tuple[CaptureShape | None, bool]:
    """``(shape, nullable)`` for an annotation; ``(None, False)`` when it is not a Capture."""

    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if origin is typing.Annotated:
        return _capture_shape(args[0], where)
    if annotation is CaptureView:
        return "one", False
    if origin is typing.Union or origin is types.UnionType:
        present = tuple(item for item in args if item is not type(None))
        if len(present) == 1 and len(present) != len(args):
            shape, _nullable = _capture_shape(present[0], where)
            if shape is not None:
                return shape, True
    elif origin in (list, collections.abc.Sequence) and args == (CaptureView,):
        return "list", False
    elif origin is tuple and args == (CaptureView, Ellipsis):
        return "tuple", False
    if _mentions_capture_view(annotation):
        raise DeriveDefinitionError(
            f"{where} is annotated {annotation!r}; a Capture parameter is CaptureView, "
            "list[CaptureView], Sequence[CaptureView] or tuple[CaptureView, ...], each "
            "optionally | None"
        )
    return None, False


def _plan(
    fn: Callable[..., Any], capture_inputs: tuple[str, ...], hints: Mapping[str, Any]
) -> _Plan:
    label = f"@derive {fn.__qualname__}"
    parameters: list[_Parameter] = []
    fields: str | None = None
    for parameter in inspect.signature(fn).parameters.values():
        if parameter.kind is inspect.Parameter.VAR_KEYWORD:
            fields = parameter.name
            continue
        if parameter.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.VAR_POSITIONAL,
        ):
            raise DeriveDefinitionError(
                f"{label}: parameter {parameter.name!r} is positional-only or *args; a derive's "
                "parameters are its input fields, by name"
            )
        shape, nullable = _capture_shape(
            hints.get(parameter.name), f"{label}: parameter {parameter.name!r}"
        )
        if shape is None and parameter.name in capture_inputs:
            shape = "value"
        parameters.append(
            _Parameter(
                name=parameter.name,
                required=parameter.default is inspect.Parameter.empty,
                capture=shape,
                nullable=nullable,
            )
        )
    named = {item.name for item in parameters}
    unknown = sorted(set(capture_inputs) - named)
    if unknown and fields is None:
        raise DeriveDefinitionError(
            f"{label}: capture_inputs names {unknown}, which are not parameters (and it has "
            "no **fields parameter to receive them)"
        )
    return _Plan(parameters=tuple(parameters), fields=fields)


def _view(where: str, value: object) -> CaptureView:
    if isinstance(value, CaptureView):
        return value
    if not isinstance(value, Mapping):
        raise DeriveInputError(f"input field {where!r} is not a Capture: {type(value).__name__}")
    try:
        return CaptureView(value)
    except CaptureInputError as exc:
        raise CaptureInputError(f"input field {where!r}: {exc}") from exc


def _hydrate(name: str, shape: CaptureShape, nullable: bool, value: object) -> object:
    if value is None:
        if nullable or shape == "value":
            return None
        raise DeriveInputError(f"input field {name!r} is null, but its parameter takes a Capture")
    if shape == "one":
        return _view(name, value)
    if shape in ("list", "tuple"):
        if not isinstance(value, list | tuple):
            raise DeriveInputError(f"input field {name!r} is not a list of Captures")
        views = [_view(f"{name}[{index}]", item) for index, item in enumerate(value)]
        return views if shape == "list" else tuple(views)
    if isinstance(value, list | tuple):
        return [_view(f"{name}[{index}]", item) for index, item in enumerate(value)]
    return _view(name, value)


def _output(value: object) -> object:
    """``value`` with every ``CaptureView`` inside it written back as its wire."""

    if isinstance(value, CaptureView):
        return value.wire()
    if isinstance(value, Mapping):
        return {key: _output(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_output(item) for item in value]
    return value


def _description(fn: Callable[..., Any], description: str | None) -> str:
    if description is None:
        doc = inspect.getdoc(fn) or ""
        description = " ".join(doc.split("\n\n", 1)[0].split())
    description = unicodedata.normalize("NFC", description).strip()
    if not description:
        raise DeriveDefinitionError(
            f"@derive {fn.__qualname__}: give the operation a description, or a docstring whose "
            "first paragraph says what it does"
        )
    return description


def _serves(serves: str | Iterable[str]) -> tuple[str, ...]:
    names = (serves,) if isinstance(serves, str) else tuple(serves)
    return tuple(sorted(set(names), key=lambda name: name.encode("utf-8")))


def _validation_message(exc: ValidationError) -> str:
    problems = []
    for error in exc.errors():
        where = ".".join(_FIELD_LABELS.get(str(part), str(part)) for part in error["loc"])
        problems.append(f"{where}: {error['msg']}" if where else str(error["msg"]))
    return "; ".join(problems)


class DeriveOperation:
    """A pure provider operation made from one function by :func:`derive`.

    It is the entrypoint object: ``operations`` names the one operation it
    serves, and calling it with a run context runs that operation. It carries
    the declaration a registration needs (:meth:`operation`), always with
    ``effect: pure`` and never an endpoint, a secret or an extra.
    """

    effect: Final[OperationEffect] = "pure"
    declared_endpoints: Final[tuple[str, ...]] = ()

    def __init__(
        self,
        fn: Callable[..., Any],
        *,
        name: str,
        serves: str | Iterable[str],
        input_contract: str,
        output_contract: str,
        description: str | None = None,
        may_truncate: bool = False,
        refusals: Mapping[str, str | Mapping[str, Any]] | None = None,
        capture_inputs: str | Iterable[str] = (),
    ) -> None:
        if isinstance(fn, DeriveOperation):
            raise DeriveDefinitionError(f"{fn.name!r} is already a derive operation")
        if not inspect.isfunction(fn):
            raise DeriveDefinitionError(f"@derive decorates a function, not {type(fn).__name__}")
        if inspect.iscoroutinefunction(fn) or inspect.isgeneratorfunction(fn):
            raise DeriveDefinitionError(
                f"@derive {fn.__qualname__}: a derive is a plain function that returns its "
                "output, not a coroutine or a generator"
            )
        captured = (capture_inputs,) if isinstance(capture_inputs, str) else tuple(capture_inputs)
        for field_name in captured:
            if not isinstance(field_name, str) or not field_name.isidentifier():
                raise DeriveDefinitionError(f"capture_inputs names {field_name!r}, not a field")
        described = _description(fn, description)
        try:
            declared = ProviderOperation.model_validate(
                {
                    "name": name,
                    "description": described,
                    "source": _PLACEHOLDER_SOURCE,
                    "serves": _serves(serves),
                    "in": input_contract,
                    "out": output_contract,
                    "effect": "pure",
                    "may_truncate": may_truncate,
                    "refusals": {
                        code: {"contract": value} if isinstance(value, str) else dict(value)
                        for code, value in (refusals or {}).items()
                    },
                    "entrypoint": _PLACEHOLDER_ENTRYPOINT,
                }
            )
        except ValidationError as exc:
            raise DeriveDefinitionError(
                f"@derive {name!r} ({fn.__qualname__}): {_validation_message(exc)}"
            ) from None
        except ValueError as exc:
            raise DeriveDefinitionError(f"@derive {name!r} ({fn.__qualname__}): {exc}") from None

        functools.update_wrapper(self, fn)
        self.fn: Callable[..., Any] = fn
        self.operations: tuple[str, ...] = (declared.name,)
        self.capture_inputs: tuple[str, ...] = captured
        self._declared = declared
        self._resolved: _Plan | None = None
        # Resolve the signature now when the annotations already resolve, so a
        # malformed one is refused where it is written; an annotation naming
        # something defined later in the module waits for the first use.
        try:
            hints = typing.get_type_hints(fn, include_extras=True)
        except Exception:
            _plan(fn, captured, {})  # the shape checks that need no annotation
        else:
            self._resolved = _plan(fn, captured, hints)

    # -- the declaration -----------------------------------------------------------------

    @property
    def name(self) -> str:
        return self._declared.name

    @property
    def description(self) -> str:
        return self._declared.description

    @property
    def serves(self) -> tuple[str, ...]:
        return self._declared.serves

    @property
    def input_contract(self) -> str:
        return self._declared.in_.spelling

    @property
    def output_contract(self) -> str:
        return self._declared.out.spelling

    @property
    def may_truncate(self) -> bool:
        return self._declared.may_truncate

    @property
    def refusals(self) -> Mapping[str, Mapping[str, Any]]:
        """The declared refusals as a registration spells them: ``{code: {contract, ...}}``."""

        return {
            code: {"contract": item.contract.spelling, "absorbable": item.absorbable}
            for code, item in self._declared.refusals.items()
        }

    def operation(self, *, entrypoint: str, source: OperationSource | str) -> ProviderOperation:
        """The registration's operation for this derive, served at ``entrypoint``.

        ``source`` is where its code is (an :class:`~.operations.OperationSource`,
        or a relative path).
        """

        where = OperationSource(path=source) if isinstance(source, str) else source
        try:
            return ProviderOperation.model_validate(
                {**self._declared.wire(), "entrypoint": entrypoint, "source": where}
            )
        except ValidationError as exc:
            raise DeriveDefinitionError(
                f"@derive {self.name!r}: {_validation_message(exc)}"
            ) from None

    def capture_fields(self) -> dict[str, CaptureShape]:
        """The input fields read as Captures, by name, with how each arrives."""

        found: dict[str, CaptureShape] = {
            item.name: item.capture for item in self._plan().parameters if item.capture
        }
        for field_name in self.capture_inputs:
            found.setdefault(field_name, "value")
        return found

    def check(self) -> None:
        """Resolve the signature now; :class:`DeriveDefinitionError` if it cannot be read."""

        self._plan()

    def _plan(self) -> _Plan:
        if self._resolved is None:
            try:
                hints = typing.get_type_hints(self.fn, include_extras=True)
            except Exception as exc:
                raise DeriveDefinitionError(
                    f"@derive {self.name!r} ({self.fn.__qualname__}): its annotations do not "
                    f"resolve: {exc}"
                ) from exc
            self._resolved = _plan(self.fn, self.capture_inputs, hints)
        return self._resolved

    # -- running -------------------------------------------------------------------------

    def _arguments(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        plan = self._plan()
        arguments: dict[str, Any] = {}
        for parameter in plan.parameters:
            if parameter.name not in payload:
                if parameter.required:
                    raise DeriveInputError(
                        f"operation {self.name!r} needs input field {parameter.name!r}, and the "
                        "input has none"
                    )
                continue
            value = payload[parameter.name]
            if parameter.capture is not None:
                value = _hydrate(parameter.name, parameter.capture, parameter.nullable, value)
            arguments[parameter.name] = value
        if plan.fields is not None:
            named = {item.name for item in plan.parameters}
            for key, value in payload.items():
                if key in named:
                    continue
                arguments[key] = (
                    _hydrate(key, "value", False, value) if key in self.capture_inputs else value
                )
        return arguments

    def run(self, payload: Mapping[str, Any]) -> ProviderResult:
        """Run the operation over ``payload``, as the harness does; return its result.

        A ``decline`` the function raises comes back as a refused result. An
        input that does not fit the signature raises :class:`DeriveInputError`
        (or :class:`~.acquisition.CaptureInputError` for a malformed Capture),
        and a return value that is not a mapping or a ``ProviderResult`` raises
        :class:`DeriveOutputError`.
        """

        if not isinstance(payload, Mapping):
            raise DeriveInputError(f"operation {self.name!r} takes a mapping of input fields")
        arguments = self._arguments(payload)
        try:
            returned = self.fn(**arguments)
        except Declined as exc:
            return ProviderResult(status="refused", refusal=exc.refusal)
        if isinstance(returned, ProviderResult):
            return returned
        if not isinstance(returned, Mapping):
            raise DeriveOutputError(
                f"operation {self.name!r} returned {type(returned).__name__}; a derive returns "
                "a mapping of output fields (or a ProviderResult)"
            )
        output = _output(returned)
        assert isinstance(output, dict)
        return ProviderResult.ok(output)

    def __call__(self, context: ProviderRunContext) -> ProviderResult:
        return self.run(context.input)

    def __repr__(self) -> str:
        return f"<derive {self.name!r} {self.fn.__module__}.{self.fn.__qualname__}>"


def derive(
    name: str,
    *,
    serves: str | Iterable[str],
    input_contract: str,
    output_contract: str,
    description: str | None = None,
    may_truncate: bool = False,
    refusals: Mapping[str, str | Mapping[str, Any]] | None = None,
    capture_inputs: str | Iterable[str] = (),
) -> Callable[[Callable[..., Any]], DeriveOperation]:
    """Declare a function as the pure operation ``name``.

    ``serves`` is the capability name (or names) a Procedure slot binds it by;
    ``input_contract`` and ``output_contract`` pin its Contracts as ``name@N``.
    ``description`` defaults to the first paragraph of the function's docstring.
    ``may_truncate`` lets it bind a slot that accepts a truncated listing.
    ``refusals`` maps each refusal code it may ``decline`` with to its detail
    Contract (``name@N``, or ``{"contract": ..., "absorbable": ...}``).
    ``capture_inputs`` names input fields to read as Captures beyond those the
    annotations already say are.

    Everything is checked when the function is decorated, against the same
    model a registration is read with; a problem raises
    :class:`DeriveDefinitionError` naming it.
    """

    def decorate(fn: Callable[..., Any]) -> DeriveOperation:
        return DeriveOperation(
            fn,
            name=name,
            serves=serves,
            input_contract=input_contract,
            output_contract=output_contract,
            description=description,
            may_truncate=may_truncate,
            refusals=refusals,
            capture_inputs=capture_inputs,
        )

    return decorate
