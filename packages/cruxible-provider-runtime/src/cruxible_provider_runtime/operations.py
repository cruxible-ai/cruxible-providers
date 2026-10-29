"""Typed provider operations: what a provider package serves, by Contract signature.

A provider package declares **operations** in its ``registration.json``. Each
names its input and output Contracts (``name@N``), its effect, the typed
refusals it may return (each a code and a detail Contract), an optional
enumeration declaration, and the entrypoint that serves it. This is the same
shape core reads (``cruxible_client.contracts.provider_operations
.ProviderOperation``); the runtime keeps its own mirror so that a package can be
read and run without core installed.

The package names Contracts by ``name@N`` and never ships their bodies or
digests: the install pins each to the exact Contract accepted at the install
base, and a Line binds an exact ``{build, operation}`` whose signature digest
travels in every run context. The runtime never interprets a Contract's fields.

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
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .canonical import SHA256_RE, canonical_json
from .errors import check_operation_refusal_code
from .protocol import OPERATION_NAME_RE

__all__ = [
    "OPERATION_EFFECTS",
    "OPERATION_SIGNATURE_DOMAIN",
    "ContractRef",
    "EnumeratedField",
    "OperationEffect",
    "OperationEnumeration",
    "OperationRefusal",
    "ProviderOperation",
    "operation_signature_digest",
    "pin_operation",
    "validate_operations",
]

OperationEffect = Literal["pure", "external_read", "external_mutation"]
OPERATION_EFFECTS: tuple[OperationEffect, ...] = ("pure", "external_read", "external_mutation")

OPERATION_SIGNATURE_DOMAIN = "cruxible-operation-signature-v1"
"""Core's signature-digest domain. The digest is core's to define; this mirrors it."""

_CONTRACT_NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_FIELD_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _entrypoint(value: str) -> str:
    module, separator, member = value.partition(":")
    if not separator or not module or not member:
        raise ValueError(f"an operation entrypoint is 'module:object', got {value!r}")
    return value


class ContractRef(_Strict):
    """A pin to one Contract version. Packages write ``name@N``; the install adds the digest."""

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


class EnumeratedField(_Strict):
    at: str = Field(min_length=1, max_length=512)


class OperationEnumeration(_Strict):
    """What a provider-parsed operation enumerates. Declared and recorded, not verified."""

    capture: str
    selector: str = Field(min_length=1, max_length=512)
    fields: dict[str, EnumeratedField] = Field(min_length=1, max_length=256)

    @field_validator("capture")
    @classmethod
    def _capture(cls, value: str) -> str:
        if not _FIELD_RE.fullmatch(value):
            raise ValueError("an enumeration names one input field holding its Capture")
        return value

    @field_validator("fields")
    @classmethod
    def _fields(cls, value: dict[str, EnumeratedField]) -> dict[str, EnumeratedField]:
        for name in value:
            if not _FIELD_RE.fullmatch(name):
                raise ValueError(f"enumerated field {name!r} is not a canonical field name")
        return value


class ProviderOperation(_Strict):
    """One operation a provider package serves."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, populate_by_name=True, serialize_by_alias=True
    )

    name: str
    in_: ContractRef = Field(alias="in")
    out: ContractRef
    effect: OperationEffect
    refusals: dict[str, OperationRefusal] = Field(
        default_factory=dict, exclude_if=lambda value: not value
    )
    enumerates: OperationEnumeration | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    entrypoint: str

    _contracts = field_validator("in_", "out", mode="before")(_contract_ref)
    _entrypoint = field_validator("entrypoint")(_entrypoint)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if not OPERATION_NAME_RE.fullmatch(value):
            raise ValueError(f"operation name {value!r} is not a canonical lowercase identifier")
        return value

    @field_validator("refusals")
    @classmethod
    def _refusals(cls, value: dict[str, OperationRefusal]) -> dict[str, OperationRefusal]:
        for code in value:
            check_operation_refusal_code(code)
        return dict(sorted(value.items()))

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
