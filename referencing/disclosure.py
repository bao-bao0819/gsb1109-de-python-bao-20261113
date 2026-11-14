"""
Build shareable, field-scoped explanation packages across trust boundaries.

Two organizations may agree to jointly explain some report fields while each
keeps an internal contract forbidding disclosure of others. This module turns
an internal `Registry` (the *original entry points*) into a second registry
that can safely be handed over:

* each *allowed* field keeps the exact same explanation it had through the
  original entry point, now served from an opaque surrogate resource;
* each *forbidden* field is unreachable, including every alias (JSON pointer,
  anchor or crawled subresource URI) through which it could otherwise be
  reached -- surrogates project only the listed allowed subtrees, so no
  undeclared part of the original contract is carried over;
* surrogate resources use `Specification.OPAQUE`, so they deliberately expose
  no identifiers, anchors or subresources. A surrogate only attests the
  equivalence of the listed fields -- it cannot endorse the whole original
  contract.

When a shared subresource makes the two requirements mutually impossible, the
conflicting entry points and fields are reported instead of silently leaking
data. Fields involved in a conflict are withheld from the surrogate.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from copy import deepcopy
from typing import Generic
from urllib.parse import urldefrag, urljoin

from referencing._attrs import frozen
from referencing._core import Registry, Specification
from referencing.typing import URI, D

#: A JSON pointer segment, decoded into a key or array index.
Segment = int | str


@frozen
class Field:
    """
    A report field, addressed the same way as in the original registry.

    The field is identified by an entry point URI plus a JSON pointer into the
    resource found there. ``aliases`` lists every reference -- the pointer
    itself, plain-name anchors, crawled subresource URIs and embedded pointer
    paths -- which resolves to the same explanation.
    """

    entry: URI
    pointer: str
    explanation: object
    aliases: tuple[URI, ...]

    @property
    def ref(self) -> URI:
        """The canonical reference for this field through its entry point."""
        return self.entry + "#" + self.pointer


@frozen
class Conflict:
    """
    A shared subresource coupling an allowed field to a forbidden one.

    The two fields cannot be served simultaneously: exposing the allowed
    explanation would also expose the forbidden node (or vice versa), even
    through an alias.
    """

    entries: tuple[URI, ...]
    fields: tuple[Field, Field]
    shared_aliases: tuple[URI, ...]


@frozen
class DisclosurePackage(Generic[D]):
    """
    The package that may be handed to the other organization.

    ``registry`` contains one opaque surrogate per original entry point and no
    retrieval fallback, so any reference not explicitly published is
    unresolvable. Inspect ``conflicts`` before sharing: fields involved in a
    conflict are not considered disclosable.
    """

    registry: Registry[D]
    allowed_fields: tuple[Field, ...]
    forbidden_fields: tuple[Field, ...]
    conflicts: tuple[Conflict, ...]

    def is_disclosable(self, field: Field) -> bool:
        """
        Return whether the given allowed field survives every conflict check.
        """
        if field not in self.allowed_fields:
            return False
        return not any(field in conflict.fields for conflict in self.conflicts)

    def field(self, entry: URI, pointer: str) -> Field:
        """
        Return the materialized allowed field at the given entry and pointer.
        """
        for each in self.allowed_fields:
            if each.entry == entry.rstrip("#") and each.pointer == pointer:
                return each
        raise KeyError((entry, pointer))

    def explain(self, ref: URI, base_uri: URI = "") -> object:
        """
        Explain a reference against the surrogate registry.

        Only published entry points and field-level JSON pointers resolve.
        Anchors, subresource URIs and pruned fields raise `Unresolvable`,
        matching the behavior a counterparty will observe.
        """
        uri, fragment = urldefrag(urljoin(base_uri, ref))
        resolver = self.registry.resolver(base_uri=uri)
        if not fragment:
            return resolver.lookup("#").contents
        return resolver.lookup("#" + fragment).contents


def build_disclosure_package(
    registry: Registry[D],
    allowed: Iterable[tuple[URI, str]],
    forbidden: Iterable[tuple[URI, str]] = (),
) -> DisclosurePackage[D]:
    """
    Construct a `DisclosurePackage` from an internal registry.

    ``allowed`` and ``forbidden`` are pairs of ``(entry_point, json_pointer)``.
    Every allowed field must remain explainable through its entry point, while
    every forbidden field -- including any alias leading to it -- must be
    unreachable in the returned package. Fields whose fates are coupled by a
    shared subresource are surfaced as conflicts.
    """
    crawled = registry.crawl()

    allowed_pairs = _dedupe(allowed)
    forbidden_pairs = _dedupe(forbidden)
    _validate_disjoint(allowed_pairs, forbidden_pairs)

    entries = sorted(
        {entry.rstrip("#") for entry, _ in allowed_pairs + forbidden_pairs},
    )

    def materialize(pairs: list[tuple[URI, str]]) -> list[Field]:
        return [
            _field(
                crawled=crawled,
                entries=entries,
                entry=entry,
                pointer=pointer,
            )
            for entry, pointer in pairs
        ]

    allowed_fields = materialize(allowed_pairs)
    forbidden_fields = materialize(forbidden_pairs)

    conflicts = _conflicts(allowed_fields, forbidden_fields)

    retain_by_entry = _retain_plan(allowed_fields, conflicts, entries)
    prune_by_entry = _prune_plan(forbidden_fields, entries)

    surrogate = Registry()
    for entry in entries:
        resource = crawled[entry]
        contents = _project(resource.contents, retain_by_entry.get(entry, ()))
        for pointer in prune_by_entry.get(entry, ()):
            _prune(contents, pointer)
        surrogate = surrogate.with_resource(
            entry,
            Specification.OPAQUE.create_resource(contents=contents),
        )

    return DisclosurePackage(
        registry=surrogate,
        allowed_fields=tuple(allowed_fields),
        forbidden_fields=tuple(forbidden_fields),
        conflicts=tuple(conflicts),
    )


def _dedupe(
    pairs: Iterable[tuple[URI, str]],
) -> list[tuple[URI, str]]:
    seen: dict[tuple[URI, str], None] = {}
    for entry, pointer in pairs:
        if not pointer.startswith("/"):
            raise ValueError(  # noqa: TRY003
                f"Field pointers must start with '/', got {pointer!r}.",
            )
        seen.setdefault((entry.rstrip("#"), pointer), None)
    return list(seen)


def _validate_disjoint(
    allowed: list[tuple[URI, str]],
    forbidden: list[tuple[URI, str]],
) -> None:
    overlap = set(allowed) & set(forbidden)
    if overlap:
        entry, pointer = next(iter(overlap))
        raise ValueError(  # noqa: TRY003
            f"{entry}#{pointer} is listed as both allowed and forbidden.",
        )


def _field(
    crawled: Registry[D],
    entries: Sequence[URI],
    entry: URI,
    pointer: str,
) -> Field:
    entry = entry.rstrip("#")
    explanation = (
        crawled.resolver(base_uri=entry).lookup("#" + pointer).contents
    )
    aliases = _aliases_for(
        crawled=crawled,
        entries=entries,
        explanation=explanation,
    )
    aliases.add(entry + "#" + pointer)
    return Field(
        entry=entry,
        pointer=pointer,
        explanation=explanation,
        aliases=tuple(sorted(aliases)),
    )


def _aliases_for(
    crawled: Registry[D],
    entries: Sequence[URI],
    explanation: object,
) -> set[URI]:
    aliases: set[URI] = set()

    for entry in entries:
        document = crawled[entry].contents
        for segments, value in _walk(document):
            if value == explanation:
                aliases.add(_make_ref(entry, segments))

    for uri in crawled:
        resource = crawled[uri]
        if resource.contents == explanation:
            aliases.add(uri.rstrip("#"))
        for anchor in resource.anchors():
            if anchor.resource.contents == explanation:
                aliases.add(uri.rstrip("#") + "#" + anchor.name)
    return aliases


def _conflicts(
    allowed_fields: Sequence[Field],
    forbidden_fields: Sequence[Field],
) -> list[Conflict]:
    conflicts: list[Conflict] = []
    for allowed in allowed_fields:
        for forbidden in forbidden_fields:
            shared = allowed.aliases and _shared_refs(
                allowed.aliases,
                forbidden.aliases,
            )
            if not shared:
                continue
            conflicts.append(
                Conflict(
                    entries=tuple(
                        sorted({allowed.entry, forbidden.entry}),
                    ),
                    fields=(allowed, forbidden),
                    shared_aliases=tuple(shared),
                ),
            )
    return conflicts


def _shared_refs(left: Sequence[URI], right: Sequence[URI]) -> list[URI]:
    shared = set(left) & set(right)
    right_parts = [_ref_parts(each) for each in right]
    left_parts = [_ref_parts(each) for each in left]
    for left_ref, (left_uri, left_segments) in zip(left, left_parts):
        if left_segments is None:
            continue
        for right_ref, (right_uri, right_segments) in zip(right, right_parts):
            if right_segments is None or left_uri != right_uri:
                continue
            if _is_prefix(left_segments, right_segments) or _is_prefix(
                right_segments,
                left_segments,
            ):
                shared.update({left_ref, right_ref})
    return sorted(shared)


def _ref_parts(ref: URI) -> tuple[URI, tuple[Segment, ...] | None]:
    uri, fragment = urldefrag(ref)
    if not fragment:
        return uri.rstrip("#"), ()
    if fragment.startswith("/"):
        return uri.rstrip("#"), _split(fragment)
    return uri.rstrip("#"), None


def _is_prefix(
    shorter: Sequence[Segment],
    longer: Sequence[Segment],
) -> bool:
    return tuple(longer[: len(shorter)]) == tuple(shorter)


def _retain_plan(
    allowed_fields: Sequence[Field],
    conflicts: Sequence[Conflict],
    entries: Sequence[URI],
) -> dict[URI, list[tuple[Segment, ...]]]:
    conflicted = {
        (field.entry, field.pointer)
        for conflict in conflicts
        for field in conflict.fields
    }
    plan: dict[URI, list[tuple[Segment, ...]]] = {
        entry: [] for entry in entries
    }
    for field in allowed_fields:
        if (field.entry, field.pointer) in conflicted:
            continue
        segments = _split(field.pointer)
        if segments not in plan[field.entry]:
            plan[field.entry].append(segments)
    for retained in plan.values():
        retained.sort(key=len)
    return plan


def _prune_plan(
    forbidden_fields: Sequence[Field],
    entries: Sequence[URI],
) -> dict[URI, list[str]]:
    plan: dict[URI, list[str]] = {entry: [] for entry in entries}
    for field in forbidden_fields:
        for alias in field.aliases:
            uri, segments = _ref_parts(alias)
            if uri not in plan or not segments:
                continue
            pointer = _make_pointer(segments)
            if pointer not in plan[uri]:
                plan[uri].append(pointer)
    for pointers in plan.values():
        pointers.sort(key=lambda pointer: pointer.count("/"), reverse=True)
    return plan


def _project(
    contents: object,
    retained: Sequence[tuple[Segment, ...]],
) -> object:
    """
    Rebuild a document containing only the retained pointer subtrees.

    Containers along each retained path are recreated, so nothing outside the
    explicitly allowed fields -- including values only reachable via aliases
    inside an unlisted subtree -- survives into the surrogate.
    """
    if not retained:
        return _empty_like(contents)

    root: object = _new_container(contents)
    for segments in retained:
        original = contents
        node = root
        for depth, segment in enumerate(segments):
            original = _step(original, segment)
            last = depth == len(segments) - 1
            node = _ensure_child(node, segment, original, last)
    return root


def _ensure_child(
    node: object,
    segment: Segment,
    original_child: object,
    last: bool,
) -> object:
    value = (
        deepcopy(original_child) if last else _new_container(original_child)
    )
    if isinstance(node, Mapping):
        existing = node.get(segment)
        if existing is None:
            node[segment] = value
            return value
        return existing

    index = int(segment)
    while len(node) <= index:
        node.append(None)
    if node[index] is None:
        node[index] = value
        return value
    return node[index]


def _new_container(value: object) -> object:
    if isinstance(value, Mapping):
        return {}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return []
    return deepcopy(value)


def _empty_like(contents: object) -> object:
    if isinstance(contents, Mapping):
        return {}
    if isinstance(contents, Sequence) and not isinstance(
        contents,
        (str, bytes),
    ):
        return []
    return contents


def _walk(
    contents: object,
    segments: tuple[Segment, ...] = (),
) -> Iterator[tuple[tuple[Segment, ...], object]]:
    yield segments, contents
    if isinstance(contents, Mapping):
        for key, value in contents.items():
            yield from _walk(value, (*segments, key))
    elif isinstance(contents, Sequence) and not isinstance(
        contents,
        (str, bytes),
    ):
        for index, value in enumerate(contents):
            yield from _walk(value, (*segments, index))


def _split(pointer: str) -> tuple[Segment, ...]:
    segments: list[Segment] = []
    for raw in pointer[1:].split("/"):
        segment = raw.replace("~1", "/").replace("~0", "~")
        segments.append(segment)
    return tuple(segments)


def _make_pointer(segments: Sequence[Segment]) -> str:
    return "".join(
        "/" + str(segment).replace("~", "~0").replace("/", "~1")
        for segment in segments
    )


def _make_ref(entry: URI, segments: Sequence[Segment]) -> URI:
    if not segments:
        return entry
    return entry + "#" + _make_pointer(segments)


def _prune(contents: object, pointer: str) -> None:
    segments = _split(pointer)
    node = contents
    try:
        for segment in segments[:-1]:
            node = _step(node, segment)
        del node[_index(node, segments[-1])]  # type: ignore[union-attr]
    except LookupError:
        # The node was already withheld by projection (e.g. an alias whose
        # container is not part of any retained field).
        pass


def _step(node: object, segment: Segment) -> object:
    return node[_index(node, segment)]  # type: ignore[index]


def _index(node: object, segment: Segment) -> Segment:
    if isinstance(node, Sequence) and not isinstance(node, (str, bytes)):
        return int(segment)
    return segment
