"""
Migrate a nested JSON resource tree into a flat, offline package.

The migration preserves each resource's *business identity* -- its retrieval
URI, internal IDs and anchors -- rather than its directory location.  Only
filesystem-relative links which pointed at a sibling control document need a
new target in the flat layout; all URI-addressed contracts are copied verbatim.

Entries are named after their entire original path (including the directory
which identifies the dialect), not merely their final filename.  Consequently
two entries which happen to be aliases remain two distinct resources, and a
third entry with the same filename but different contents cannot be merged
with them.
"""

from __future__ import annotations

from argparse import ArgumentParser
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import hashlib
import json
import re

URL_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")

__all__ = [
    "AliasGroup",
    "Entry",
    "Migration",
    "Verification",
    "build",
    "migrate",
    "verify",
]

SOURCE_LINK = '"$schema": "../../test-schema.json"'
FLAT_LINK = '"$schema": "test-schema.json"'
SOURCE_LINK_TARGET = "../../test-schema.json"
FLAT_LINK_TARGET = "test-schema.json"
_SOURCE_PATTERN_TEXT = r"\.\./\.\./test-schema.json$"
_FLAT_PATTERN_TEXT = r"test-schema\.json$"

_CONTROL = "test-schema.json"
_SPECIFICATIONS = "specifications.json"
_MARKER = ".migrated"
_ALIASES = "ALIASES.md"
_MANIFEST = "migration.json"
_REPORT = "verification.json"
_README = "README.md"
_README_TEXT = """\
# Offline resource package

This directory is the flattened, offline copy of the JSON resources under
`suite/tests`, plus the suite control document and dialect index.

## Layout

- `*.json` -- one flat file per source resource.
- `test-schema.json` -- the suite's control schema, reachable without a
  directory climb.
- `specifications.json` -- the unchanged dialect index.
- `migration.json` -- machine-readable old/new entry correspondence,
  rewritten links, preserved external identities and alias groups.
- `ALIASES.md` -- human-readable groups of entries with identical bytes.
- `verification.json` -- reference-equivalence results.

## Naming

A flat filename is `STEM--DRAFT.json`, e.g.
`multiple-lookup--07.json` for
`tests/json-schema-draft-07/multiple-lookup.json`.

The draft is part of the name because resources with different dialects are
different business definitions.  In particular, a resource whose content
merely matches another resource's bytes stays a separate entry.  This
prevents two aliases from being folded together with a third resource which
shares their final filename but not their contents.

## References

Only file-level relative control links are rewritten:

```text
"$schema": "../../test-schema.json"  ->  "$schema": "test-schema.json"
```

The control document's `$schema` pattern is adjusted the same way.  Every URI
identity (`$id` / `id`, `$ref`, `$dynamicRef`, registry keys, `urn:`, `tag:`
and absolute `http:` contracts) is copied verbatim.  Relative and
fragment-only identities keep their meaning because their containing resource
is copied byte-for-byte apart from the control link.

## Verification

`verification.json` records:

- `assertions`: old and new lookups compared, including every chained `then`.
- `expectations`: migrated lookups checked against the suite's declared
  `target` or `error`.
- `links_checked`: local control links which resolve to the flat control
  document.

RFC 3986 normalization cases are compared old-to-new but excluded from the
declared-target check, mirroring the suite's own `xfail` while URL
canonicalization is unsupported by the current API.

## Reproduce

From the repository root:

```bash
.venv/bin/python -m referencing.flatten suite offline-package --force
```

The migration refuses to overwrite a non-empty directory unless `--force`
is given or the directory carries the `.migrated` marker.
"""


@dataclass(frozen=True)
class Entry:
    """One source resource and its location in the flat package."""

    source: str
    target: str
    sha256: str
    aliases: tuple[str, ...] = ()

    @property
    def flat(self) -> str:
        """The target's filename, which is unique within the package."""
        return Path(self.target).name


