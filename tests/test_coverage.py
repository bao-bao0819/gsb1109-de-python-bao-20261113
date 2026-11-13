"""Tests of spherical-area coverage accounting."""

import math

import pytest

import mercantile
from mercantile.coverage import (
    CoverageError,
    CoverageTile,
    coverage_report,
    spherical_area,
)


def test_spherical_area_whole_sphere():
    """The zoom 0 tile covers the Web Mercator latitude range."""
    area = spherical_area((0, 0, 0))
    expected = (
        2 * math.pi * mercantile.RE ** 2
        * 2 * math.sin(math.radians(85.0511287798066))
    )
    assert area == pytest.approx(expected)


def test_spherical_area_quarter_tile():
    """Clipping at the equator gives half of the parent area."""
    parent = spherical_area((0, 0, 0))
    north = spherical_area((0, 0, 0), north=0.0)
    south = spherical_area((0, 0, 0), south=0.0)
    assert north == pytest.approx(parent / 2)
    assert south == pytest.approx(parent / 2)
    assert north + south == pytest.approx(parent)


def test_spherical_area_matches_bounds():
    tile = mercantile.Tile(486, 332, 10)
    bbox = mercantile.bounds(tile)
    expected = (
        mercantile.RE ** 2
        * math.radians(bbox.east - bbox.west)
        * (math.sin(math.radians(bbox.north)) - math.sin(math.radians(bbox.south)))
    )
    assert spherical_area(tile) == pytest.approx(expected)


def test_spherical_area_clips_to_tile():
    """Latitude limits outside the tile are clipped."""
    tile = mercantile.Tile(0, 0, 1)
    bbox = mercantile.bounds(tile)
    assert spherical_area(tile, south=-90, north=90) == pytest.approx(
        spherical_area(tile)
    )
    assert spherical_area(tile, south=bbox.north) == 0
    assert spherical_area(tile, north=bbox.south) == 0


def test_spherical_area_rejects_empty_extent():
    with pytest.raises(CoverageError):
        CoverageTile((0, 0, 0), south=10, north=10)


def test_children_area_partition():
    """Four equal-count children exactly partition the parent area."""
    parent = CoverageTile((1, 0, 1))
    kids = parent.children()
    assert len(kids) == 4
    assert sum(kid.area() for kid in kids) == pytest.approx(
        parent.area(), rel=1e-12
    )


def test_children_of_clipped_tile_cover_clipped_area():
    parent = CoverageTile((1, 0, 1), south=0.0, north=40.0)
    kids = parent.children()
    assert len(kids) == 2
    assert sum(kid.area() for kid in kids) == pytest.approx(parent.area())


def test_count_full_tiles():
    """Counts at a common zoom are integer tile counts."""
    bbox = CoverageTile((0, 0, 1))
    assert bbox.count(1) == pytest.approx(1.0)
    assert bbox.count(2) == pytest.approx(4.0)
    assert sum(kid.count(2) for kid in bbox.children()) == pytest.approx(4.0)


def test_count_mixed_zooms():
    """A coarse tile and a finer tile count consistently."""
    coarse = CoverageTile((0, 0, 0))
    fine = CoverageTile((0, 0, 1))
    assert coarse.count(2) == pytest.approx(16.0)
    assert fine.count(2) == pytest.approx(4.0)
    assert fine.count(0) == pytest.approx(0.25)


def test_clipped_tile_counts_whole_tile_by_default():
    tile = CoverageTile((0, 0, 0), south=0.0, north=45.0)
    assert tile.count(0) == pytest.approx(1.0)
    assert tile.count(0, clipped=True) < 1.0
    assert tile.count(0, clipped=True) == pytest.approx(
        (tile.east - tile.west) / 360.0
        * (0.5 - _mercator_y(45.0))
        * 1.0
    )


def _mercator_y(lat):
    sinlat = math.sin(math.radians(lat))
    return 0.5 - 0.25 * math.log((1.0 + sinlat) / (1.0 - sinlat)) / math.pi


def test_report_fully_surveyed():
    total = [CoverageTile((0, 0, 0))]
    report = coverage_report(total, total)
    assert report.area_coverage == pytest.approx(1.0)
    assert report.count_coverage == pytest.approx(1.0)
    assert report.difference_pp == pytest.approx(0.0)
    assert report.contributions[0].difference_pp == pytest.approx(0.0)


def test_report_empty_surveyed():
    report = coverage_report([(0, 0, 0)], [])
    assert report.area_coverage == 0.0
    assert report.count_coverage == 0.0
    assert report.difference_pp == 0.0


