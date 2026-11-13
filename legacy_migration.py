"""Verifiable migration from a legacy WKT ledger to geometry-object delivery.

The legacy ledger is a text file with one record per line::

    <record_id><TAB><WKT>

Blank lines and lines starting with ``#`` are ignored.

Each record is migrated into an ordered list of members.  Every member is
either delivered as a pygeoif geometry *object* (the new interface) or, when
the target cannot express it (e.g. ``POINT EMPTY`` or curved geometries),
*carried* with its original WKT fragment so the record can round-trip.

The target file is JSONL, one record per line.  Verification matches members
by a content fingerprint (geometry type, dimensionality, emptiness and
coordinates) -- never by array position -- so a reordered target file still
verifies, and members that cannot be matched are reported separately.

CLI::

    python3 legacy_migration.py migrate ledger.txt target.jsonl
    python3 legacy_migration.py verify  ledger.txt target.jsonl
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Any

from pygeoif import GeometryCollection
from pygeoif import LinearRing
from pygeoif import LineString
from pygeoif import MultiLineString
from pygeoif import MultiPoint
from pygeoif import MultiPolygon
from pygeoif import Polygon
from pygeoif.exceptions import WKTParserError
from pygeoif.factories import from_wkt
from pygeoif.factories import shape
from pygeoif.factories import split_wkt_components
from pygeoif.geometry import Geometry

SRID_PREFIX = re.compile(r"^SRID=\d+;", flags=re.IGNORECASE)
EMPTY_WKT = re.compile(
    r"^(?P<type>[A-Z]+)\s*(?P<marker>Z|M|ZM)?\s+EMPTY$",
    flags=re.IGNORECASE,
)
COLLECTION_WKT = re.compile(
    r"^GEOMETRYCOLLECTION\s*(?:Z|M|ZM)?\s*\((?P<body>.*)\)$",
    flags=re.IGNORECASE | re.DOTALL,
)

# Empty members pygeoif can construct.  ``POINT EMPTY`` is absent on purpose:
# ``pygeoif.Point`` requires coordinates, so empty points are carried.
EMPTY_CONSTRUCTORS: dict[str, Any] = {
    "LINESTRING": lambda: LineString([]),
    "LINEARRING": lambda: LinearRing([]),
    "POLYGON": Polygon,
    "MULTIPOINT": lambda: MultiPoint([]),
    "MULTILINESTRING": lambda: MultiLineString([]),
    "MULTIPOLYGON": lambda: MultiPolygon([]),
    "GEOMETRYCOLLECTION": lambda: GeometryCollection([]),
}

STATUS_OBJECT = "object"
STATUS_CARRIED = "carried"


@dataclass
class Member:
    """One migrated sub-object of a ledger record."""

    seq: int
    status: str
    geom_type: str
    fingerprint: str
    label: str
    geometry: Geometry | GeometryCollection | None = None
    has_z: bool | None = None
    is_empty: bool = False
    declared_marker: str | None = None
    wkt_fragment: str | None = None
    reason: str | None = None


@dataclass
class MigratedRecord:
    """A ledger record delivered as geometry objects plus carried fragments."""

    record_id: str
    is_collection: bool
    members: list[Member] = field(default_factory=list)

    @property
    def geometries(self) -> list[Geometry | GeometryCollection]:
        """The members the new interface can deliver as objects, in order."""
        return [m.geometry for m in self.members if m.geometry is not None]


def _normalize_wkt(wkt: str) -> str:
    return " ".join(wkt.upper().split())


def _object_fingerprint(geometry: Geometry | GeometryCollection) -> str:
    if geometry.is_empty:
        return f"EMPTY:{geometry.geom_type}"
    mapping = geometry.__geo_interface__
    canonical = json.dumps(mapping, sort_keys=True, separators=(",", ":"))
    return f"GEOM:{canonical}"


def _carried_fingerprint(fragment: str) -> str:
    return f"CARRIED:{_normalize_wkt(fragment)}"


def _object_label(geometry: Geometry | GeometryCollection) -> str:
    return geometry.wkt if not geometry.is_empty else f"{geometry.geom_type} EMPTY"


def _migrate_empty_member(
    seq: int,
    fragment: str,
    match: re.Match[str],
) -> Member:
    geom_type = match.group("type").upper()
    marker = (match.group("marker") or "").upper() or None
    if marker in {"M", "ZM"}:
        return Member(
            seq=seq,
            status=STATUS_CARRIED,
            geom_type=geom_type,
            fingerprint=_carried_fingerprint(fragment),
            label=_normalize_wkt(fragment),
            wkt_fragment=fragment,
            declared_marker=marker,
            reason="measured (M) geometries are not expressible",
        )
    constructor = EMPTY_CONSTRUCTORS.get(geom_type)
    if constructor is None:
        return Member(
            seq=seq,
            status=STATUS_CARRIED,
            geom_type=geom_type,
            fingerprint=_carried_fingerprint(fragment),
            label=_normalize_wkt(fragment),
            wkt_fragment=fragment,
            declared_marker=marker,
            reason=f"empty {geom_type} is not expressible as a pygeoif object",
        )
    geometry = constructor()
    fingerprint = f"EMPTY:{geometry.geom_type}"
    if marker == "Z":
        # pygeoif empties carry no dimensionality flag; keep it in the
        # fingerprint and metadata so the Z marker survives the round-trip.
        fingerprint = f"{fingerprint}:Z"
    return Member(
        seq=seq,
        status=STATUS_OBJECT,
        geom_type=geometry.geom_type,
        fingerprint=fingerprint,
        label=f"{geom_type} {marker + ' ' if marker else ''}EMPTY",
        geometry=geometry,
        has_z=geometry.has_z,
        is_empty=True,
        declared_marker=marker,
    )


def migrate_member(seq: int, fragment: str) -> Member:
    """Migrate one WKT fragment to a geometry object or a carried fragment."""
    fragment = fragment.strip()
    empty_match = EMPTY_WKT.match(SRID_PREFIX.sub("", fragment).strip())
    if empty_match:
        return _migrate_empty_member(seq, fragment, empty_match)
    try:
        geometry = from_wkt(fragment)
    except (WKTParserError, TypeError, KeyError) as exc:
        return Member(
            seq=seq,
            status=STATUS_CARRIED,
            geom_type=fragment.split("(", 1)[0].split()[0].upper(),
            fingerprint=_carried_fingerprint(fragment),
            label=_normalize_wkt(fragment),
            wkt_fragment=fragment,
            reason=f"not expressible as a pygeoif object: {exc}",
        )
    if geometry is None:  # pragma: no cover - defensive
        msg = f"parser returned no geometry for {fragment}"
        raise WKTParserError(msg)
    return Member(
        seq=seq,
        status=STATUS_OBJECT,
        geom_type=geometry.geom_type,
        fingerprint=_object_fingerprint(geometry),
        label=_object_label(geometry),
        geometry=geometry,
        has_z=geometry.has_z,
        is_empty=geometry.is_empty,
    )


def migrate_record(record_id: str, wkt: str) -> MigratedRecord:
    """Migrate one ledger record, preserving member order and identity."""
    wkt = " ".join(wkt.split())
    collection_match = COLLECTION_WKT.match(SRID_PREFIX.sub("", wkt).strip())
    if collection_match:
        fragments = split_wkt_components(collection_match.group("body"))
        is_collection = True
    else:
        fragments = [wkt]
        is_collection = False
    members = [migrate_member(seq, frag) for seq, frag in enumerate(fragments)]
    return MigratedRecord(
        record_id=record_id,
        is_collection=is_collection,
        members=members,
    )


def read_ledger(path: str | Path) -> list[tuple[str, str]]:
    """Read the legacy ledger into ``(record_id, wkt)`` pairs."""
    records = []
    for lineno, line in enumerate(Path(path).read_text().splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        record_id, sep, wkt = line.partition("\t")
        if not sep or not record_id.strip() or not wkt.strip():
            msg = f"ledger line {lineno}: expected '<id><TAB><WKT>'"
            raise ValueError(msg)
        records.append((record_id.strip(), wkt.strip()))
    return records


def migrate_ledger(path: str | Path) -> list[MigratedRecord]:
    """Migrate every record of a legacy ledger file."""
    return [migrate_record(record_id, wkt) for record_id, wkt in read_ledger(path)]


def _member_to_entry(member: Member) -> dict[str, Any]:
    entry: dict[str, Any] = {
        # seq is informational only; verification never matches on it.
        "seq": member.seq,
        "status": member.status,
        "geom_type": member.geom_type,
        "fingerprint": member.fingerprint,
        "label": member.label,
    }
    if member.status == STATUS_OBJECT:
        geometry = member.geometry
        assert geometry is not None  # noqa: S101
        entry["has_z"] = geometry.has_z
        entry["is_empty"] = geometry.is_empty
        entry["geometry"] = (
            None if geometry.is_empty else geometry.__geo_interface__
        )
        if member.declared_marker:
            entry["declared_marker"] = member.declared_marker
    else:
        entry["wkt"] = member.wkt_fragment
        entry["reason"] = member.reason
        if member.declared_marker:
            entry["declared_marker"] = member.declared_marker
    return entry


def record_to_entry(record: MigratedRecord) -> dict[str, Any]:
    """Serialize a migrated record for the target JSONL file."""
    return {
        "id": record.record_id,
        "is_collection": record.is_collection,
        "member_count": len(record.members),
        "members": [_member_to_entry(m) for m in record.members],
    }


def write_target(records: list[MigratedRecord], path: str | Path) -> None:
    """Write migrated records to the target JSONL file."""
    with Path(path).open("w") as fh:
        for record in records:
            fh.write(json.dumps(record_to_entry(record)) + "\n")


def read_target(path: str | Path) -> list[dict[str, Any]]:
    """Read a target JSONL file back into record entries."""
    return [
        json.loads(line)
        for line in Path(path).read_text().splitlines()
        if line.strip()
    ]


def member_entry_to_wkt(entry: dict[str, Any]) -> str:
    """Reconstruct the WKT of one target member (round-trip)."""
    if entry["status"] == STATUS_CARRIED:
        return entry["wkt"]
    if entry.get("is_empty"):
        marker = entry.get("declared_marker")
        return f"{entry['geom_type'].upper()} {marker + ' ' if marker else ''}EMPTY"
    geometry = shape(entry["geometry"])
    return geometry.wkt


def record_entry_to_wkt(entry: dict[str, Any]) -> str:
    """Reconstruct the full WKT of a target record (round-trip)."""
    members = [member_entry_to_wkt(m) for m in entry["members"]]
    if entry["is_collection"]:
        return f"GEOMETRYCOLLECTION ({', '.join(members)})"
    return members[0]


@dataclass
class RecordDiff:
    """Reconciliation result for a single record id."""

    record_id: str
    matched: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    unexpected: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.missing and not self.unexpected


@dataclass
class VerificationReport:
    """Result of verifying a target file against the source ledger."""

    record_diffs: list[RecordDiff] = field(default_factory=list)
    missing_records: list[str] = field(default_factory=list)
    unexpected_records: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return (
            not self.missing_records
            and not self.unexpected_records
            and all(diff.ok for diff in self.record_diffs)
        )


def _labels_by_fingerprint(
    members: list[Member] | list[dict[str, Any]],
) -> dict[str, str]:
    labels = {}
    for member in members:
        if isinstance(member, Member):
            labels.setdefault(member.fingerprint, member.label)
        else:
            labels.setdefault(member["fingerprint"], member.get("label", "?"))
    return labels


def verify_migration(
    source_path: str | Path,
    target_path: str | Path,
) -> VerificationReport:
    """Verify a target file against the source ledger.

    Members are matched by content fingerprint as a multiset -- never by
    array position -- so a reordered target file still verifies, and any
    member that cannot be matched (e.g. an elevation that landed on the
    wrong point) is listed separately.
    """
    expected_records = {r.record_id: r for r in migrate_ledger(source_path)}
    actual_records = {r["id"]: r for r in read_target(target_path)}

    report = VerificationReport()
    report.missing_records = sorted(set(expected_records) - set(actual_records))
    report.unexpected_records = sorted(set(actual_records) - set(expected_records))

    for record_id in sorted(set(expected_records) & set(actual_records)):
        expected = expected_records[record_id]
        actual = actual_records[record_id]
        labels = _labels_by_fingerprint(expected.members)
        labels.update(_labels_by_fingerprint(actual["members"]))

        remaining: dict[str, int] = {}
        for member in actual["members"]:
            remaining[member["fingerprint"]] = (
                remaining.get(member["fingerprint"], 0) + 1
            )

        diff = RecordDiff(record_id=record_id)
        for member in expected.members:
            fingerprint = member.fingerprint
            if remaining.get(fingerprint, 0) > 0:
                remaining[fingerprint] -= 1
                diff.matched.append(labels[fingerprint])
            else:
                diff.missing.append(labels[fingerprint])
        for fingerprint, count in remaining.items():
            diff.unexpected.extend([labels[fingerprint]] * count)
        report.record_diffs.append(diff)
    return report


def _format_report(report: VerificationReport) -> str:
    lines = []
    for record_id in report.missing_records:
        lines.append(f"record {record_id!r}: missing from target")
    for record_id in report.unexpected_records:
        lines.append(f"record {record_id!r}: not present in source ledger")
    for diff in report.record_diffs:
        if diff.ok:
            lines.append(
                f"record {diff.record_id!r}: OK ({len(diff.matched)} members matched)",
            )
            continue
        lines.append(f"record {diff.record_id!r}: MISMATCH")
        for label in diff.matched:
            lines.append(f"  matched:    {label}")
        for label in diff.missing:
            lines.append(f"  missing:    {label}")
        for label in diff.unexpected:
            lines.append(f"  unexpected: {label}")
    lines.append("verification: " + ("OK" if report.ok else "FAILED"))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Command line entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("migrate", "verify"):
        sub = subparsers.add_parser(command)
        sub.add_argument("ledger", help="legacy ledger file (id<TAB>WKT per line)")
        sub.add_argument("target", help="target JSONL file")
    args = parser.parse_args(argv)

    if args.command == "migrate":
        records = migrate_ledger(args.ledger)
        write_target(records, args.target)
        carried = sum(
            1 for r in records for m in r.members if m.status == STATUS_CARRIED
        )
        print(  # noqa: T201
            f"migrated {len(records)} records "
            f"({sum(len(r.members) for r in records)} members, "
            f"{carried} carried as original fragments) -> {args.target}",
        )
        return 0

    report = verify_migration(args.ledger, args.target)
    print(_format_report(report))  # noqa: T201
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
