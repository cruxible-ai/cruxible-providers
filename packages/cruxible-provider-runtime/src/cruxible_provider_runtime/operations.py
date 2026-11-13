"""Typed provider operations: what a provider package serves (registration schema 3).

A provider package declares **operations** in its ``registration.json``. Each
says what it does (``description``), where its code is (``source``), which
capability names it serves (``serves``: names, no versions), its own input and
output Contracts (``name@N``), its effect, whether it may return a truncated
listing (``may_truncate``), the typed refusals it may return (each a code and a
detail Contract), and the entrypoint that serves it. This is the shape core
reads (``cruxible_client.contracts.provider_registration.DeclaredOperation``);
the runtime keeps its own mirror so that a package can be read and run without
core installed.

The Contracts an operation names are not looked up on an instance: the package
carries its own copies in the registration's ``declares`` block
(:mod:`cruxible_provider_runtime.registration`). A Line binds an exact
``{build, operation}`` whose signature digest, over the pinned ``in``, ``out``
and effect, travels in every run context. The runtime never interprets a
Contract's fields when it runs one.

A capability under ``workspace.`` is served only by core's builtin, so no
package operation may name one in ``serves``.

There is no shared refusal vocabulary. An operation's ``refusals`` *is* its
vocabulary; a code the runtime reserves for its own machinery
(:data:`cruxible_provider_runtime.errors.RUNTIME_REFUSAL_CODES`) may not be
declared by an operation, so the two can never be confused on the wire.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Callable, Iterable
from pathlib import PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .canonical import SHA256_RE, canonical_json
from .errors import check_operation_refusal_code
from .protocol import OPERATION_NAME_RE

__all__ = [
    "MAX_OPERATION_DESCRIPTION",
    "MAX_SERVES",
    "OPERATION_EFFECTS",
    "OPERATION_SIGNATURE_DOMAIN",
    "RESERVED_CAPABILITY_PREFIX",
    "ContractRef",
    "OperationEffect",
    "OperationRefusal",
    "OperationSource",
    "ProviderOperation",
    "callable_path",
    "operation_signature_digest",
    "pin_operation",
    "validate_capability_name",
    "validate_operations",
]

OperationEffect = Literal["pure", "external_read", "external_mutation"]
OPERATION_EFFECTS: tuple[OperationEffect, ...] = ("pure", "external_read", "external_mutation")

OPERATION_SIGNATURE_DOMAIN = "cruxible-operation-signature-v1"
"""Core's signature-digest domain. The digest is core's to define; this mirrors it."""

RESERVED_CAPABILITY_PREFIX = "workspace."
"""Capabilities under this prefix are served by core's builtin, never by a package."""

#: The most capability names one operation serves (core's ``MAX_SERVES``).
MAX_SERVES = 16
MAX_OPERATION_DESCRIPTION = 2048

_CONTRACT_NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_CAPABILITY_NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_REPOSITORY_RE = re.compile(r"^https://[^\s]{1,503}$")
_REVISION_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _utf8(value: str) -> bytes:
    return value.encode("utf-8")


def callable_path(value: str) -> str:
    """``module:callable`` with identifier parts only, or ``ValueError``."""

    module, sep, member = value.partition(":")
    if not sep or not member.isidentifier() or not all(p.isidentifier() for p in module.split(".")):
        raise ValueError("entrypoint must be module:callable")
    return value


def validate_capability_name(value: str) -> str:
    """A capability name: a canonical lowercase dotted identifier, as a Contract name."""

    if unicodedata.normalize("NFC", value) != value or not _CAPABILITY_NAME_RE.fullmatch(value):
        raise ValueError(f"capability name {value!r} is not a canonical identifier")
    return value


def _relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in value
        or path.as_posix() != value
        or value == "."
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("an operation source path must be a normalized relative POSIX path")
    return value


