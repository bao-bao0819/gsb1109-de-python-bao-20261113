import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audit import build_case, verify


@pytest.fixture()
def analyzed(tmp_path):
    db_path = tmp_path / "repairs.db"
    build_case.build(str(db_path))
    timeline, locations, results = verify.analyze(str(db_path))
    by_form = {item["form_id"]: item for item in results}
    return db_path, timeline, locations, by_form


def test_pk_change_is_confirmed_within_one_lifecycle(analyzed):
    _, timeline, _, by_form = analyzed
    assert by_form["F1"]["verdict"] == "确认"
    rowid1 = timeline[1]
    assert len(rowid1) == 1
    versions = [snap[1] for snap in rowid1[0]]
    assert versions == [1, 2, 3]
    assert rowid1[0][1][2]["biz_no"] == "A002"


def test_rowid_reuse_splits_segments_and_resets_version(analyzed):
    _, timeline, _, _ = analyzed
    segments = timeline[5]
    assert len(segments) == 2
    assert segments[0][-1][0] == "DELETE"
    assert segments[0][-1][2]["biz_no"] == "E001"
    assert segments[1][0][0] == "INSERT"
    assert segments[1][0][1] == 1
    assert segments[1][0][2]["biz_no"] == "E002"


def test_rowid_reuse_succession_stays_undetermined(analyzed):
    _, _, _, by_form = analyzed
    item = by_form["F2"]
    assert item["verdict"] == "未定"
    assert any("断裂" in gap for gap in item["gaps"])
    assert any("单方主张" in gap for gap in item["gaps"])


def test_swap_moves_non_key_attributes_with_physical_row(analyzed):
    _, timeline, locations, _ = analyzed
    assert timeline[3][-1][-1][2]["biz_no"] == "D001"
    assert timeline[3][-1][-1][2]["model"] == "叉车"
    assert timeline[4][-1][-1][2]["biz_no"] == "C001"
    assert timeline[4][-1][-1][2]["model"] == "铲车"
    assert {rowid for rowid, _, _, _ in locations["C001"]} == {3, 4}
    assert {rowid for rowid, _, _, _ in locations["D001"]} == {3, 4}


def test_dual_claim_leaves_both_forms_undetermined(analyzed):
    _, _, _, by_form = analyzed
    for form_id in ("F3", "F4"):
        item = by_form[form_id]
        assert item["verdict"] == "未定"
        assert any("抢认" in gap for gap in item["gaps"])
        assert any("从未共享物理行" in line for line in item["evidence"])


def test_undetermined_predecessor_is_still_fully_traceable(analyzed):
    _, timeline, locations, _ = analyzed
    b001_points = locations["B001"]
    assert b001_points == [(2, 1, 1, "INSERT")]
    assert timeline[2][0][-1][0] != "DELETE"


def test_transient_key_is_flagged_not_as_registered_device(analyzed):
    _, _, locations, by_form = analyzed
    tmp_points = locations["TMP9"]
    assert all(kind == "UPDATE" for _, _, _, kind in tmp_points)
    assert "TMP9" not in by_form
