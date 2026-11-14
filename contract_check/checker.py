"""Judge a data-product compatibility promise against two schema versions.

The check combines three inputs:

1. Version numbers (semver): the bump from old to new decides which
   read/write combinations the release notes are allowed to promise.
2. Field conventions: each field is required or optional; enum fields
   carry their full value set; readers are either ``strict`` (reject
   unknown fields) or ``lenient`` (ignore unknown fields). Unknown enum
   values are rejected by every reader, because the reader cannot map
   them to a code path.
3. Allowed read/write combinations: for a backward-compatible
   (minor/patch) release both ``old_write_new_read`` and
   ``new_write_old_read`` must succeed for every record the writer can
   legally emit.

When the promise is judged broken, each violation carries the shortest
record that is legal under the writer's schema yet rejected by the
reader: required fields only, canonical smallest values, plus the single
minimal mutation that triggers the rejection.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from semver import Version

OLD_WRITE_NEW_READ = "old_write_new_read"
NEW_WRITE_OLD_READ = "new_write_old_read"

#: Read/write combinations a backward-compatible release must support.
BACKWARD_COMPATIBLE_SCOPE = frozenset({OLD_WRITE_NEW_READ, NEW_WRITE_OLD_READ})

#: Reader conventions for fields the reader's schema does not declare.
STRICT = "strict"
LENIENT = "lenient"

_SCALAR_CANONICAL = {"string": "", "integer": 0, "boolean": False}


@dataclass(frozen=True)
class Field:
    """One declared field of a record schema."""

    name: str
    type: str = "string"
    required: bool = True
    values: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, raw: dict) -> Field:
        type_ = raw.get("type", "string")
        values = tuple(raw.get("values", ()))
        if type_ == "enum" and not values:
            raise ValueError(f"enum field {raw['name']!r} must list values")
        if type_ != "enum" and type_ not in _SCALAR_CANONICAL:
            raise ValueError(f"unsupported type {type_!r} for field {raw['name']!r}")
        return cls(
            name=raw["name"],
            type=type_,
            required=bool(raw.get("required", True)),
            values=values,
        )

    def canonical_value(self):
        """Smallest legal value, used to keep counterexamples minimal."""
        if self.type == "enum":
            return self.values[0]
        return _SCALAR_CANONICAL[self.type]


@dataclass(frozen=True)
class Schema:
    """A versioned record schema of a data product."""

    product: str
    version: Version
    fields: tuple[Field, ...]

    @classmethod
    def from_dict(cls, raw: dict) -> Schema:
        return cls(
            product=raw["product"],
            version=Version.parse(raw["version"]),
            fields=tuple(Field.from_dict(f) for f in raw["fields"]),
        )

    @classmethod
    def load(cls, path: str | Path) -> Schema:
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    @property
    def field_map(self) -> dict[str, Field]:
        return {f.name: f for f in self.fields}

    def minimal_record(self) -> dict:
        """Shortest legal record: required fields, canonical values."""
        return {f.name: f.canonical_value() for f in self.fields if f.required}


@dataclass(frozen=True)
class Direction:
    """One read/write combination: writer schema, reader schema."""

    name: str
    writer: Schema
    reader: Schema


@dataclass(frozen=True)
class Violation:
    """A promised read/write combination that can fail, with a witness."""

    direction: str
    kind: str
    field: str
    detail: str
    record: dict = field(compare=False)

    def to_dict(self) -> dict:
        return {
            "direction": self.direction,
            "kind": self.kind,
            "field": self.field,
            "detail": self.detail,
            "record": self.record,
        }


@dataclass(frozen=True)
class Verdict:
    """Outcome of the contract check."""

    old_version: Version
    new_version: Version
    promise: str
    in_scope: frozenset[str]
    violations: tuple[Violation, ...]

    @property
    def status(self) -> str:
        if not self.in_scope:
            return "no_promise"
        return "broken" if self.violations else "kept"

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "old_version": str(self.old_version),
            "new_version": str(self.new_version),
            "promise": self.promise,
            "in_scope": sorted(self.in_scope),
            "violations": [v.to_dict() for v in self.violations],
        }


def promise_scope(old: Version, new: Version) -> tuple[str, frozenset[str]]:
    """Map a version bump to the read/write combinations it may promise.

    A major bump promises nothing. A minor or patch bump is a
    backward-compatible release: old readers must accept new writers'
    records and new readers must accept old writers' records.
    """
    if new <= old:
        raise ValueError(f"new version {new} must be greater than old version {old}")
    if new.major != old.major:
        return "none (major bump)", frozenset()
    return "backward compatible (minor/patch bump)", BACKWARD_COMPATIBLE_SCOPE


def check_direction(direction: Direction, reader_convention: str) -> list[Violation]:
    """Find every way the reader can reject a legal writer record."""
    writer_fields = direction.writer.field_map
    reader_fields = direction.reader.field_map
    violations = []

    for name, wfield in writer_fields.items():
        rfield = reader_fields.get(name)
        if rfield is None:
            if reader_convention == STRICT:
                record = direction.writer.minimal_record()
                record.setdefault(name, wfield.canonical_value())
                violations.append(
                    Violation(
                        direction=direction.name,
                        kind="unknown_field",
                        field=name,
                        detail=(
                            f"strict reader does not declare field {name!r} "
                            "and rejects records carrying it"
                        ),
                        record=record,
                    )
                )
            continue
        if wfield.type == "enum" and rfield.type == "enum":
            extra = [v for v in wfield.values if v not in rfield.values]
            if extra:
                record = direction.writer.minimal_record()
                record[name] = extra[0]
                violations.append(
                    Violation(
                        direction=direction.name,
                        kind="unknown_enum_value",
                        field=name,
                        detail=(
                            f"reader's enum {name!r} lacks value(s) "
                            f"{', '.join(repr(v) for v in extra)}"
                        ),
                        record=record,
                    )
                )

    for name, rfield in reader_fields.items():
        if not rfield.required:
            continue
        wfield = writer_fields.get(name)
        if wfield is None:
            violations.append(
                Violation(
                    direction=direction.name,
                    kind="missing_required_field",
                    field=name,
                    detail=(
                        f"reader requires field {name!r} but the writer's "
                        "schema does not declare it"
                    ),
                    record=direction.writer.minimal_record(),
                )
            )
        elif not wfield.required:
            violations.append(
                Violation(
                    direction=direction.name,
                    kind="required_tightened",
                    field=name,
                    detail=(
                        f"reader requires field {name!r} but the writer "
                        "may legally omit it"
                    ),
                    record=direction.writer.minimal_record(),
                )
            )

    return violations


def check_contract(old: Schema, new: Schema, reader_convention: str = STRICT) -> Verdict:
    """Judge whether the release keeps the promise its version bump makes."""
    if reader_convention not in (STRICT, LENIENT):
        raise ValueError(f"reader_convention must be {STRICT!r} or {LENIENT!r}")
    if old.product != new.product:
        raise ValueError(
            f"cannot compare different products: {old.product!r} vs {new.product!r}"
        )
    promise, in_scope = promise_scope(old.version, new.version)
    directions = [
        Direction(OLD_WRITE_NEW_READ, writer=old, reader=new),
        Direction(NEW_WRITE_OLD_READ, writer=new, reader=old),
    ]
    violations = [
        violation
        for direction in directions
        if direction.name in in_scope
        for violation in check_direction(direction, reader_convention)
    ]
    return Verdict(
        old_version=old.version,
        new_version=new.version,
        promise=promise,
        in_scope=in_scope,
        violations=tuple(violations),
    )