class ContractRef(_Strict):
    """A pin to one Contract version.

    Operations write ``name@N``; the install pins each to the digest of the copy
    the package carries. A carried Contract pins every nested Contract by digest.
    """

    name: str
    version: int = Field(ge=1, le=999_999_999)
    digest: str | None = Field(default=None, exclude_if=lambda value: value is None)

    @classmethod
    def parse(cls, spelling: str) -> ContractRef:
        name, at, version = spelling.strip().partition("@")
        if not at or not version.isdigit():
            raise ValueError(f"a Contract pin is written name@N, not {spelling!r}")
        return cls(name=name, version=int(version))

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if unicodedata.normalize("NFC", value) != value or not _CONTRACT_NAME_RE.fullmatch(value):
            raise ValueError(f"Contract name {value!r} is not a canonical identifier")
        return value

    @field_validator("digest")
    @classmethod
    def _digest(cls, value: str | None) -> str | None:
        if value is not None and not SHA256_RE.fullmatch(value):
            raise ValueError("a Contract pin digest is a sha256 digest")
        return value

    @property
    def spelling(self) -> str:
        return f"{self.name}@{self.version}"


def _contract_ref(value: object) -> object:
    """Packages write ``name@N``; the structured form is accepted as is."""

    if isinstance(value, str):
        return ContractRef.parse(value)
    return value


class OperationRefusal(_Strict):
    """One typed refusal an operation may return: its detail Contract.

    ``absorbable`` declares that the refusal means "this input is unavailable",
    which a Line acquisition rule may absorb instead of failing the run.
    """

    contract: ContractRef
    absorbable: bool = False

    _contract = field_validator("contract", mode="before")(_contract_ref)


class OperationSource(_Strict):
    """Where an operation's code is.

    ``path`` is relative to the root of ``repository`` (or, for a local build
    with no repository, to the provider's project directory). ``revision`` is
    the full commit id the build was made from, and needs ``repository``.
    """

    path: str = Field(max_length=512)
    repository: str | None = Field(default=None, exclude_if=lambda value: value is None)
    revision: str | None = Field(default=None, exclude_if=lambda value: value is None)

    _path = field_validator("path")(_relative_path)

    @field_validator("repository")
    @classmethod
    def _repository(cls, value: str | None) -> str | None:
        if value is not None and not _REPOSITORY_RE.fullmatch(value):
            raise ValueError("an operation source repository is an https URL")
        return value

    @field_validator("revision")
    @classmethod
    def _revision(cls, value: str | None) -> str | None:
        if value is not None and not _REVISION_RE.fullmatch(value):
            raise ValueError("an operation source revision is a full lowercase commit id")
        return value

    @model_validator(mode="after")
    def _repository_for_revision(self) -> OperationSource:
        if self.revision is not None and self.repository is None:
            raise ValueError("an operation source revision names its repository")
        return self


