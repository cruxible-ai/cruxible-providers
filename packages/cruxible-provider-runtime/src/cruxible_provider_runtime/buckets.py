"""Input-bucket vocabulary format, selectors, and measured classification.

Buckets are *measured, not claimed*. A manifest declares which buckets an
implementation claims to serve; the bucket recorded on a run is derived by the
interface's registered classifier from the actual input. An input classifying
into an unclaimed bucket refuses at admission.

Vocabulary format
-----------------

A vocabulary is one YAML/JSON document per interface::

    interface_id: ts.forecast
    status: draft
    version: 1
    dimensions:
      - name: frequency
        description: sampling cadence of the series
        classes:
          - id: sub_hourly
            description: mean spacing under one hour
          ...

A *bucket id* is the canonical rendering of one class per dimension, in the
vocabulary's declared dimension order::

    frequency=daily;length=medium;domain=counts

A *bucket selector* is the same shape with ``*`` permitted for a dimension,
so a manifest can claim a whole face of the cube without enumerating it::

    frequency=daily;length=*;domain=counts
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .errors import RefusalCode, refuse

__all__ = [
    "BucketClass",
    "BucketDimension",
    "BucketVocabulary",
    "BucketSelector",
    "bucket_id",
    "parse_bucket_id",
    "BucketClassifier",
]

BucketClassifier = Callable[[Mapping[str, Any]], Mapping[str, str] | None]
"""Derives a dimension->class mapping from an actual input, or ``None``."""

_SEP = ";"
_EQ = "="


class BucketClass(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    description: str

    @field_validator("id")
    @classmethod
    def _no_separators(cls, value: str) -> str:
        if _SEP in value or _EQ in value or value == "*":
            raise ValueError(f"invalid bucket class id: {value!r}")
        return value


class BucketDimension(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    description: str
    classes: tuple[BucketClass, ...]

    @field_validator("name")
    @classmethod
    def _no_separators(cls, value: str) -> str:
        if _SEP in value or _EQ in value:
            raise ValueError(f"invalid bucket dimension name: {value!r}")
        return value

    @field_validator("classes")
    @classmethod
    def _non_empty_unique(cls, value: tuple[BucketClass, ...]) -> tuple[BucketClass, ...]:
        if not value:
            raise ValueError("a bucket dimension needs at least one class")
        ids = [c.id for c in value]
        if len(set(ids)) != len(ids):
            raise ValueError(f"duplicate bucket class ids: {ids}")
        return value

    @property
    def class_ids(self) -> tuple[str, ...]:
        return tuple(c.id for c in self.classes)


class BucketVocabulary(BaseModel):
    """The registered bucket vocabulary for one interface."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    interface_id: str
    version: int = 1
    status: Literal["draft", "accepted"] = "draft"
    description: str = ""
    dimensions: tuple[BucketDimension, ...]

    @field_validator("dimensions")
    @classmethod
    def _non_empty_unique(cls, value: tuple[BucketDimension, ...]) -> tuple[BucketDimension, ...]:
        if not value:
            raise ValueError("a bucket vocabulary needs at least one dimension")
        names = [d.name for d in value]
        if len(set(names)) != len(names):
            raise ValueError(f"duplicate bucket dimension names: {names}")
        return value

    @property
    def dimension_names(self) -> tuple[str, ...]:
        return tuple(d.name for d in self.dimensions)

    def bucket_id(self, assignment: Mapping[str, str]) -> str:
        """Render a full class assignment as a canonical bucket id."""

        parts: list[str] = []
        for dimension in self.dimensions:
            try:
                value = assignment[dimension.name]
            except KeyError:
                raise refuse(
                    RefusalCode.INVALID_BUCKET_VOCABULARY,
                    f"assignment is missing dimension {dimension.name!r}",
                    interface_id=self.interface_id,
                    assignment=dict(assignment),
                ) from None
            if value not in dimension.class_ids:
                raise refuse(
                    RefusalCode.INVALID_BUCKET_VOCABULARY,
                    f"class {value!r} is not registered for dimension {dimension.name!r}",
                    interface_id=self.interface_id,
                    known=list(dimension.class_ids),
                )
            parts.append(f"{dimension.name}{_EQ}{value}")
        extra = set(assignment) - set(self.dimension_names)
        if extra:
            raise refuse(
                RefusalCode.INVALID_BUCKET_VOCABULARY,
                f"assignment carries unregistered dimensions: {sorted(extra)}",
                interface_id=self.interface_id,
            )
        return _SEP.join(parts)

    def all_bucket_ids(self) -> tuple[str, ...]:
        """Every bucket in the cube, in canonical order (small by construction)."""

        buckets: list[str] = [""]
        for dimension in self.dimensions:
            buckets = [
                (prefix + _SEP if prefix else "") + f"{dimension.name}{_EQ}{class_id}"
                for prefix in buckets
                for class_id in dimension.class_ids
            ]
        return tuple(buckets)

    def parse_selector(self, selector: str) -> BucketSelector:
        return BucketSelector.parse(selector, self)


