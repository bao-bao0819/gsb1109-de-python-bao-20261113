"""
Pre-ingestion auditing for packages of vendor JSON contracts.

A vendor package bundles multiple JSON Schema documents in one directory.
Different documents often reuse the same anchor name (e.g. an ``amount``
plain-name anchor).  Whether such reuse is a problem depends on the
*resolution scope* the anchor is parsed in:

* two anchors with the same name under different resource base URIs never
  interact and must not be rejected,
* two *different* anchor definitions end up under the same base URI when
  the package is crawled (including embedded subresources reached from
  more than one entry document), the anchors collide -- a registry would
  silently keep only one of them.

The auditing here mirrors `Registry.crawl`, so the reported scope is the
scope the references are actually resolved in.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, TypeVar
from urllib.parse import urljoin
import json
import sys

import attrs

from referencing import Resource, Specification
from referencing.exceptions import CannotDetermineSpecification
from referencing.jsonschema import DRAFT202012, UnknownDialect

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

#: A deserialized JSON document (or fragment thereof).
Document = Any

#: A canonical resource base URI; ``None`` for resources without an ID.
Scope = str | None


def _index_pointers(contents: Document) -> dict[int, str]:
    """
    Map every nested object in a document to its JSON Pointer.

    Subresources produced by a `Specification` are plain nested objects,
    so their location within the entry document can be recovered by object
    identity.
    """
    pointers: dict[int, str] = {}

    def walk(node: Document, pointer: str) -> None:
        if isinstance(node, dict):
            pointers[id(node)] = pointer
            for key, value in node.items():
                walk(value, f"{pointer}/{_escape(str(key))}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{pointer}/{index}")

    walk(contents, "")
    return pointers


def _escape(segment: str) -> str:
    return segment.replace("~", "~0").replace("/", "~1")


@attrs.frozen
class Occurrence:
    """
    An anchor or resource definition encountered while crawling an entry.
    """

    entry: str
    pointer: str
    definition: Document


@attrs.frozen
class AnchorConflict:
    """
    Differing definitions for one anchor name within one resource scope.
    """

    scope: Scope
    name: str
    occurrences: tuple[Occurrence, ...]

    @property
    def entries(self) -> tuple[str, ...]:
        """
        Entries from which each conflicting definition is reachable.
        """
        return tuple(sorted({each.entry for each in self.occurrences}))

    @property
    def definitions(self) -> tuple[Document, ...]:
        """
        Distinct conflicting definitions, in first-seen order.
        """
        seen: list[Document] = []
        for each in self.occurrences:
            if not any(each.definition == existing for existing in seen):
                seen.append(each.definition)
        return tuple(seen)


@attrs.frozen
class ResourceConflict:
    """
    Differing documents claiming the same canonical resource URI.
    """

    uri: str
    occurrences: tuple[Occurrence, ...]

    @property
    def entries(self) -> tuple[str, ...]:
        """
        Entries which claim the conflicting resource URI.
        """
        return tuple(sorted({each.entry for each in self.occurrences}))

    @property
    def definitions(self) -> tuple[Document, ...]:
        """
        Distinct conflicting documents, in first-seen order.
        """
        seen: list[Document] = []
        for each in self.occurrences:
            if not any(each.definition == existing for existing in seen):
                seen.append(each.definition)
        return tuple(seen)


@attrs.frozen
class LoadError:
    """
    An entry document which could not be parsed or interpreted.
    """

    entry: str
    message: str


@attrs.frozen
class AuditReport:
    """
    The result of auditing a package of contracts.
    """

    anchor_conflicts: tuple[AnchorConflict, ...]
    resource_conflicts: tuple[ResourceConflict, ...]
    load_errors: tuple[LoadError, ...]

    def ok(self) -> bool:
        """
        Whether the package can be ingested without any finding.
        """
        return not (
            self.anchor_conflicts
            or self.resource_conflicts
            or self.load_errors
        )

    def entries(self) -> tuple[str, ...]:
        """
        All entry documents mentioned by at least one finding.
        """
        entries = {
            occurrence.entry
            for conflict in self.anchor_conflicts
            for occurrence in conflict.occurrences
        }
        entries.update(
            occurrence.entry
            for conflict in self.resource_conflicts
            for occurrence in conflict.occurrences
        )
        entries.update(error.entry for error in self.load_errors)
        return tuple(sorted(entries))

    def for_entry(self, entry: str) -> AuditReport:
        """
        Return the subset of findings reachable from the given entry.
        """
        anchor_conflicts = tuple(
            conflict
            for conflict in self.anchor_conflicts
            if entry in conflict.entries
        )
        resource_conflicts = tuple(
            conflict
            for conflict in self.resource_conflicts
            if entry in conflict.entries
        )
        load_errors = tuple(
            error for error in self.load_errors if error.entry == entry
        )
        return AuditReport(
            anchor_conflicts=anchor_conflicts,
            resource_conflicts=resource_conflicts,
            load_errors=load_errors,
        )


def check_entry(
    path: str | Path,
    default_specification: Specification[Document] = DRAFT202012,
) -> tuple[
    dict[tuple[Scope, str], list[Occurrence]],
    dict[str, list[Occurrence]],
    LoadError | None,
]:
    """
    Crawl one entry document, collecting scoped anchor and resource claims.
    """
    path = Path(path)
    try:
        contents = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        return {}, {}, LoadError(entry=str(path), message=str(error))
    try:
        root = Resource.from_contents(
            contents,
            default_specification=default_specification,
        )
    except (CannotDetermineSpecification, UnknownDialect) as error:
        return {}, {}, LoadError(entry=str(path), message=str(error))

    pointers = _index_pointers(contents)
    anchors: dict[tuple[Scope, str], list[Occurrence]] = {}
    resources: dict[str, list[Occurrence]] = {}
    entry = str(path)

    def visit(resource: Resource[Any], base: Scope) -> None:
        resource_id = resource.id()
        if resource_id is not None:
            base = urljoin(base or "", resource_id)
            pointer = pointers.get(id(resource.contents), "")
            resources.setdefault(base, []).append(
                Occurrence(
                    entry=entry,
                    pointer=pointer,
                    definition=resource.contents,
                ),
            )

        for anchor in resource.anchors():
            pointer = pointers.get(id(anchor.resource.contents), "")
            anchors.setdefault((base, anchor.name), []).append(
                Occurrence(
                    entry=entry,
                    pointer=pointer,
                    definition=anchor.resource.contents,
                ),
            )

        for subresource in resource.subresources():
            visit(subresource, base)

    visit(root, root.id())
    return anchors, resources, None


def check_package(
    directory: str | Path,
    default_specification: Specification[Document] = DRAFT202012,
) -> AuditReport:
    """
    Audit every ``*.json`` contract in the given (possibly nested) directory.

    Anchors are grouped by the scope they are actually parsed in -- the
    resource base URI produced by crawling -- so harmless reuse of an anchor
    name under different bases does not produce a finding, while colliding
    definitions (including ones embedded under several entry documents) do.
    """
    directory = Path(directory)
    if not directory.is_dir():
        message = f"{directory} is not a directory"
        raise NotADirectoryError(message)

    scoped_claims: dict[
        tuple[Scope, str],
        list[Occurrence],
    ] = {}
    local_claims: dict[
        tuple[str, str],
        list[Occurrence],
    ] = {}
    resource_claims: dict[str, list[Occurrence]] = {}
    load_errors: list[LoadError] = []

    for path in sorted(directory.rglob("*.json")):
        entry = str(path)
        anchors, resources, error = check_entry(
            path,
            default_specification=default_specification,
        )
        if error is not None:
            load_errors.append(error)
            continue
        for (scope, name), occurrences in anchors.items():
            if scope is None:
                # Without an ID the document is registered under its own
                # external URI, so its anchors never share a scope with
                # another entry.
                local_claims.setdefault((entry, name), []).extend(
                    occurrences,
                )
            else:
                scoped_claims.setdefault((scope, name), []).extend(
                    occurrences,
                )
        for uri, occurrences in resources.items():
            resource_claims.setdefault(uri, []).extend(occurrences)

    anchor_candidates = [
        AnchorConflict(
            scope=scope,
            name=name,
            occurrences=tuple(
                sorted(occurrences, key=_occurrence_key),
            ),
        )
        for (scope, name), occurrences in scoped_claims.items()
    ]
    anchor_candidates.extend(
        AnchorConflict(
            scope=None,
            name=name,
            occurrences=tuple(
                sorted(occurrences, key=_occurrence_key),
            ),
        )
        for (_, name), occurrences in local_claims.items()
    )
    anchor_conflicts = _conflicts(
        anchor_candidates,
        key=lambda conflict: (conflict.scope or "", conflict.name),
    )
    resource_conflicts = _conflicts(
        (
            ResourceConflict(
                uri=uri,
                occurrences=tuple(
                    sorted(occurrences, key=_occurrence_key),
                ),
            )
            for uri, occurrences in resource_claims.items()
        ),
        key=lambda conflict: conflict.uri,
    )

    return AuditReport(
        anchor_conflicts=tuple(anchor_conflicts),
        resource_conflicts=tuple(resource_conflicts),
        load_errors=tuple(
            sorted(load_errors, key=lambda error: error.entry),
        ),
    )


def _occurrence_key(occurrence: Occurrence) -> tuple[str, str]:
    return occurrence.entry, occurrence.pointer


class _HasOccurrences(Protocol):
    occurrences: tuple[Occurrence, ...]


_ConflictT = TypeVar("_ConflictT", bound=_HasOccurrences)


def _conflicts(
    candidates: Iterable[_ConflictT],
    key: Callable[[_ConflictT], Any],
) -> list[_ConflictT]:
    return sorted(
        (
            each
            for each in candidates
            if len(
                {
                    _definition_key(item.definition)
                    for item in each.occurrences
                },
            )
            > 1
        ),
        key=key,
    )


def _definition_key(definition: Document) -> str:
    return json.dumps(definition, sort_keys=True, default=str)


def format_report(report: AuditReport) -> str:
    """
    Render an `AuditReport` as a human-readable message.
    """
    if report.ok():
        return (
            "No conflicts found: all anchors resolve within distinct scopes."
        )

    lines: list[str] = []
    for conflict in report.anchor_conflicts:
        scope = conflict.scope or "<no base URI>"
        lines.append(
            f"Anchor {conflict.name!r} has conflicting definitions in "
            f"scope {scope}:",
        )
        lines.append(f"  entries: {', '.join(conflict.entries)}")
        lines.extend(
            f"  - {each.entry}#{each.pointer or '/'}"
            for each in conflict.occurrences
        )
    for conflict in report.resource_conflicts:
        lines.append(
            f"Resource URI {conflict.uri!r} is claimed with differing "
            "contents:",
        )
        lines.append(f"  entries: {', '.join(conflict.entries)}")
        lines.extend(
            f"  - {each.entry}#{each.pointer or '/'}"
            for each in conflict.occurrences
        )
    lines.extend(
        f"Could not load {error.entry}: {error.message}"
        for error in report.load_errors
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """
    Command-line entry point returning a process exit status.
    """
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        sys.stderr.write(
            "usage: python -m referencing.audit DIRECTORY\n",
        )
        return 2
    report = check_package(argv[0])
    sys.stdout.write(format_report(report) + "\n")
    return 0 if report.ok() else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