class ProviderOperation(_Strict):
    """One operation a provider package serves (registration schema 3).

    ``serves`` lists capability names, sorted and once each, at most sixteen,
    none under ``workspace.``: fit is checked against whichever version a slot
    pins. ``may_truncate``: the operation may return a truncated listing, so it
    binds only a slot that declares ``may_truncate`` too.
    """

    model_config = ConfigDict(
        extra="forbid", frozen=True, populate_by_name=True, serialize_by_alias=True
    )

    name: str
    description: str = Field(min_length=1, max_length=MAX_OPERATION_DESCRIPTION)
    source: OperationSource
    serves: tuple[str, ...] = Field(min_length=1, max_length=MAX_SERVES)
    in_: ContractRef = Field(alias="in")
    out: ContractRef
    effect: OperationEffect
    may_truncate: bool = Field(default=False, exclude_if=lambda value: value is False)
    refusals: dict[str, OperationRefusal] = Field(
        default_factory=dict, exclude_if=lambda value: not value
    )
    entrypoint: str

    _contracts = field_validator("in_", "out", mode="before")(_contract_ref)
    _entrypoint = field_validator("entrypoint")(callable_path)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if not OPERATION_NAME_RE.fullmatch(value):
            raise ValueError(f"operation name {value!r} is not a canonical lowercase identifier")
        return value

    @field_validator("description")
    @classmethod
    def _description(cls, value: str) -> str:
        if unicodedata.normalize("NFC", value) != value or value != value.strip():
            raise ValueError("an operation description is trimmed NFC text")
        return value

    @field_validator("serves")
    @classmethod
    def _serves(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for name in value:
            validate_capability_name(name)
            if name.startswith(RESERVED_CAPABILITY_PREFIX):
                raise ValueError(
                    f"`{name}` is served by core. Capabilities under "
                    f"{RESERVED_CAPABILITY_PREFIX} are core's; a package serves none of them"
                )
        if value != tuple(sorted(set(value), key=_utf8)):
            raise ValueError("serves lists capability names sorted and once each")
        return value

    @field_validator("refusals")
    @classmethod
    def _refusals(cls, value: dict[str, OperationRefusal]) -> dict[str, OperationRefusal]:
        for code in value:
            check_operation_refusal_code(code)
        return dict(sorted(value.items(), key=lambda item: _utf8(item[0])))

    def contract_refs(self) -> tuple[ContractRef, ...]:
        """Every Contract this operation pins: in, out, then refusals by code."""

        return (self.in_, self.out, *(item.contract for item in self.refusals.values()))

    @property
    def pinned(self) -> bool:
        return all(ref.digest is not None for ref in self.contract_refs())

    def declares_refusal(self, code: str) -> bool:
        return code in self.refusals

    def signature_digest(self) -> str:
        """The signature digest of a pinned operation; ``ValueError`` if unpinned."""

        if self.in_.digest is None or self.out.digest is None:
            raise ValueError(
                f"operation {self.name!r} is not pinned; a signature is keyed by exact "
                "Contract digests"
            )
        return operation_signature_digest(
            in_digest=self.in_.digest, out_digest=self.out.digest, effect=self.effect
        )

    def wire(self) -> dict[str, Any]:
        """The operation exactly as a registration document spells it."""

        document: dict[str, Any] = self.model_dump(mode="json", by_alias=True)
        return document


def operation_signature_digest(*, in_digest: str, out_digest: str, effect: OperationEffect) -> str:
    """``{in, out, effect}`` digested exactly as core digests an operation signature.

    Core's rule is a SHA-256 over the canonical JSON of the preimage with a
    reserved ``tag`` field naming the domain; this reproduces it byte for byte
    (the cross-check against core's own function is part of the repository's
    core integration lane).
    """

    for value in (in_digest, out_digest):
        if not SHA256_RE.fullmatch(value):
            raise ValueError(f"expected a sha256:<hex> Contract digest, got {value!r}")
    preimage = {
        "tag": OPERATION_SIGNATURE_DOMAIN,
        "in": in_digest,
        "out": out_digest,
        "effect": effect,
    }
    return "sha256:" + hashlib.sha256(canonical_json(preimage)).hexdigest()


def validate_operations(operations: Iterable[ProviderOperation]) -> tuple[ProviderOperation, ...]:
    """Operations sorted by name (UTF-8 byte order) and each named once."""

    items = tuple(operations)
    names = tuple(item.name for item in items)
    if not names:
        raise ValueError("a package declares one or more operations")
    if names != tuple(sorted(set(names), key=lambda name: name.encode("utf-8"))):
        raise ValueError("operations must be sorted by name and each named once")
    return items


def pin_operation(
    operation: ProviderOperation, digests: Callable[[ContractRef], str]
) -> ProviderOperation:
    """``operation`` with every Contract it names pinned to ``digests(ref)``.

    A ref that already carries a digest must keep it; a different answer is a
    package built against another Contract, and it refuses.
    """

    def pin(ref: ContractRef) -> ContractRef:
        digest = digests(ref)
        if ref.digest is not None and ref.digest != digest:
            raise ValueError(
                f"operation {operation.name!r} pins {ref.spelling} at {ref.digest}, not {digest}"
            )
        return ref.model_copy(update={"digest": digest})

    return operation.model_copy(
        update={
            "in_": pin(operation.in_),
            "out": pin(operation.out),
            "refusals": {
                code: refusal.model_copy(update={"contract": pin(refusal.contract)})
                for code, refusal in operation.refusals.items()
            },
        }
    )
