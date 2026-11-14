"""
Migrating JSON Pointer references between versions of a document.

A new version of a document may fold parts of an object into an array
(and reorder the array entries by some stable business identifier), or
move fields around altogether. Business rules commonly persist JSON
Pointers into the document, so the pointers themselves have to be
repaired alongside the data.

The repair combines three sources of evidence:

* the old document, used to read the value an old pointer reaches and to
  confirm an untouched location still means the same business field,
* the new document, searched for where that value now lives, and
* a migration mapping describing the structural changes the value
  comparison cannot see by itself (deletions, renames, array folds).

Every old pointer is reported exactly once as having a unique new
location, being deleted, or having several ambiguous candidate
locations. Each report keeps the evidence the decision was based on so
that ambiguous or deleted references can be reviewed afterwards.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping as MappingABC, Sequence
from enum import Enum
from typing import Any

from attrs import field

from referencing import exceptions
from referencing._attrs import frozen

#: A JSON document (or any fragment of one).
JSON = Any

__all__ = [
    "Match",
    "MigrationReport",
    "PointerMigrator",
    "Status",
    "build",
    "escape",
    "normalize_mapping",
    "parse",
    "resolve",
    "unescape",
]


def escape(segment: str) -> str:
    """
    Escape a single reference token per RFC 6901.

    Tildes must be escaped before slashes, otherwise escaping a slash
    would itself be mangled.
    """
    return segment.replace("~", "~0").replace("/", "~1")


def unescape(token: str) -> str:
    """
    Unescape a single JSON Pointer reference token per RFC 6901.
    """
    return token.replace("~1", "/").replace("~0", "~")


def parse(pointer: str) -> tuple[str, ...]:
    """
    Split a JSON Pointer into its unescaped reference tokens.

    Raises:

        `referencing.exceptions.InvalidPointer`

            if the pointer is not syntactically valid, including tokens
            escaped in a way RFC 6901 forbids (e.g. ``~2`` or a
            trailing ``~``).
    """
    if pointer == "":
        return ()
    if not pointer.startswith("/"):
        raise exceptions.InvalidPointer(pointer=pointer)

    tokens: list[str] = []
    for raw in pointer[1:].split("/"):
        if _invalid_escape(raw):
            raise exceptions.InvalidPointer(pointer=pointer)
        tokens.append(unescape(raw))
    return tuple(tokens)


def _invalid_escape(token: str) -> bool:
    index = 0
    while index < len(token):
        if token[index] == "~":
            if index + 1 >= len(token) or token[index + 1] not in "01":
                return True
            index += 2
        else:
            index += 1
    return False


def build(tokens: Iterable[str | int]) -> str:
    """
    Build a JSON Pointer from unescaped reference tokens.
    """
    return "".join(
        "/" + (str(token) if isinstance(token, int) else escape(token))
        for token in tokens
    )


def _as_index(token: str, length: int) -> int:
    if (
        not token
        or token == "-"
        or (token.startswith("0") and token != "0")
    ):
        raise LookupError(token)
    try:
        index = int(token)
    except ValueError as error:
        raise LookupError(token) from error
    if not 0 <= index < length:
        raise LookupError(token)
    return index


def resolve(document: JSON, pointer: str) -> JSON:
    """
    Resolve a JSON Pointer against a document.

    Raises:

        `LookupError` if the pointer does not lead anywhere.
    """
    current = document
    for token in parse(pointer):
        try:
            if isinstance(current, MappingABC):
                current = current[token]
            elif _is_array(current):
                current = current[_as_index(token, len(current))]
            else:
                raise LookupError(pointer)
        except LookupError as error:
            raise LookupError(pointer) from error
    return current


def _is_array(value: JSON) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, str)


def _is_container(value: JSON) -> bool:
    return isinstance(value, (MappingABC, Sequence)) and not isinstance(
        value,
        str,
    )


class Status(Enum):
    """
    The outcome of migrating a single old pointer.
    """

    #: Exactly one new location was found.
    unique = "unique"

    #: The referenced field was deleted (or explicitly dropped).
    deleted = "deleted"

    #: More than one new location plausibly holds the field.
    ambiguous = "ambiguous"

    #: The old pointer itself does not resolve within the old document.
    dangling = "dangling"

    #: An untouched location now holds a different business field.
    repurposed = "repurposed"


@frozen
class Match:
    """
    A single candidate location within the new document.
    """

    pointer: str
    value: JSON


@frozen
class MigrationReport:
    """
    The result of migrating one old pointer, with its supporting evidence.
    """

    old_pointer: str
    status: Status
    new_pointer: str | None = None
    candidates: tuple[Match, ...] = ()
    old_value: JSON = None
    new_value: JSON = None
    reason: str = ""
    sources: tuple[str, ...] = ()

    @property
    def matched(self) -> bool:
        """
        Whether a unique new location was determined.
        """
        return self.status is Status.unique


@frozen
class _Directive:
    """
    One normalized entry of a migration mapping.
    """

    old_pointer: str
    deleted: bool
    new_pointer: str | None = None
    key: str | None = None
    sources: tuple[str, ...] = ()

    @property
    def folded(self) -> bool:
        return self.key is not None


def _as_pointer(value: object, entry: object) -> str:
    if not isinstance(value, str) or not value:
        raise exceptions.InvalidMigration(entry=entry)
    parse(value)
    return value


def _sources_of(entry: MappingABC[str, object]) -> tuple[str, ...]:
    source = entry.get("source")
    if source is None:
        return ()
    if isinstance(source, str):
        return (source,)
    if isinstance(source, Sequence) and all(
        isinstance(item, str) for item in source
    ):
        return tuple(source)
    raise exceptions.InvalidMigration(entry=dict(entry))


def normalize_mapping(
    mapping: MappingABC[str, object],
) -> tuple[_Directive, ...]:
    """
    Validate and normalize a migration mapping into typed directives.

    Accepted entry shapes, keyed by the old pointer, are:

    * a new pointer string, for a straightforward rename or move,
    * ``None`` or ``{"action": "deleted"}`` for a deleted field,
    * ``{"action": "moved", "to": <pointer>}`` for an explicit move,
    * ``{"action": "folded", "to": <array pointer>, "key": <field>}``
      for an object folded into an array; each array item carries the
      old object key in ``key`` as a stable business identifier.

    An optional ``source`` label (string or list of strings) is carried
    onto every report as auditable provenance for the directive.
    """
    directives: list[_Directive] = []
    for old_pointer, entry in mapping.items():
        parse(old_pointer)
        if isinstance(entry, str):
            directives.append(
                _Directive(
                    old_pointer=old_pointer,
                    deleted=False,
                    new_pointer=_as_pointer(entry, entry=entry),
                    sources=("mapping",),
                ),
            )
        elif entry is None:
            directives.append(
                _Directive(
                    old_pointer=old_pointer,
                    deleted=True,
                    sources=("mapping",),
                ),
            )
        elif isinstance(entry, MappingABC):
            directives.append(_normalize_object_entry(old_pointer, entry))
        else:
            raise exceptions.InvalidMigration(entry=entry)
    return tuple(directives)


def _normalize_object_entry(
    old_pointer: str,
    entry: MappingABC[str, object],
) -> _Directive:
    action = entry.get("action", "moved")
    sources = _sources_of(entry) or ("mapping",)
    if action == "deleted":
        return _Directive(
            old_pointer=old_pointer,
            deleted=True,
            sources=sources,
        )
    if action == "moved":
        if "to" not in entry:
            raise exceptions.InvalidMigration(entry=dict(entry))
        return _Directive(
            old_pointer=old_pointer,
            deleted=False,
            new_pointer=_as_pointer(entry["to"], entry=dict(entry)),
            sources=sources,
        )
    if action == "folded":
        target = entry.get("to")
        key = entry.get("key")
        if not isinstance(target, str) or not isinstance(key, str) or not key:
            raise exceptions.InvalidMigration(entry=dict(entry))
        parse(target)
        return _Directive(
            old_pointer=old_pointer,
            deleted=False,
            new_pointer=target,
            key=key,
            sources=sources,
        )
    raise exceptions.InvalidMigration(entry=dict(entry))


@frozen
class PointerMigrator:
    """
    Migrate JSON Pointer references from an old document to a new one.
    """

    old_document: JSON
    new_document: JSON
    _directives: tuple[_Directive, ...] = field(default=(), alias="mapping")
    _exact: MappingABC[str, _Directive] = field(init=False)
    _folds: tuple[_Directive, ...] = field(init=False)

    def __attrs_post_init__(self) -> None:
        exact = {
            directive.old_pointer: directive
            for directive in self._directives
            if not directive.folded
        }
        folds = tuple(
            directive for directive in self._directives if directive.folded
        )
        object.__setattr__(self, "_exact", exact)
        object.__setattr__(self, "_folds", folds)

    @classmethod
    def create(
        cls,
        old_document: JSON,
        new_document: JSON,
        mapping: MappingABC[str, object] | None = None,
    ) -> PointerMigrator:
        """
        Build a migrator, validating the supplied migration mapping.
        """
        directives = normalize_mapping(mapping or {})
        return cls(
            old_document=old_document,
            new_document=new_document,
            mapping=directives,
        )

    def migrate(
        self,
        pointers: Iterable[str],
    ) -> tuple[MigrationReport, ...]:
        """
        Migrate each old pointer, preserving order and de-duplicating.
        """
        reports: list[MigrationReport] = []
        seen: set[str] = set()
        for pointer in pointers:
            if pointer in seen:
                continue
            seen.add(pointer)
            reports.append(self.migrate_one(pointer))
        return tuple(reports)

    def migrate_one(self, pointer: str) -> MigrationReport:
        """
        Migrate a single old pointer.
        """
        parse(pointer)

        directive = self._exact.get(pointer)
        if directive is not None:
            return self._apply_exact(pointer, directive)

        fold = self._fold_for(pointer)
        if fold is not None:
            return self._apply_fold(pointer, fold)

        try:
            old_value = resolve(self.old_document, pointer)
        except LookupError:
            return MigrationReport(
                old_pointer=pointer,
                status=Status.dangling,
                reason="pointer does not resolve within the old document",
                sources=("old-document",),
            )

        tokens = parse(pointer)
        if _exists(self.new_document, tokens):
            new_value = resolve(self.new_document, pointer)
            if _same_scalar_or_equal(new_value, old_value):
                return MigrationReport(
                    old_pointer=pointer,
                    status=Status.unique,
                    new_pointer=pointer,
                    old_value=old_value,
                    new_value=new_value,
                    reason="location unchanged and value preserved",
                    sources=("value-comparison",),
                )
            return self._repurposed(pointer, old_value, new_value)

        return self._match_by_value(pointer, old_value)

    def _apply_exact(
        self,
        pointer: str,
        directive: _Directive,
    ) -> MigrationReport:
        old_value = self._try_resolve_old(pointer)
        if directive.deleted:
            return MigrationReport(
                old_pointer=pointer,
                status=Status.deleted,
                old_value=old_value,
                reason="marked deleted in the migration mapping",
                sources=directive.sources,
            )

        target = directive.new_pointer
        assert target is not None
        try:
            new_value = resolve(self.new_document, target)
        except LookupError:
            return MigrationReport(
                old_pointer=pointer,
                status=Status.deleted,
                old_value=old_value,
                reason=(
                    "mapping target does not resolve within the new document"
                ),
                sources=directive.sources,
            )
        return MigrationReport(
            old_pointer=pointer,
            status=Status.unique,
            new_pointer=target,
            old_value=old_value,
            new_value=new_value,
            reason="recorded move in the migration mapping",
            sources=directive.sources,
        )

    def _fold_for(self, pointer: str) -> _Directive | None:
        for fold in self._folds:
            prefix = fold.old_pointer
            if pointer == prefix or pointer.startswith(prefix + "/"):
                return fold
        return None

    def _apply_fold(
        self,
        pointer: str,
        fold: _Directive,
    ) -> MigrationReport:
        target = fold.new_pointer
        assert target is not None and fold.key is not None
        old_value = self._try_resolve_old(pointer)

        try:
            items = resolve(self.new_document, target)
        except LookupError:
            return MigrationReport(
                old_pointer=pointer,
                status=Status.deleted,
                old_value=old_value,
                reason="fold target array does not exist in the new document",
                sources=fold.sources,
            )
        if not _is_array(items):
            return MigrationReport(
                old_pointer=pointer,
                status=Status.ambiguous,
                old_value=old_value,
                reason="fold target in the new document is not an array",
                sources=fold.sources,
            )

        prefix_tokens = parse(fold.old_pointer)
        remainder = parse(pointer)[len(prefix_tokens):]
        if not remainder:
            return MigrationReport(
                old_pointer=pointer,
                status=Status.unique,
                new_pointer=target,
                old_value=old_value,
                new_value=items,
                reason="folded object collection now lives in the array",
                sources=fold.sources,
            )

        business_id = remainder[0]
        subpath = remainder[1:]
        matches = [
            index
            for index, item in enumerate(items)
            if isinstance(item, MappingABC)
            and fold.key in item
            and _same_scalar_or_equal(item[fold.key], business_id)
        ]
        if not matches:
            return MigrationReport(
                old_pointer=pointer,
                status=Status.deleted,
                old_value=old_value,
                reason=(
                    "no array item carries the business identifier "
                    f"{business_id!r} in {fold.key!r}"
                ),
                sources=fold.sources,
            )

        candidates: list[Match] = []
        for index in matches:
            candidate_tokens = (*parse(target), index, *subpath)
            candidate_pointer = build(candidate_tokens)
            if _exists(self.new_document, candidate_tokens):
                candidates.append(
                    Match(
                        pointer=candidate_pointer,
                        value=resolve(self.new_document, candidate_pointer),
                    ),
                )
        if not candidates:
            return MigrationReport(
                old_pointer=pointer,
                status=Status.deleted,
                old_value=old_value,
                reason=(
                    "the business identifier survives but the referenced "
                    "sub-field was removed"
                ),
                sources=fold.sources,
            )
        return self._report_candidates(
            pointer,
            old_value,
            candidates,
            reason="located via the stable business identifier",
            sources=fold.sources,
        )

    def _repurposed(
        self,
        pointer: str,
        old_value: JSON,
        new_value: JSON,
    ) -> MigrationReport:
        candidates = self._value_candidates(old_value)
        return MigrationReport(
            old_pointer=pointer,
            status=Status.repurposed,
            candidates=tuple(candidates),
            old_value=old_value,
            new_value=new_value,
            reason=(
                "the untouched location now holds a different business "
                "field; it must not be treated as a successful migration"
            ),
            sources=("value-comparison",),
        )

    def _match_by_value(
        self,
        pointer: str,
        old_value: JSON,
    ) -> MigrationReport:
        if _is_container(old_value):
            return MigrationReport(
                old_pointer=pointer,
                status=Status.ambiguous,
                old_value=old_value,
                reason=(
                    "the old value is a container with no mapping entry; "
                    "structural matching alone would be ambiguous"
                ),
                sources=("value-comparison",),
            )
        return self._report_candidates(
            pointer,
            old_value,
            self._value_candidates(old_value),
            reason="located by matching the old scalar value",
            sources=("value-comparison",),
        )

    def _value_candidates(self, old_value: JSON) -> list[Match]:
        return [
            Match(pointer=location, value=value)
            for location, value in _find_values(self.new_document, old_value)
        ]

    def _report_candidates(
        self,
        pointer: str,
        old_value: JSON,
        candidates: list[Match],
        *,
        reason: str,
        sources: tuple[str, ...],
    ) -> MigrationReport:
        if len(candidates) == 1:
            only = candidates[0]
            return MigrationReport(
                old_pointer=pointer,
                status=Status.unique,
                new_pointer=only.pointer,
                candidates=tuple(candidates),
                old_value=old_value,
                new_value=only.value,
                reason=reason,
                sources=sources,
            )
        if not candidates:
            return MigrationReport(
                old_pointer=pointer,
                status=Status.deleted,
                old_value=old_value,
                reason="no matching value remains in the new document",
                sources=sources,
            )
        return MigrationReport(
            old_pointer=pointer,
            status=Status.ambiguous,
            candidates=tuple(candidates),
            old_value=old_value,
            reason="several new locations hold the same value",
            sources=sources,
        )

    def _try_resolve_old(self, pointer: str) -> JSON:
        try:
            return resolve(self.old_document, pointer)
        except LookupError:
            return None


def _same_scalar_or_equal(left: JSON, right: JSON) -> bool:
    # JSON booleans must not compare equal to the numbers 0 and 1.
    if type(left) is not type(right):
        return False
    return left == right


def _exists(document: JSON, tokens: Sequence[str | int]) -> bool:
    current = document
    for token in tokens:
        if isinstance(current, MappingABC):
            if token not in current:
                return False
            current = current[token]
        elif _is_array(current):
            if not isinstance(token, int):
                return False
            if not 0 <= token < len(current):
                return False
            current = current[token]
        else:
            return False
    return True


def _find_values(
    document: JSON,
    expected: JSON,
) -> list[tuple[str, JSON]]:
    found: list[tuple[str, JSON]] = []

    def walk(node: JSON, tokens: list[str | int]) -> None:
        if not _is_container(node) and _same_scalar_or_equal(node, expected):
            found.append((build(tokens), node))
        if isinstance(node, MappingABC):
            for key, value in node.items():
                walk(value, [*tokens, str(key)])
        elif _is_array(node):
            for index, value in enumerate(node):
                walk(value, [*tokens, index])

    walk(document, [])
    return found
