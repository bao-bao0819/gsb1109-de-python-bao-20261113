"""End-to-end tests for the cadastral area-transfer ledger."""

import json

import pytest

from cadastral_transfer import compute_area_ledger, load_parcels
from cadastral_transfer.report import new_rows, old_rows


def feature(parcel_id, rings):
    return {
        "type": "Feature",
        "id": parcel_id,
        "properties": {"id": parcel_id},
        "geometry": {"type": "Polygon", "coordinates": rings},
    }


def box(x0, y0, x1, y1):
    return [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]]


def write_batch(directory, features):
    directory.mkdir()
    (directory / "parcels.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": features}),
        encoding="utf-8",
    )


@pytest.fixture()
def ledger(tmp_path):
    old_dir = tmp_path / "old"
    new_dir = tmp_path / "new"
    write_batch(
        old_dir,
        [
            feature("O1", box(0, 0, 10, 10)),  # split into N1+N2
            feature("O2", box(20, 0, 30, 10)),  # merged into N3
            feature("O3", box(30, 0, 40, 10)),  # merged into N3
            feature("O4", box(50, 0, 60, 10)),  # half retreat
            feature("O5", box(90, 0, 100, 10)),  # disputes O6
            feature("O6", box(95, 0, 105, 10)),  # disputes O5
            feature("O7", box(110, 0, 120, 10)),  # only touches N7
            feature(
                "O9",
                [
                    [[160, 0], [170, 0], [170, 10], [160, 10], [160, 0]],
                    [[162, 2], [164, 2], [164, 4], [162, 4], [162, 2]],
                ],
            ),
        ],
    )
    write_batch(
        new_dir,
        [
            feature("N1", box(0, 0, 5, 10)),
            feature("N2", box(5, 0, 10, 10)),
            feature("N3", box(20, 0, 40, 10)),
            feature("N4", box(50, 0, 55, 10)),
            feature("N5", box(70, 0, 80, 10)),  # pure fill
            feature("N6", box(90, 0, 105, 10)),  # covers disputed zone
            feature("N7", box(120, 0, 130, 10)),  # only touches O7
            feature("O1", box(140, 0, 150, 10)),  # recycled identifier
            feature(
                "N9",
                [
                    [[160, 0], [170, 0], [170, 10], [160, 10], [160, 0]],
                    [[162, 2], [164, 2], [164, 4], [162, 4], [162, 2]],
                ],
            ),
        ],
    )
    return compute_area_ledger(load_parcels(old_dir), load_parcels(new_dir))


def test_split_one_to_many(ledger):
    assert ledger.transfer[("O1", "N1")] == pytest.approx(50.0)
    assert ledger.transfer[("O1", "N2")] == pytest.approx(50.0)


def test_merge_many_to_one(ledger):
    assert ledger.transfer[("O2", "N3")] == pytest.approx(100.0)
    assert ledger.transfer[("O3", "N3")] == pytest.approx(100.0)


def test_touching_edges_are_not_transfers(ledger):
    assert ledger.transfer.get(("O7", "N7"), 0.0) == pytest.approx(0.0)
    assert ledger.retreat["O7"] == pytest.approx(100.0)
    assert ledger.fill["N7"] == pytest.approx(100.0)


def test_retreat_and_fill(ledger):
    assert ledger.transfer[("O4", "N4")] == pytest.approx(50.0)
    assert ledger.retreat["O4"] == pytest.approx(50.0)
    assert ledger.fill["N5"] == pytest.approx(100.0)


def test_old_batch_dispute_is_separated(ledger):
    assert ledger.dispute_old["O5"] == pytest.approx(50.0)
    assert ledger.dispute_old["O6"] == pytest.approx(50.0)
    assert ledger.transfer[("O5", "N6")] == pytest.approx(50.0)
    assert ledger.transfer[("O6", "N6")] == pytest.approx(50.0)
    assert ledger.old_dispute_on_new["N6"] == pytest.approx(50.0)
    assert ledger.old_pairs[("O5", "O6")] == pytest.approx(50.0)


def test_recycled_identifier_gets_no_identity(ledger):
    # The new parcel named "O1" sits on empty land: pure fill, no transfer.
    assert ledger.transfer.get(("O1", "O1"), 0.0) == pytest.approx(0.0)
    assert ledger.fill["O1"] == pytest.approx(100.0)


def test_polygon_with_hole(ledger):
    assert ledger.old_area["O9"] == pytest.approx(96.0)
    assert ledger.transfer[("O9", "N9")] == pytest.approx(96.0)


def test_old_side_identity(ledger):
    for row in old_rows(ledger, eps=1e-9):
        assert row["check"] == pytest.approx(0.0, abs=1e-9)


def test_new_side_identity(ledger):
    for row in new_rows(ledger, eps=1e-9):
        assert row["check"] == pytest.approx(0.0, abs=1e-9)


def test_wkt_loading(tmp_path):
    old_dir = tmp_path / "old"
    new_dir = tmp_path / "new"
    old_dir.mkdir()
    new_dir.mkdir()
    (old_dir / "A.wkt").write_text(
        "POLYGON ((0 0, 4 0, 4 4, 0 4, 0 0))", encoding="utf-8"
    )
    (new_dir / "B.wkt").write_text(
        "POLYGON ((2 0, 6 0, 6 4, 2 4, 2 0))", encoding="utf-8"
    )
    result = compute_area_ledger(load_parcels(old_dir), load_parcels(new_dir))
    assert result.transfer[("A", "B")] == pytest.approx(8.0)
    assert result.retreat["A"] == pytest.approx(8.0)
    assert result.fill["B"] == pytest.approx(8.0)