@dataclass(frozen=True)
class AliasGroup:
    """A set of distinct entries which happen to share identical bytes."""

    sha256: str
    members: tuple[str, ...]

    @property
    def canonical(self) -> str:
        """The first member, used only for display."""
        return self.members[0]


@dataclass
class Migration:
    """The package produced by `migrate`."""

    source: Path
    destination: Path
    entries: tuple[Entry, ...]
    aliases: tuple[AliasGroup, ...]
    external_identities: tuple[str, ...]
    problems: tuple[str, ...] = field(default_factory=tuple)

    def manifest(self) -> dict[str, Any]:
        """Return the old/new entry correspondence as serializable data."""
        return {
            "source_root": self.source.as_posix(),
            "destination": self.destination.as_posix(),
            "control_document": {
                "source": (self.source / _CONTROL).as_posix(),
                "target": (self.destination / _CONTROL).as_posix(),
            },
            "rewrites": [
                {
                    "from": SOURCE_LINK_TARGET,
                    "to": FLAT_LINK_TARGET,
                    "reason": (
                        "file-relative control link now shares one flat "
                        "directory"
                    ),
                    "occurrences": len(self.entries),
                },
            ],
            "external_identities_preserved": list(self.external_identities),
            "entries": [
                {
                    "source": entry.source,
                    "target": entry.target,
                    "sha256": entry.sha256,
                    "aliases": list(entry.aliases),
                }
                for entry in self.entries
            ],
            "alias_groups": [
                {
                    "sha256": group.sha256,
                    "members": list(group.members),
                }
                for group in self.aliases
            ],
            "problems": list(self.problems),
        }


@dataclass
class Verification:
    """The result of comparing pre- and post-migration lookups."""

    assertions: int = 0
    expectations: int = 0
    failures: list[str] = field(default_factory=list)
    links_checked: int = 0

    @property
    def ok(self) -> bool:
        """Whether every reference still reaches its business definition."""
        return not self.failures

    def report(self) -> dict[str, Any]:
        """Return the verification result as serializable data."""
        return {
            "assertions": self.assertions,
            "expectations": self.expectations,
            "links_checked": self.links_checked,
            "failures": self.failures,
            "verified": self.ok,
        }