def test_report_requires_total():
    with pytest.raises(CoverageError):
        coverage_report([], [])


def test_report_high_latitude_share():
    """The northern quarter tile has a much smaller area share."""
    total = [
        mercantile.Tile(0, 0, 2),
        mercantile.Tile(1, 0, 2),
        mercantile.Tile(0, 1, 2),
        mercantile.Tile(1, 1, 2),
    ]
    surveyed = [mercantile.Tile(0, 0, 2), mercantile.Tile(1, 0, 2)]
    report = coverage_report(total, surveyed)

    assert report.count_coverage == pytest.approx(0.5)
    assert report.area_coverage < 0.1
    assert report.difference_pp == pytest.approx(
        (report.area_coverage - 0.5) * 100.0
    )
    assert report.relative_difference < 0


def test_report_contributions_sum_to_difference():
    total = [
        mercantile.Tile(0, 0, 2),
        mercantile.Tile(1, 0, 2),
        mercantile.Tile(0, 1, 2),
        mercantile.Tile(1, 1, 2),
        mercantile.Tile(0, 2, 2),
    ]
    surveyed = [
        mercantile.Tile(0, 0, 2),
        mercantile.Tile(0, 2, 2),
        mercantile.Tile(1, 2, 2),
    ]
    report = coverage_report(total, surveyed)

    assert sum(item.difference_pp for item in report.contributions) == pytest.approx(
        report.difference_pp
    )
    assert sum(item.area_share for item in report.contributions) == pytest.approx(
        report.area_coverage
    )
    assert sum(item.count_share for item in report.contributions) == pytest.approx(
        report.count_coverage
    )


def test_report_largest_contributor_identified():
    """The region whose count share most exceeds its area share ranks first."""
    total = [
        CoverageTile((0, 0, 2), label="north"),
        CoverageTile((0, 1, 2), label="north"),
        CoverageTile((0, 2, 2), label="south"),
        CoverageTile((0, 3, 2), label="south"),
    ]
    surveyed = [
        CoverageTile((0, 0, 2), label="north"),
        CoverageTile((0, 1, 2), label="north"),
        CoverageTile((0, 2, 2), label="south"),
    ]
    report = coverage_report(total, surveyed)

    top = report.contributions[0]
    assert top.label == "south"
    assert top.rank == 1
    assert top.difference_pp > 0
    assert top.area_share > top.count_share


def test_report_contribution_rank_order():
    total = [
        CoverageTile((0, 0, 2), label="high"),
        CoverageTile((0, 3, 2), label="low"),
    ]
    surveyed = [
        CoverageTile((0, 0, 2), label="high"),
        CoverageTile((1, 3, 2), label="low"),
    ]
    report = coverage_report(total, surveyed)
    diffs = [abs(item.difference_pp) for item in report.contributions]
    assert diffs == sorted(diffs, reverse=True)
    assert [item.rank for item in report.contributions] == list(
        range(1, len(diffs) + 1)
    )


def test_report_mixed_zoom_no_double_count():
    """A parent and one of its children count once."""
    parent = CoverageTile((0, 0, 1))
    child = CoverageTile((0, 0, 2))
    report = coverage_report([parent, child], [child], count_zoom=1)
    assert report.total_area == pytest.approx(parent.area())
    assert report.total_count == pytest.approx(parent.count(1))
    assert report.surveyed_area == pytest.approx(child.area())
    assert report.surveyed_count == pytest.approx(child.count(1))


def test_report_overlapping_surveys_count_once():
    tile = CoverageTile((0, 0, 1))
    report = coverage_report([tile], [tile, tile])
    assert report.surveyed_area == pytest.approx(tile.area())
    assert report.area_coverage == pytest.approx(1.0)


def test_report_latitude_clipped_survey():
    total = [CoverageTile((0, 0, 1))]
    surveyed = CoverageTile((0, 0, 1), south=0.0, north=40.0)
    report = coverage_report(total, [surveyed])

    assert report.surveyed_area == pytest.approx(surveyed.area())
    assert report.area_coverage == pytest.approx(
        surveyed.area() / total[0].area()
    )
    # Default count basis records the partially surveyed block as a tile.
    assert report.surveyed_count == pytest.approx(1.0)
    assert report.count_coverage == pytest.approx(1.0)


def test_report_latitude_clipped_survey_clip_count():
    total = [CoverageTile((0, 0, 1))]
    surveyed = CoverageTile((0, 0, 1), south=0.0, north=40.0)
    report = coverage_report(total, [surveyed], count_partial="clip")

    assert report.surveyed_count == pytest.approx(
        surveyed.count(1, clipped=True)
    )
    assert report.count_coverage < 1.0


