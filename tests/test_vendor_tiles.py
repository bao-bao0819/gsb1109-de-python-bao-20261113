import json
import subprocess
import sys

import pytest

from vendor_tiles import (
    Convention,
    analyze,
    child_corner,
    convert_tile,
)


def tms_y(z, y):
    return 2**z - 1 - y


def test_convert_tile_tms_flip():
    assert convert_tile((9, 106, 193), Convention(flip_y=True)) == (9, 106, 318)


def test_convert_tile_zoom_mislabeled():
    assert convert_tile((8, 300, 100), Convention(dz=1)) == (9, 300, 100)
    assert convert_tile((8, 300, 100), Convention()) is None
    assert convert_tile((9, 10, 20), Convention(dz=-1)) == (8, 10, 20)
    assert convert_tile((9, 300, 20), Convention(dz=-1)) is None


def test_child_corner():
    assert child_corner((9, 106, 318), (10, 212, 636)) == "NW"
    assert child_corner((9, 106, 318), (10, 213, 637)) == "SE"
    assert child_corner((9, 106, 318), (10, 213, 386)) is None


def tms_directory():
    # Ground truth XYZ: parent (9, 106, 318) with NW child (10, 212, 636)
    # and SE child (10, 213, 637); the vendor stored TMS y values.
    return {
        "tiles": [
            {"id": "p", "z": 9, "x": 106, "y": tms_y(9, 318)},
            {"id": "p_alias", "z": 9, "x": 106, "y": tms_y(9, 318)},
            {"id": "c_nw", "z": 10, "x": 212, "y": tms_y(10, 636)},
            {"id": "c_se", "z": 10, "x": 213, "y": tms_y(10, 637)},
        ],
        "relations": [
            {"parent": "p", "child": "c_nw", "corner": "NW"},
            {"parent": "p", "child": "c_se", "corner": "SE"},
            {"parent": "p", "child": "p_alias"},  # same extent, same level
        ],
        "landmarks": [
            {"tile": "p", "name": "lighthouse", "corner": "SE"},
            {"tile": "c_se", "name": "lighthouse", "corner": "NW"},
            {"tile": "p", "name": "chapel", "corner": "NW"},
            {"tile": "c_nw", "name": "chapel", "corner": "NW"},
        ],
    }


def test_tms_directory_with_corner_landmarks():
    report = analyze(tms_directory())
    assert report["convention"] == {"name": "tms", "flip_y": True, "dz": 0}
    assert report["landmarks_consistent"] is True
    assert report["contradiction"] is None
    assert len(report["verified_relations"]) == 3
    assert report["unverified_relations"] == []
    converted = {t["id"]: (t["z"], t["x"], t["y"]) for t in report["tiles"]}
    assert converted["p"] == (9, 106, 318)
    assert converted["c_nw"] == (10, 212, 636)
    assert converted["c_se"] == (10, 213, 637)


def test_incomplete_child_set_still_verifies():
    data = tms_directory()
    # Drop the SE child: a corner-missing child set is still evidence.
    data["tiles"] = [t for t in data["tiles"] if t["id"] != "c_se"]
    data["relations"] = [r for r in data["relations"] if r["child"] != "c_se"]
    data["landmarks"] = [
        lm for lm in data["landmarks"] if not (lm["name"] == "lighthouse")
    ]
    report = analyze(data)
    assert report["convention"]["flip_y"] is True
    assert report["unverified_relations"] == []


def test_zoom_off_by_one_detected():
    data = {
        "tiles": [
            {"id": "a", "z": 8, "x": 300, "y": 100},  # x >= 2**8: label too low
            {"id": "b", "z": 9, "x": 601, "y": 200},
        ],
        "relations": [{"parent": "a", "child": "b"}],
    }
    report = analyze(data)
    assert report["convention"]["dz"] == 1
    assert report["convention"]["flip_y"] is False
    assert report["unverified_relations"] == []
    converted = {t["id"]: (t["z"], t["x"], t["y"]) for t in report["tiles"]}
    assert converted["a"] == (9, 300, 100)
    assert converted["b"] == (10, 601, 200)
    # The flipped variant fits the same evidence; it stays an alternative.
    assert "tms+1z" in report["alternatives"]


def test_level_flipped_separately_reports_earliest_pair():
    data = {
        "tiles": [
            {"id": "p9", "z": 9, "x": 50, "y": 60},
            {"id": "p10", "z": 10, "x": 101, "y": 121},  # SE child of p9
            # True SE child of p10 is (11, 203, 243) but level 11 was
            # flipped on its own before delivery.
            {"id": "c11", "z": 11, "x": 203, "y": tms_y(11, 243)},
        ],
        "relations": [
            {"parent": "p9", "child": "p10", "corner": "SE"},
            {"parent": "p10", "child": "c11", "corner": "SE"},
        ],
    }
    report = analyze(data)
    assert report["convention"] == {"name": "xyz", "flip_y": False, "dz": 0}
    assert [r["child"] for r in report["verified_relations"]] == ["p10"]
    assert [r["child"] for r in report["unverified_relations"]] == ["c11"]
    contradiction = report["contradiction"]
    assert contradiction["parent_level"] == 10
    assert contradiction["child_level"] == 11
    assert contradiction["fixed_by_flipping_level"] == 11


def test_inconsistent_landmarks_are_flagged():
    data = {
        "tiles": [
            {"id": "a", "z": 5, "x": 1, "y": 1},
            {"id": "b", "z": 5, "x": 30, "y": 30},
        ],
        "landmarks": [
            {"tile": "a", "name": "tower", "corner": "SE"},
            {"tile": "b", "name": "tower", "corner": "NW"},
        ],
    }
    report = analyze(data)
    assert report["landmarks_consistent"] is False


def test_cli_roundtrip(tmp_path):
    src = tmp_path / "directory.json"
    src.write_text(json.dumps(tms_directory()))
    proc = subprocess.run(
        [sys.executable, "vendor_tiles.py", str(src)],
        capture_output=True,
        text=True,
        check=True,
    )
    report = json.loads(proc.stdout)
    assert report["convention"]["name"] == "tms"


def test_unknown_tile_reference_rejected():
    with pytest.raises(ValueError):
        analyze({"tiles": [], "relations": [{"parent": "a", "child": "b"}]})
