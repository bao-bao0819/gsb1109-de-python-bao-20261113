"""
Compare two versions of a set of resources behind fixed business entries.

The typical use case is a contract service publishing a new version of a
collection of documents: a data collection side does not want to revalidate
every historical batch, so it needs the *minimal* list of fields whose
parsing conclusion changes between the two versions, restricted to the
entries and fields the business side actually uses.

A field which has merely moved location but keeps an equivalent definition
is not a business change. A field which keeps the same name while its unit,
enumeration or missing-value convention changes is a definite change, and
anything the provided equivalence criteria do not cover is reported as
undecided rather than guessed at.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any
import json

from attrs import field

from referencing._attrs import frozen
from referencing._core import Registry, Resource
from referencing.exceptions import Unresolvable

if TYPE_CHECKING:
    from referencing.typing import URI

#: The field exists only in the new version.
ADDED = "added"

#: The field exists in both versions but its definition changes semantics.
CHANGED = "changed"

#: The field exists only in the old version.
REMOVED = "removed"

#: The two definitions differ in a way the criteria do not cover.
UNDECIDED = "undecided"

#: Human-readable labels for the conventions this module knows about.
_CONVENTION_LABELS = {
    "type": "类型",
    "enum": "枚举",
    "const": "取值约束",
    "unit": "单位",
    "default:present": "缺值约定",
    "default:value": "缺省值",
    "nullable": "缺值约定",
    "required": "必填约定",
}

#: The conventions the built-in criteria consider covered by default.
_DEFAULT_COVERED = frozenset(_CONVENTION_LABELS)

#: Properties which are fully represented by normalized conventions and
#: therefore must not be compared a second time under their raw names.
_REMAP_RESERVED = frozenset(
    {"properties", "items", "type", "enum", "const", "unit", "default",
     "x-nullable"},
)

#: Documentary properties which never affect a parsing conclusion.
_DEFAULT_IGNORED = frozenset(
    {"description", "title", "examples", "$comment", "readOnly", "writeOnly"},
)


def _escape(segment: str) -> str:
    return segment.replace("~", "~0").replace("/", "~1")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


@frozen
class FieldDefinition:
    """
    A single field reached from a business entry.

    The definition is traceable back to the original entry: ``entry`` is the
    entry reference used to reach the containing document, and ``location``
    is a JSON pointer relative to the node the entry points at.
    """

    entry: URI
    name: str
    location: str
    definition: Mapping[str, Any]
    required: bool
    properties: Mapping[str, Any]


@frozen
class FieldChange:
    """
    A conclusion difference found for one field between the two versions.

    Exactly one of ``old`` and ``new`` is ``None`` for an addition or
    removal, and both are present for a change or an undecided field.
    """

    kind: str
    entry: URI
    field: str
    old: FieldDefinition | None
    new: FieldDefinition | None
    differences: tuple[str, ...] = ()
    reason: str = ""

    @property
    def undecided(self) -> bool:
        """
        Whether this entry should be sent back for a manual decision.
        """
        return self.kind == UNDECIDED

    @property
    def locations(self) -> tuple[str | None, str | None]:
        """
        The traceable old/new JSON pointers for this change.
        """
        old_location = self.old.location if self.old is not None else None
        new_location = self.new.location if self.new is not None else None
        return old_location, new_location


@frozen
class ComparisonReport:
    """
    The outcome of comparing two resource-set versions.

    ``changes`` contains every definite change plus every undecided field;
    ``equivalent`` lists paired definitions (including relocated ones) which
    the criteria judge equivalent and which therefore require no
    revalidation.
    """

    changes: tuple[FieldChange, ...]
    equivalent: tuple[tuple[FieldDefinition, FieldDefinition], ...]

    def __iter__(self):
        return iter(self.changes)

    def __len__(self) -> int:
        return len(self.changes)

    @property
    def undecided(self) -> tuple[FieldChange, ...]:
        """
        The entries which require a manual business decision.
        """
        return tuple(each for each in self.changes if each.undecided)

    @property
    def definite(self) -> tuple[FieldChange, ...]:
        """
        The changes which the criteria confirm as real business changes.
        """
        return tuple(each for each in self.changes if not each.undecided)

    def __str__(self) -> str:
        return "\n".join(
            f"{each.kind}: {each.entry} {each.field}"
            + (f" ({each.reason})" if each.reason else "")
            for each in self.changes
        )


@frozen
class EquivalenceCriteria:
    """
    Business-supplied rules for judging two field definitions equivalent.

    ``property_aliases`` maps dialect-specific property names to the
    canonical conventions understood here, e.g. ``{"x-unit": "unit"}``.

    ``covered_properties`` declares which normalized properties the business
    side is able to sign off on. Differences outside this set produce
    undecided fields rather than definite changes.

    ``ignored_properties`` lists normalized properties which never affect
    the parsing conclusion (documentation and similar metadata).

    ``field_equivalence`` is an optional callable returning ``True`` to
    force equivalence, ``False`` to force a definite change, or ``None`` to
    fall back to the built-in normalized-property comparison.
    """

    property_aliases: Mapping[str, str] = field(factory=dict)
    covered_properties: frozenset[str] = frozenset(_DEFAULT_COVERED)
    ignored_properties: frozenset[str] = frozenset(_DEFAULT_IGNORED)
    field_equivalence: Callable[
        [FieldDefinition, FieldDefinition],
        bool | None,
    ] | None = None

    def is_covered(self, differences: Sequence[str]) -> bool:
        """
        Whether every listed normalized property is business-covered.
        """
        return all(
            each in self.covered_properties for each in differences
        )


def _as_registry(
    resources: Registry[Any] | Mapping[URI, Mapping[str, Any]],
) -> Registry[Any]:
    if isinstance(resources, Registry):
        return resources.crawl()
    return Registry().with_resources(
        (uri, Resource.opaque(contents=contents))
        for uri, contents in resources.items()
    )


def _resolve(registry: Registry[Any], entry: URI):
    try:
        return registry.resolver().lookup(entry).contents
    except Unresolvable:
        return None


def _normalize(
    definition: Mapping[str, Any],
    required: bool,
    aliases: Mapping[str, str],
    ignored: frozenset[str],
) -> dict[str, Any]:
    remapped = {
        aliases.get(key, key): value for key, value in definition.items()
    }

    properties: dict[str, Any] = {}

    type_value = remapped.get("type")
    if type_value is not None:
        types = type_value if isinstance(type_value, list) else [type_value]
        properties["type"] = tuple(sorted(types))
        properties["nullable"] = "null" in types
    else:
        properties["nullable"] = bool(remapped.get("x-nullable", False))

    if "enum" in remapped:
        properties["enum"] = tuple(
            sorted(_canonical(each) for each in remapped["enum"]),
        )
    if "const" in remapped:
        properties["const"] = _canonical(remapped["const"])

    unit = remapped.get("unit")
    if unit is not None:
        properties["unit"] = unit

    properties["default:present"] = "default" in remapped
    if "default" in remapped:
        properties["default:value"] = _canonical(remapped["default"])

    properties["required"] = required

    for key, value in remapped.items():
        if key in properties or key in _REMAP_RESERVED:
            continue
        if key in ignored:
            continue
        properties[key] = _canonical(value)
    return properties


def _required_of(node: Mapping[str, Any]) -> frozenset[str]:
    required = node.get("required")
    if isinstance(required, Sequence) and not isinstance(required, str):
        return frozenset(required)
    return frozenset()


def _walk_fields(
    node: Any,
    entry: URI,
    pointer: str,
    parent_required: frozenset[str],
    aliases: Mapping[str, str],
    ignored: frozenset[str],
):
    if not isinstance(node, Mapping):
        return

    properties = node.get("properties")
    if isinstance(properties, Mapping):
        for name, definition in properties.items():
            location = f"{pointer}/properties/{_escape(name)}"
            if not isinstance(definition, Mapping):
                continue
            required = name in parent_required
            yield FieldDefinition(
                entry=entry,
                name=name,
                location=location,
                definition=definition,
                required=required,
                properties=_normalize(
                    definition,
                    required,
                    aliases,
                    ignored,
                ),
            )
            yield from _walk_fields(
                definition,
                entry,
                location,
                _required_of(definition),
                aliases,
                ignored,
            )

    items = node.get("items")
    if isinstance(items, Mapping):
        yield from _walk_fields(
            items,
            entry,
            f"{pointer}/items",
            _required_of(items),
            aliases,
            ignored,
        )


def _fields_for_entry(
    registry: Registry[Any],
    entry: URI,
    criteria: EquivalenceCriteria,
) -> list[FieldDefinition] | None:
    root = _resolve(registry, entry)
    if root is None:
        return None
    if not isinstance(root, Mapping):
        return []

    fields: list[FieldDefinition] = []
    fragment = entry.split("#", 1)[1] if "#" in entry else ""
    if fragment.startswith("/"):
        name = fragment.rstrip("/").rsplit("/", 1)[-1]
        name = name.replace("~1", "/").replace("~0", "~")
        required = name in _required_of(root)
        fields.append(
            FieldDefinition(
                entry=entry,
                name=name,
                location="",
                definition=root,
                required=required,
                properties=_normalize(
                    root,
                    required,
                    criteria.property_aliases,
                    criteria.ignored_properties,
                ),
            ),
        )
    fields.extend(
        _walk_fields(
            root,
            entry,
            "",
            _required_of(root),
            criteria.property_aliases,
            criteria.ignored_properties,
        ),
    )
    return fields


def _differences(
    old: FieldDefinition,
    new: FieldDefinition,
) -> tuple[str, ...]:
    keys = set(old.properties) | set(new.properties)
    return tuple(
        sorted(
            key
            for key in keys
            if old.properties.get(key) != new.properties.get(key)
        ),
    )


def _reason(differences: Sequence[str]) -> str:
    labels = [_CONVENTION_LABELS.get(each, each) for each in differences]
    return "、".join(labels) + "发生变化"


def _pair(
    old_candidates: Sequence[FieldDefinition],
    new_candidates: Sequence[FieldDefinition],
) -> tuple[
    list[tuple[FieldDefinition, FieldDefinition]],
    list[FieldDefinition],
    list[FieldDefinition],
]:
    """
    Greedily pair same-named fields by smallest definition distance.

    Pairing on name rather than location is what lets a field which moved
    elsewhere in the document match its old definition as long as the two
    definitions remain equivalent.
    """
    old_left = set(range(len(old_candidates)))
    new_left = set(range(len(new_candidates)))
    pairs: list[tuple[FieldDefinition, FieldDefinition]] = []

    scored = sorted(
        (
            len(_differences(old_candidates[i], new_candidates[j])),
            i,
            j,
        )
        for i in old_left
        for j in new_left
    )
    for _, i, j in scored:
        if i in old_left and j in new_left:
            old_left.remove(i)
            new_left.remove(j)
            pairs.append((old_candidates[i], new_candidates[j]))

    leftovers_old = [
        each for index, each in enumerate(old_candidates) if index in old_left
    ]
    leftovers_new = [
        each for index, each in enumerate(new_candidates) if index in new_left
    ]
    return pairs, leftovers_old, leftovers_new


def _classify_pair(
    old: FieldDefinition,
    new: FieldDefinition,
    criteria: EquivalenceCriteria,
) -> tuple[FieldChange | None, tuple[FieldDefinition, FieldDefinition] | None]:
    if criteria.field_equivalence is not None:
        verdict = criteria.field_equivalence(old, new)
        if verdict is True:
            return None, (old, new)
    else:
        verdict = None

    differences = _differences(old, new)
    if not differences:
        return None, (old, new)
    if verdict is False or criteria.is_covered(differences):
        return (
            FieldChange(
                kind=CHANGED,
                entry=old.entry,
                field=old.name,
                old=old,
                new=new,
                differences=differences,
                reason=_reason(differences),
            ),
            None,
        )
    return (
        FieldChange(
            kind=UNDECIDED,
            entry=old.entry,
            field=old.name,
            old=old,
            new=new,
            differences=differences,
            reason="超出等价判据的差异: " + ", ".join(differences),
        ),
        None,
    )


def _compare_entry(
    entry: URI,
    old_registry: Registry[Any],
    new_registry: Registry[Any],
    criteria: EquivalenceCriteria,
    used_fields: frozenset[str] | None,
) -> tuple[
    list[FieldChange],
    list[tuple[FieldDefinition, FieldDefinition]],
]:
    old_fields = _fields_for_entry(old_registry, entry, criteria)
    new_fields = _fields_for_entry(new_registry, entry, criteria)

    if old_fields is None and new_fields is None:
        raise ValueError(  # noqa: TRY003
            f"Entry {entry!r} is unreachable in both versions.",
        )
    if old_fields is None:
        return (
            [
                FieldChange(
                    kind=ADDED,
                    entry=entry,
                    field=each.name,
                    old=None,
                    new=each,
                )
                for each in new_fields or []
                if used_fields is None or each.name in used_fields
            ],
            [],
        )
    if new_fields is None:
        return (
            [
                FieldChange(
                    kind=REMOVED,
                    entry=entry,
                    field=each.name,
                    old=each,
                    new=None,
                )
                for each in old_fields or []
                if used_fields is None or each.name in used_fields
            ],
            [],
        )

    if used_fields is not None:
        old_fields = [each for each in old_fields if each.name in used_fields]
        new_fields = [each for each in new_fields if each.name in used_fields]

    old_by_name: dict[str, list[FieldDefinition]] = {}
    new_by_name: dict[str, list[FieldDefinition]] = {}
    for each in old_fields:
        old_by_name.setdefault(each.name, []).append(each)
    for each in new_fields:
        new_by_name.setdefault(each.name, []).append(each)

    changes: list[FieldChange] = []
    equivalent: list[tuple[FieldDefinition, FieldDefinition]] = []

    for name in sorted(set(old_by_name) | set(new_by_name)):
        old_candidates = old_by_name.get(name, [])
        new_candidates = new_by_name.get(name, [])
        pairs, leftovers_old, leftovers_new = _pair(
            old_candidates,
            new_candidates,
        )
        for old, new in pairs:
            change, equivalence = _classify_pair(old, new, criteria)
            if change is not None:
                changes.append(change)
            if equivalence is not None:
                equivalent.append(equivalence)
        changes.extend(
            FieldChange(
                kind=REMOVED,
                entry=entry,
                field=name,
                old=leftover,
                new=None,
            )
            for leftover in leftovers_old
        )
        changes.extend(
            FieldChange(
                kind=ADDED,
                entry=entry,
                field=name,
                old=None,
                new=leftover,
            )
            for leftover in leftovers_new
        )

    return changes, equivalent


def compare_versions(
    old: Registry[Any] | Mapping[URI, Mapping[str, Any]],
    new: Registry[Any] | Mapping[URI, Mapping[str, Any]],
    entries: Sequence[URI],
    criteria: EquivalenceCriteria | None = None,
    used_fields: Mapping[URI, Sequence[str]] | None = None,
) -> ComparisonReport:
    """
    Compare two resource-set versions behind the given business entries.

    ``old`` and ``new`` are either ready-made `Registry` instances or
    mappings of entry URIs to deserialized documents. ``entries`` is the
    list of entry references the business side actually uses (fragments are
    supported and resolved through the normal referencing machinery).

    ``used_fields`` optionally restricts the comparison per entry to the
    field names the business side actually reads; when omitted, every field
    reachable from the entries is considered.

    Only fields reachable from one of ``entries`` are considered, and the
    returned report separates definite changes from fields left undecided
    because ``criteria`` does not cover their differences.
    """
    if criteria is None:
        criteria = EquivalenceCriteria()

    old_registry = _as_registry(old)
    new_registry = _as_registry(new)

    changes: list[FieldChange] = []
    equivalent: list[tuple[FieldDefinition, FieldDefinition]] = []
    for entry in entries:
        allowed = (
            frozenset(used_fields[entry])
            if used_fields is not None and entry in used_fields
            else None
        )
        entry_changes, entry_equivalent = _compare_entry(
            entry,
            old_registry,
            new_registry,
            criteria,
            allowed,
        )
        changes.extend(entry_changes)
        equivalent.extend(entry_equivalent)

    return ComparisonReport(
        changes=tuple(changes),
        equivalent=tuple(equivalent),
    )