def _digest(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _dump(contents: Any) -> str:
    return json.dumps(contents, indent=2, ensure_ascii=False) + "\n"


def _flat_name(source: str) -> str:
    path = Path(source)
    if len(path.parts) == 3 and path.parts[0] == "tests":
        directory = path.parts[1].removeprefix("json-schema-draft-")
    else:
        directory = path.parent.as_posix().replace("/", "--")
    return f"{path.stem}--{directory}.json"


def _resource_paths(source: Path) -> list[str]:
    return sorted(
        path.relative_to(source).as_posix()
        for path in (source / "tests").glob("*/**/*.json")
        if path.name != _SPECIFICATIONS
    )


def _normalise(text: str) -> str:
    return text.replace(SOURCE_LINK, FLAT_LINK)


def _normalise_control(text: str) -> str:
    return text.replace(
        json.dumps(_SOURCE_PATTERN_TEXT),
        json.dumps(_FLAT_PATTERN_TEXT),
    )


def _external_identities(paths: list[Path]) -> tuple[str, ...]:
    """
    Collect absolute (scheme-bearing) identities which the package retains.

    Relative IDs (``"nested.json"``), fragments (``"#foo"``) and pointers are
    deliberately excluded: their meaning is base-dependent and is represented
    by the verbatim contents of the resource which declares them.
    """
    identities: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if (
                    key in {"$id", "id", "$schema", "$ref", "$dynamicRef"}
                    and isinstance(child, str)
                    and _is_absolute(child)
                ):
                    identities.add(child)
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    for path in paths:
        if path.name == _SPECIFICATIONS:
            continue
        contents = _load(path)
        if isinstance(contents, dict):
            for uri in contents.get("registry", {}):
                if _is_absolute(uri):
                    identities.add(uri)
        walk(contents)
    return tuple(sorted(identities))


def _is_absolute(uri: str) -> bool:
    return bool(URL_SCHEME.match(uri))


def _prepare(destination: Path, *, force: bool) -> None:
    if destination.exists():
        if not destination.is_dir():
            message = f"{destination} exists and is not a directory"
            raise NotADirectoryError(message)
        if destination.joinpath(_MARKER).exists() or force:
            for child in destination.iterdir():
                if child.is_file():
                    child.unlink()
        elif any(destination.iterdir()):
            message = f"{destination} is not an empty migration destination"
            raise FileExistsError(message)
    destination.mkdir(parents=True, exist_ok=True)


def _alias_groups(entries: list[Entry]) -> tuple[AliasGroup, ...]:
    groups: dict[str, list[str]] = defaultdict(list)
    for entry in entries:
        groups[entry.sha256].append(entry.target)
    return tuple(
        sorted(
            (
                AliasGroup(sha256=digest, members=tuple(sorted(members)))
                for digest, members in groups.items()
                if len(members) > 1
            ),
            key=lambda group: group.members,
        ),
    )


def _mark_aliases(
    entries: list[Entry],
    aliases: tuple[AliasGroup, ...],
) -> list[Entry]:
    groups = {
        member: group
        for group in aliases
        for member in group.members
    }
    marked: list[Entry] = []
    for entry in entries:
        group = groups.get(entry.target)
        siblings = (
            tuple(each for each in group.members if each != entry.target)
            if group is not None
            else ()
        )
        marked.append(
            Entry(
                source=entry.source,
                target=entry.target,
                sha256=entry.sha256,
                aliases=siblings,
            ),
        )
    return marked


def migrate(
    source: Path,
    destination: Path,
    *,
    force: bool = False,
) -> Migration:
    """
    Copy the nested resource tree at ``source/tests`` into ``destination``.

    Parameters:
        source: directory containing ``tests/`` plus the control documents
            ``test-schema.json`` and ``specifications.json``.
        destination: directory to populate with flat resources.
        force: replace existing files in a non-empty destination.
    """
    _prepare(destination, force=force)

    paths = _resource_paths(source)
    problems: list[str] = []

    targets: dict[str, str] = {
        source_path: _flat_name(source_path)
        for source_path in paths
    }
    seen: dict[str, str] = {}
    for source_path, target in targets.items():
        if target in seen:
            problems.append(
                f"collision: {seen[target]} and {source_path} -> {target}",
            )
        seen[target] = source_path

    entries: list[Entry] = []
    for source_path, target in targets.items():
        original = source / source_path
        text = original.read_text(encoding="utf-8")
        rewritten = _normalise(text)
        output = destination / target
        output.write_text(rewritten, encoding="utf-8")

        expected_bytes = rewritten.encode("utf-8")
        if output.read_bytes() != expected_bytes:
            problems.append(f"non-deterministic copy: {source_path}")

        entries.append(
            Entry(
                source=source_path,
                target=Path(target).as_posix(),
                sha256=_digest(expected_bytes),
            ),
        )

    control = (source / _CONTROL).read_text(encoding="utf-8")
    (destination / _CONTROL).write_text(
        _normalise_control(control),
        encoding="utf-8",
    )

    specifications = source / "tests" / _SPECIFICATIONS
    (destination / _SPECIFICATIONS).write_text(
        specifications.read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    aliases = _alias_groups(entries)
    entries = _mark_aliases(entries, aliases)
    entries.sort(key=lambda entry: entry.target)

    migration = Migration(
        source=source,
        destination=destination,
        entries=tuple(entries),
        aliases=aliases,
        external_identities=_external_identities(
            [source / path for path in paths] + [source / _CONTROL],
        ),
        problems=tuple(problems),
    )

    (destination / _MARKER).write_text(
        "generated by referencing.flatten\n",
        encoding="utf-8",
    )
    (destination / _MANIFEST).write_text(
        _dump(migration.manifest()),
        encoding="utf-8",
    )
    (destination / _ALIASES).write_text(
        _render_aliases(migration),
        encoding="utf-8",
    )
    (destination / _README).write_text(_README_TEXT, encoding="utf-8")
    return migration


def _render_aliases(migration: Migration) -> str:
    lines = [
        "# Alias groups",
        "",
        "These entries have identical bytes, so they were aliases in the",
        "source tree.  They remain separate resources: each has its own",
        "draft-qualified name and its own old/new mapping.",
        "",
    ]
    if not migration.aliases:
        lines.append("No content-identical alias groups were found.")
    for index, group in enumerate(migration.aliases, start=1):
        lines.append(f"## Group {index}")
        lines.append("")
        lines.append(f"sha256: `{group.sha256}`")
        lines.append("")
        for member in group.members:
            lines.append(f"- `{member}`")
        lines.append("")
    return "\n".join(lines)


def _digest_of(contents: Any) -> str:
    return _digest(json.dumps(contents, sort_keys=True, ensure_ascii=False))


def _check_contract(
    verification: Verification,
    entry: Entry,
    index: int,
    registry: Any,
    test: dict[str, Any],
) -> None:
    """
    Check the migrated registry against the suite's declared expectations.

    This is deliberately independent of the old registry: it compares the
    business contract for each reference with what the migrated package
    actually reaches, so identical old/new errors cannot hide a regression.
    """
    from referencing.exceptions import Unresolvable

    resolver = registry.resolver(base_uri=test.get("base_uri") or "")
    current: dict[str, Any] | None = test
    while current is not None:
        expect_error = bool(current.get("error"))
        try:
            resolved = resolver.lookup(current["ref"])
        except Unresolvable:
            if not expect_error:
                verification.failures.append(
                    f"{entry.source}[{index}]: expected a target for "
                    f"{current['ref']!r} but lookup failed",
                )
        else:
            if expect_error:
                verification.failures.append(
                    f"{entry.source}[{index}]: expected an error for "
                    f"{current['ref']!r} but it resolved",
                )
            else:
                verification.expectations += 1
                if resolved.contents != current["target"]:
                    verification.failures.append(
                        f"{entry.source}[{index}]: {current['ref']!r} reached "
                        f"{_digest_of(resolved.contents)} instead of target",
                    )
            resolver = resolved.resolver
        current = current.get("then")


def _outcomes(registry: Any, test: dict[str, Any]) -> list[str]:
    from referencing.exceptions import Unresolvable

    outcomes: list[str] = []
    resolver = registry.resolver(base_uri=test.get("base_uri") or "")
    current: dict[str, Any] | None = test
    while current is not None:
        try:
            resolved = resolver.lookup(current["ref"])
        except Unresolvable:
            outcome = "unresolvable"
        else:
            outcome = _digest_of(resolved.contents)
            resolver = resolved.resolver
        outcomes.append(
            f"error:{outcome}" if current.get("error") else outcome,
        )
        current = current.get("then")
    return outcomes


def verify(migration: Migration) -> Verification:
    """
    Resolve every pre-migration reference in both packages and compare.

    The comparison uses the same registry construction and dialect selection
    as the referencing test suite.  Each chained ``then`` lookup is followed
    through the resolver returned by the previous lookup, so fragment
    self-references and subresource base changes are exercised too.

    RFC 3986 normalization assertions are checked for old/new equivalence but
    not against their declared targets, mirroring the suite's own ``xfail``
    for the API's currently missing URL canonicalization.
    """
    import referencing
    import referencing.jsonschema

    verification = Verification()
    source = migration.source
    destination = migration.destination
    dialects = _load(source / "tests" / _SPECIFICATIONS)

    for entry in migration.entries:
        old = _load(source / entry.source)
        new = _load(destination / entry.flat)
        draft = Path(entry.source).parts[1]
        specification = referencing.jsonschema.specification_with(
            dialects[draft],
        )

        old_registry = referencing.Registry().with_resources(
            (uri, specification.create_resource(contents))
            for uri, contents in old.get("registry", {}).items()
        )
        new_registry = referencing.Registry().with_resources(
            (uri, specification.create_resource(contents))
            for uri, contents in new.get("registry", {}).items()
        )

        old_text = (source / entry.source).read_text(encoding="utf-8")
        new_text = (destination / entry.flat).read_text(encoding="utf-8")
        if _normalise(old_text) != new_text:
            verification.failures.append(
                f"{entry.source}: content changed beyond the control link",
            )

        old_tests = old.get("tests", [])
        new_tests = new.get("tests", [])
        if len(old_tests) != len(new_tests):
            verification.failures.append(f"{entry.source}: test count changed")
            continue

        for index, (old_test, new_test) in enumerate(
            zip(old_tests, new_tests, strict=True),
        ):
            verification.assertions += _walk_verify(
                verification,
                entry,
                index,
                old_test,
                new_test,
                old_registry,
                new_registry,
            )
            if "normalization" not in Path(entry.source).stem:
                _check_contract(
                    verification,
                    entry,
                    index,
                    new_registry,
                    new_test,
                )

    schema = _load(destination / _CONTROL)
    control = schema.get("properties", {}).get("$schema", {})
    for entry in migration.entries:
        contents = _load(destination / entry.flat)
        link = contents.get("$schema")
        if link is not None:
            verification.links_checked += 1
            if not (destination / link).is_file():
                verification.failures.append(
                    f"{entry.flat}: $schema link does not exist: {link}",
                )
            if control.get("pattern") != _FLAT_PATTERN_TEXT:
                verification.failures.append(
                    f"{_CONTROL}: $schema pattern was not migrated",
                )
    (destination / _REPORT).write_text(
        _dump(verification.report()),
        encoding="utf-8",
    )
    return verification


def build(
    source: Path,
    destination: Path,
    *,
    force: bool = False,
) -> tuple[Migration, Verification]:
    """
    Create a complete package and its verification report.

    This is the supported entry point: it always leaves ``verification.json``
    in the destination, including when an existing package is rebuilt.
    """
    migration = migrate(source, destination, force=force)
    verification = verify(migration)
    return migration, verification


def _walk_verify(
    verification: Verification,
    entry: Entry,
    index: int,
    old_test: dict[str, Any],
    new_test: dict[str, Any],
    old_registry: Any,
    new_registry: Any,
) -> int:
    count = 1
    for key in ("ref", "base_uri", "target", "error"):
        if old_test.get(key) != new_test.get(key):
            verification.failures.append(
                f"{entry.source}[{index}]: {key} changed",
            )
    old_outcomes = _outcomes(old_registry, old_test)
    new_outcomes = _outcomes(new_registry, new_test)
    if old_outcomes != new_outcomes:
        verification.failures.append(
            f"{entry.source}[{index}]: lookup changed "
            f"{old_outcomes!r} -> {new_outcomes!r}",
        )

    old_then = old_test.get("then")
    new_then = new_test.get("then")
    if (old_then is None) != (new_then is None):
        verification.failures.append(
            f"{entry.source}[{index}]: chained lookup changed shape",
        )
    elif old_then is not None:
        count += _walk_verify(
            verification,
            entry,
            index,
            old_then,
            new_then,
            old_registry,
            new_registry,
        )
    return count


def main(argv: list[str] | None = None) -> int:
    """Run the migration command line interface."""
    parser = ArgumentParser(prog="python -m referencing.flatten")
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument(
        "--force",
        action="store_true",
        help="replace files in a non-empty destination",
    )
    args = parser.parse_args(argv)

    migration, verification = build(
        args.source,
        args.destination,
        force=args.force,
    )

    print(f"resources: {len(migration.entries)}")
    print(f"aliases: {len(migration.aliases)}")
    print(f"assertions: {verification.assertions}")
    print(f"links: {verification.links_checked}")
    for problem in migration.problems:
        print(f"problem: {problem}")
    for failure in verification.failures:
        print(f"failure: {failure}")
    print(f"verified: {verification.ok and not migration.problems}")
    return 0 if verification.ok and not migration.problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