class BucketSelector(BaseModel):
    """A claim over one or more buckets; ``*`` matches any class of a dimension."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    interface_id: str
    pattern: tuple[tuple[str, str], ...] = Field(
        description="(dimension, class-or-star) pairs in vocabulary order",
    )

    @classmethod
    def parse(cls, selector: str, vocabulary: BucketVocabulary) -> BucketSelector:
        assignment = _parse_pairs(selector, vocabulary.interface_id)
        pattern: list[tuple[str, str]] = []
        for dimension in vocabulary.dimensions:
            if dimension.name not in assignment:
                raise refuse(
                    RefusalCode.INVALID_BUCKET_VOCABULARY,
                    f"selector {selector!r} is missing dimension {dimension.name!r}",
                    interface_id=vocabulary.interface_id,
                )
            value = assignment.pop(dimension.name)
            if value != "*" and value not in dimension.class_ids:
                raise refuse(
                    RefusalCode.INVALID_BUCKET_VOCABULARY,
                    f"selector {selector!r} names unregistered class {value!r}",
                    interface_id=vocabulary.interface_id,
                    dimension=dimension.name,
                    known=list(dimension.class_ids),
                )
            pattern.append((dimension.name, value))
        if assignment:
            raise refuse(
                RefusalCode.INVALID_BUCKET_VOCABULARY,
                f"selector {selector!r} names unregistered dimensions: {sorted(assignment)}",
                interface_id=vocabulary.interface_id,
            )
        return cls(interface_id=vocabulary.interface_id, pattern=tuple(pattern))

    def matches(self, candidate_bucket_id: str) -> bool:
        assignment = _parse_pairs(candidate_bucket_id, self.interface_id)
        for name, value in self.pattern:
            actual = assignment.get(name)
            if actual is None:
                return False
            if value != "*" and actual != value:
                return False
        return True

    def render(self) -> str:
        return _SEP.join(f"{name}{_EQ}{value}" for name, value in self.pattern)


def _parse_pairs(text: str, interface_id: str) -> dict[str, str]:
    assignment: dict[str, str] = {}
    if not text:
        raise refuse(
            RefusalCode.INVALID_BUCKET_VOCABULARY,
            "empty bucket expression",
            interface_id=interface_id,
        )
    for chunk in text.split(_SEP):
        if _EQ not in chunk:
            raise refuse(
                RefusalCode.INVALID_BUCKET_VOCABULARY,
                f"malformed bucket expression segment {chunk!r}",
                interface_id=interface_id,
            )
        name, _, value = chunk.partition(_EQ)
        if name in assignment:
            raise refuse(
                RefusalCode.INVALID_BUCKET_VOCABULARY,
                f"dimension {name!r} repeated in bucket expression",
                interface_id=interface_id,
            )
        assignment[name] = value
    return assignment


def bucket_id(vocabulary: BucketVocabulary, assignment: Mapping[str, str]) -> str:
    return vocabulary.bucket_id(assignment)


def parse_bucket_id(vocabulary: BucketVocabulary, value: str) -> dict[str, str]:
    """Parse and validate a full (star-free) bucket id against ``vocabulary``."""

    assignment = _parse_pairs(value, vocabulary.interface_id)
    # bucket_id() re-validates dimension coverage and class membership.
    vocabulary.bucket_id(assignment)
    return assignment