def test_replace_tile_with_four_children_preserves_totals():
    """Replacing a whole tile with four equal-coverage children is neutral."""
    parent = CoverageTile((1, 0, 1), label="region")
    children = parent.children()

    surveyed = CoverageTile((1, 0, 1), south=0.0, north=40.0, label="region")
    as_children = []
    for child in parent.children():
        south = max(child.south, 0.0)
        north = min(child.north, 40.0)
        if south < north:
            as_children.append(
                CoverageTile(child.tile, south, north, label="region")
            )

    before = coverage_report([parent], [surveyed], count_zoom=1)
    after = coverage_report(children, as_children, count_zoom=1)

    assert after.total_area == pytest.approx(before.total_area)
    assert after.surveyed_area == pytest.approx(before.surveyed_area)
    assert after.area_coverage == pytest.approx(before.area_coverage)


def test_replace_full_tile_with_four_children_preserves_all_totals():
    """With no clipping, replacing a tile by its children is fully neutral."""
    parent = CoverageTile((1, 0, 1), label="region")
    children = parent.children()

    before = coverage_report([parent], [parent], count_zoom=1)
    after = coverage_report(children, children, count_zoom=1)

    assert after.total_area == pytest.approx(before.total_area)
    assert after.surveyed_area == pytest.approx(before.surveyed_area)
    assert after.total_count == pytest.approx(before.total_count)
    assert after.surveyed_count == pytest.approx(before.surveyed_count)
    assert after.area_coverage == pytest.approx(before.area_coverage)
    assert after.count_coverage == pytest.approx(before.count_coverage)


def test_depth_invariance_of_report():
    """Replacing every record with children leaves the report unchanged."""
    total = [CoverageTile((0, 0, 1))]
    surveyed = [CoverageTile((0, 0, 1), south=0.0, north=40.0)]
    before = coverage_report(total, surveyed)

    total_kids = total[0].children()
    surveyed_kids = []
    for child in surveyed[0].children():
        surveyed_kids.append(child)
    after = coverage_report(total_kids, surveyed_kids)

    assert after.area_coverage == pytest.approx(before.area_coverage)
    assert after.surveyed_area == pytest.approx(before.surveyed_area)


def test_report_outside_surveyed_is_reported():
    total = [CoverageTile((0, 0, 1))]
    surveyed = [CoverageTile((1, 1, 1))]
    report = coverage_report(total, surveyed)

    assert report.surveyed_area == 0.0
    assert report.outside_area > 0.0
    assert report.area_coverage == 0.0


def test_report_as_dict_top():
    total = [
        CoverageTile((0, 0, 2), label="a"),
        CoverageTile((0, 3, 2), label="b"),
        CoverageTile((1, 3, 2), label="c"),
    ]
    report = coverage_report(total, total, count_zoom=2)
    data = report.as_dict(top=2)
    assert len(data["contributions"]) == 2
    assert len(report.as_dict()["contributions"]) == 3
    assert data["area_coverage"] == pytest.approx(report.area_coverage)
    assert data["total_area_km2"] == pytest.approx(report.total_area / 1e6)


def test_invalid_count_zoom():
    with pytest.raises(CoverageError):
        coverage_report([(0, 0, 0)], [], count_zoom=-1)
    with pytest.raises(CoverageError):
        CoverageTile((0, 0, 0)).count(1.5)


def test_invalid_count_partial():
    with pytest.raises(CoverageError):
        coverage_report([(0, 0, 0)], [], count_partial="nope")


def test_invalid_record():
    with pytest.raises(CoverageError):
        coverage_report([{"foo": 1}], [])
    with pytest.raises(CoverageError):
        coverage_report([(0, 0, 0)], ["nope"])


def test_record_from_mapping_with_xy_z():
    report = coverage_report(
        [{"x": 0, "y": 0, "z": 0, "label": "world"}], []
    )
    assert report.contributions == ()
    assert report.total_area == pytest.approx(spherical_area((0, 0, 0)))


def test_coverage_tile_properties():
    tile = CoverageTile((0, 0, 1), south=0.0, north=40.0, label="x")
    bbox = mercantile.bounds(tile.tile)
    assert tile.west == bbox.west
    assert tile.east == bbox.east
    assert tile.bounds == mercantile.LngLatBbox(
        bbox.west, 0.0, bbox.east, 40.0
    )
    assert tile.is_partial
    assert not CoverageTile((0, 0, 1)).is_partial
