"""Tests for the legacy WKT ledger migration."""

import json

import pytest
from pygeoif import GeometryCollection
from pygeoif import LineString
from pygeoif import Point

from legacy_migration import STATUS_CARRIED
from legacy_migration import STATUS_OBJECT
from legacy_migration import main
from legacy_migration import migrate_ledger
from legacy_migration import migrate_record
from legacy_migration import read_target
from legacy_migration import record_entry_to_wkt
from legacy_migration import verify_migration
from legacy_migration import write_target

LEDGER = """\
# legacy survey ledger
rec-1\tGEOMETRYCOLLECTION (POINT Z (1 2 3), LINESTRING (0 0, 1 1), POINT EMPTY)
rec-2\tPOINT Z (7 8 9)
rec-3\tLINESTRING (0 0, 1 1, 2 2)
rec-4\tGEOMETRYCOLLECTION (POINT Z (1 2 3), CIRCULARSTRING (0 0, 1 1, 2 0), LINESTRING Z EMPTY)
rec-5\tGEOMETRYCOLLECTION EMPTY
"""


@pytest.fixture()
def ledger_path(tmp_path):
    path = tmp_path / "ledger.txt"
    path.write_text(LEDGER)
    return path


@pytest.fixture()
def target_path(tmp_path, ledger_path):
    path = tmp_path / "target.jsonl"
    write_target(migrate_ledger(ledger_path), path)
    return path


def test_mixed_collection_preserves_members():
    record = migrate_record(
        "rec-1",
        "GEOMETRYCOLLECTION (POINT Z (1 2 3), LINESTRING (0 0, 1 1), POINT EMPTY)",
    )
    assert record.is_collection
    assert len(record.members) == 3

    point, line, empty = record.members
    assert point.status == STATUS_OBJECT
    assert isinstance(point.geometry, Point)
    assert point.geometry.has_z is True
    assert point.geometry.z == 3

    assert line.status == STATUS_OBJECT
    assert isinstance(line.geometry, LineString)
    assert line.geometry.has_z is False

    # pygeoif cannot represent an empty point: carried with original fragment.
    assert empty.status == STATUS_CARRIED
    assert empty.wkt_fragment == "POINT EMPTY"


def test_member_order_and_identity_preserved(ledger_path):
    records = {r.record_id: r for r in migrate_ledger(ledger_path)}
    rec_4 = records["rec-4"]
    assert [m.geom_type for m in rec_4.members] == [
        "Point",
        "CIRCULARSTRING",
        "LineString",
    ]
    carried = rec_4.members[1]
    assert carried.status == STATUS_CARRIED
    assert carried.wkt_fragment == "CIRCULARSTRING (0 0, 1 1, 2 0)"

    empty_line = rec_4.members[2]
    assert empty_line.status == STATUS_OBJECT
    assert empty_line.is_empty
    assert empty_line.declared_marker == "Z"


def test_empty_types_preserved(ledger_path):
    records = {r.record_id: r for r in migrate_ledger(ledger_path)}
    rec_5 = records["rec-5"]
    assert not rec_5.is_collection
    member = rec_5.members[0]
    assert member.status == STATUS_OBJECT
    assert isinstance(member.geometry, GeometryCollection)
    assert member.geometry.is_empty


def test_new_interface_delivers_geometry_objects(ledger_path):
    records = {r.record_id: r for r in migrate_ledger(ledger_path)}
    geometries = records["rec-1"].geometries
    assert len(geometries) == 2
    assert all(isinstance(g, (Point, LineString)) for g in geometries)


def test_verify_clean_target(ledger_path, target_path):
    report = verify_migration(ledger_path, target_path)
    assert report.ok
    assert not report.missing_records
    assert not report.unexpected_records


def test_verify_reordered_target(ledger_path, target_path):
    """A reordered target file must still verify: matching is by identity."""
    entries = read_target(target_path)
    for entry in entries:
        entry["members"] = list(reversed(entry["members"]))
    entries.reverse()
    target_path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")

    report = verify_migration(ledger_path, target_path)
    assert report.ok


def test_verify_detects_elevation_on_wrong_point(ledger_path, target_path):
    """Elevation patched onto the wrong point is flagged, not silently matched."""
    entries = {e["id"]: e for e in read_target(target_path)}
    rec_1 = entries["rec-1"]
    point_entry = rec_1["members"][0]
    assert point_entry["geom_type"] == "Point"
    # tamper: drop the elevation, i.e. the 3D point became a 2D point
    point_entry["geometry"] = {"type": "Point", "coordinates": (1, 2)}
    point_entry["fingerprint"] = "tampered"
    target_path.write_text("\n".join(json.dumps(e) for e in entries.values()) + "\n")

    report = verify_migration(ledger_path, target_path)
    assert not report.ok
    diff = next(d for d in report.record_diffs if d.record_id == "rec-1")
    assert diff.missing == ["POINT Z (1 2 3)"]
    assert len(diff.unexpected) == 1
    # the other members are still matched by identity
    assert "LINESTRING (0 0, 1 1)" in diff.matched
    assert "POINT EMPTY" in diff.matched


def test_verify_lists_unknown_records_separately(ledger_path, target_path):
    entries = read_target(target_path)
    entries.append({"id": "rec-99", "is_collection": False, "members": []})
    entries = [e for e in entries if e["id"] != "rec-2"]
    target_path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")

    report = verify_migration(ledger_path, target_path)
    assert not report.ok
    assert report.missing_records == ["rec-2"]
    assert report.unexpected_records == ["rec-99"]


def test_round_trip_wkt(ledger_path, target_path):
    entries = {e["id"]: e for e in read_target(target_path)}
    assert record_entry_to_wkt(entries["rec-1"]) == (
        "GEOMETRYCOLLECTION (POINT Z (1 2 3), LINESTRING (0 0, 1 1), POINT EMPTY)"
    )
    assert record_entry_to_wkt(entries["rec-2"]) == "POINT Z (7 8 9)"
    assert record_entry_to_wkt(entries["rec-4"]) == (
        "GEOMETRYCOLLECTION (POINT Z (1 2 3), "
        "CIRCULARSTRING (0 0, 1 1, 2 0), LINESTRING Z EMPTY)"
    )
    assert record_entry_to_wkt(entries["rec-5"]) == "GEOMETRYCOLLECTION EMPTY"


def test_cli_migrate_and_verify(ledger_path, target_path, capsys):
    assert main(["verify", str(ledger_path), str(target_path)]) == 0
    out = capsys.readouterr().out
    assert "verification: OK" in out

    new_target = target_path.with_suffix(".jsonl2")
    assert main(["migrate", str(ledger_path), str(new_target)]) == 0
    assert "carried" in capsys.readouterr().out
    assert verify_migration(ledger_path, new_target).ok


def test_cli_verify_fails_on_tampered(ledger_path, target_path, capsys):
    entries = read_target(target_path)
    entries[1]["members"][0]["fingerprint"] = "tampered"
    target_path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    assert main(["verify", str(ledger_path), str(target_path)]) == 1
    assert "FAILED" in capsys.readouterr().out
