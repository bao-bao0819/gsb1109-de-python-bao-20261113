"""Test station/offset sectioning along baselines."""

import math

import pytest

from pygeoif import LinearRing
from pygeoif import LineString
from pygeoif import Point
from pygeoif.exceptions import DimensionError
from pygeoif.sectioning import BaselineSectioner
from pygeoif.sectioning import Match
from pygeoif.sectioning import NearestPoint
from pygeoif.sectioning import Section
from pygeoif.sectioning import Tangent


def test_construction_rejects_non_linestring() -> None:
    with pytest.raises(TypeError, match="baseline must be a LineString"):
        BaselineSectioner([(0, 0), (1, 1)])  # type: ignore[arg-type]


def test_construction_rejects_3d_baseline() -> None:
    with pytest.raises(DimensionError, match="only implemented for 2D"):
        BaselineSectioner(LineString([(0, 0, 0), (1, 1, 1)]))


def test_construction_rejects_short_and_degenerate_baseline() -> None:
    with pytest.raises(ValueError, match="at least two distinct points"):
        BaselineSectioner(LineString([(0, 0)]))
    with pytest.raises(ValueError, match="positive length"):
        BaselineSectioner(LineString([(0, 0), (1e-12, 0)]), tolerance=1e-6)


def test_construction_validates_arguments() -> None:
    line = LineString([(0, 0), (1, 0)])
    with pytest.raises(ValueError, match="tangent must be"):
        BaselineSectioner(line, tangent="sideways")
    with pytest.raises(ValueError, match="tolerance must be"):
        BaselineSectioner(line, tolerance=-1)


def test_length_and_properties() -> None:
    line = LineString([(0, 0), (3, 4), (3, 9)])
    sectioner = BaselineSectioner(line)

    assert math.isclose(sectioner.length, 10.0)
    assert sectioner.tangent is Tangent.OUTGOING
    assert sectioner.baseline == line
    assert len(sectioner) == 0
    assert sectioner.sections == ()
    assert "nope" not in sectioner


def test_locate_left_and_right_of_travel_direction() -> None:
    sectioner = BaselineSectioner(LineString([(0, 0), (10, 0)]))

    left = sectioner.locate(4, 3)
    right = sectioner.locate(4, -3)

    assert left.coords == ((4.0, 3.0),)
    assert right.coords == ((4.0, -3.0),)


def test_locate_left_flips_with_travel_direction() -> None:
    sectioner = BaselineSectioner(LineString([(0, 0), (10, 0), (0, 0)]))

    point = sectioner.locate(15, 2)

    assert point.coords == ((5.0, -2.0),)


def test_locate_at_turn_vertex_uses_tangent_convention() -> None:
    sectioner = BaselineSectioner(LineString([(0, 0), (10, 0), (10, 10)]))

    outgoing = sectioner.locate(10, 1, Tangent.OUTGOING)
    incoming = sectioner.locate(10, 1, Tangent.INCOMING)

    assert outgoing.coords == ((9.0, 0.0),)
    assert incoming.coords == ((10.0, 1.0),)
    assert sectioner.locate(10, 1).coords == outgoing.coords
    assert sectioner.locate(10, 1, "incoming").coords == incoming.coords


def test_locate_inside_following_segment() -> None:
    sectioner = BaselineSectioner(LineString([(0, 0), (10, 0), (10, 10)]))

    assert sectioner.locate(15, 2).coords == ((8.0, 5.0),)


def test_locate_at_open_baseline_endpoints() -> None:
    sectioner = BaselineSectioner(LineString([(0, 0), (10, 0), (10, 10)]))

    assert sectioner.locate(0, 5).coords == ((0.0, 5.0),)
    assert sectioner.locate(20, 5, Tangent.INCOMING).coords == ((5.0, 10.0),)
    with pytest.raises(ValueError, match="No outgoing segment"):
        sectioner.locate(20, 0, Tangent.OUTGOING)
    with pytest.raises(ValueError, match="No incoming segment"):
        sectioner.locate(0, 0, Tangent.INCOMING)


def test_incoming_near_open_start_stays_on_first_segment() -> None:
    sectioner = BaselineSectioner(LineString([(0, 0), (100, 0), (100, 10)]))

    point = sectioner.locate(1e-8, 1, Tangent.INCOMING)

    assert 0.0 <= point.x <= 1e-6
    assert math.isclose(point.y, 1.0)


def test_locate_validates_chainage_and_offset() -> None:
    sectioner = BaselineSectioner(LineString([(0, 0), (10, 0)]))

    with pytest.raises(ValueError, match="chainage must be between"):
        sectioner.locate(10.1, 0)
    with pytest.raises(ValueError, match="offset must be a finite number"):
        sectioner.locate(5, math.nan)


def test_ring_wraps_chainage_and_tangents() -> None:
    ring = LinearRing([(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)])
    sectioner = BaselineSectioner(ring)

    assert math.isclose(sectioner.length, 40.0)
    assert sectioner.locate(40, 1, Tangent.OUTGOING).coords == ((0.0, 1.0),)
    assert sectioner.locate(0, 1, Tangent.INCOMING).coords == ((1.0, 0.0),)
    assert sectioner.locate(40, 1, Tangent.INCOMING).coords == ((1.0, 0.0),)
    assert sectioner.locate(25, 0).coords == ((5.0, 10.0),)


def test_add_section_records_metadata() -> None:
    sectioner = BaselineSectioner(LineString([(0, 0), (10, 0), (10, 10)]))

    section = sectioner.add_section("S-01", 10, 1, Tangent.INCOMING)

    assert isinstance(section, Section)
    assert section.identifier == "S-01"
    assert math.isclose(section.chainage, 10.0)
    assert section.offset == 1
    assert section.tangent is Tangent.INCOMING
    assert section.segment_index == 0
    assert section.point.coords == ((10.0, 1.0),)
    assert "S-01" in sectioner
    assert len(sectioner) == 1
    assert sectioner.section("S-01") == section


def test_add_section_rejects_duplicates_and_normalizes_ring() -> None:
    sectioner = BaselineSectioner(
        LinearRing([(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)]),
    )
    sectioner.add_section(1, 40, 0)

    assert math.isclose(sectioner.section(1).chainage, 0.0)
    with pytest.raises(ValueError, match="already registered"):
        sectioner.add_section(1, 5, 0)
    with pytest.raises(KeyError, match="No section registered"):
        sectioner.section(99)


def test_sections_are_ordered_by_chainage() -> None:
    sectioner = BaselineSectioner(LineString([(0, 0), (10, 0)]))
    sectioner.add_section("b", 8, 0)
    sectioner.add_section("a", 2, 0)

    assert [section.identifier for section in sectioner.sections] == ["a", "b"]


def test_section_window_splits_at_chainage_midpoint() -> None:
    sectioner = BaselineSectioner(LineString([(0, 0), (10, 0)]))
    sectioner.add_section("a", 2, 0)
    sectioner.add_section("b", 8, 0)

    assert sectioner._section_window(2.0) == (0.0, 5.0)
    assert sectioner._section_window(8.0) == (5.0, 10.0)


def test_section_window_wraps_around_closed_ring() -> None:
    sectioner = BaselineSectioner(
        LinearRing([(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)]),
    )
    sectioner.add_section("start", 1, 0)
    sectioner.add_section("end", 39, 0)

    assert sectioner._section_window(1.0) == (0.0, 20.0)
    assert sectioner._section_window(39.0) == (20.0, 0.0)


def test_same_station_left_and_right_sections_stay_distinguishable() -> None:
    sectioner = BaselineSectioner(LineString([(0, 0), (10, 0)]))
    left = sectioner.add_section("L", 5, 2)
    right = sectioner.add_section("R", 5, -2)

    assert left.point.coords == ((5.0, 2.0),)
    assert right.point.coords == ((5.0, -2.0),)
    assert sectioner.find_section((5, 2)).identifier == "L"
    assert sectioner.find_section((5, -2)).identifier == "R"
    assert [
        match.section.identifier
        for match in sectioner.find_sections((5, 2), max_distance=0.1)
    ] == ["L"]


def test_nearest_point_reports_side_and_along() -> None:
    sectioner = BaselineSectioner(LineString([(0, 0), (10, 0)]))

    nearest = sectioner.nearest_point(Point(4, 2))

    assert isinstance(nearest, NearestPoint)
    assert nearest.point.coords == ((4.0, 0.0),)
    assert math.isclose(nearest.distance, 2.0)
    assert math.isclose(nearest.side, 2.0)
    assert math.isclose(nearest.along, 0.0)
    assert nearest.chainage == 4.0
    assert nearest.segment_index == 0


def test_nearest_points_keeps_both_ring_vertex_tangents() -> None:
    ring = LinearRing([(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)])
    sectioner = BaselineSectioner(ring)

    projections = sectioner.nearest_points((0, 0))

    zero_distance = [p for p in projections if math.isclose(p.distance, 0.0)]
    assert {p.segment_index for p in zero_distance} == {0, 3}
    assert {p.chainage for p in zero_distance} == {0.0, 40.0}


def test_nearest_point_rejects_3d_coordinate() -> None:
    sectioner = BaselineSectioner(LineString([(0, 0), (10, 0)]))

    with pytest.raises(DimensionError):
        sectioner.nearest_point(Point(1, 2, 3))


def test_folded_branches_keep_nearest_section_first() -> None:
    hairpin = LineString([(0, 0), (10, 0), (10, 2), (0, 2)])
    sectioner = BaselineSectioner(hairpin)
    sectioner.add_section("S1", 5, 0)
    sectioner.add_section("S2", 17, 0)

    first = sectioner.find_sections((5, 0))
    second = sectioner.find_sections((5, 2))

    assert [match.section.identifier for match in first] == ["S1", "S2"]
    assert math.isclose(first[0].distance, 0.0)
    assert math.isclose(first[1].distance, 2.0)
    assert [match.section.identifier for match in second] == ["S2", "S1"]


def test_folded_branch_tie_returns_both_sections() -> None:
    hairpin = LineString([(0, 0), (10, 0), (10, 2), (0, 2)])
    sectioner = BaselineSectioner(hairpin)
    sectioner.add_section("S1", 5, 0)
    sectioner.add_section("S2", 17, 0)

    matches = sectioner.find_sections((5, 1))

    assert isinstance(matches[0], Match)
    assert sorted(match.section.identifier for match in matches) == ["S1", "S2"]
    assert all(math.isclose(match.distance, 1.0) for match in matches)


def test_coincident_folded_branches_never_jump_to_other_branch() -> None:
    sectioner = BaselineSectioner(LineString([(0, 0), (10, 0), (0, 0)]))
    sectioner.add_section("A", 3, 0)
    sectioner.add_section("C", 17, 0)

    matches = sectioner.find_sections((3, 0), max_distance=0.01)

    assert {match.section.identifier for match in matches} == {"A", "C"}
    assert all(math.isclose(match.distance, 0.0) for match in matches)


def test_max_distance_filters_remote_sections() -> None:
    sectioner = BaselineSectioner(LineString([(0, 0), (10, 0), (10, 2), (0, 2)]))
    sectioner.add_section("S1", 5, 0)
    sectioner.add_section("S2", 17, 0)

    assert [
        match.section.identifier
        for match in sectioner.find_sections((5, 0), max_distance=0.5)
    ] == ["S1"]
    assert sectioner.find_sections((20, 20), max_distance=5.0) == ()
    assert sectioner.find_section((20, 20), max_distance=5.0) is None
    with pytest.raises(ValueError, match="max_distance must be"):
        sectioner.find_sections((0, 0), max_distance=-1)


def test_round_trip_looks_each_section_back_up() -> None:
    sectioner = BaselineSectioner(
        LineString([(0, 0), (3, 4), (3, 9), (-2, 9)]),
    )
    for station, offset in [(2.5, 1.5), (5.0, -2.0), (8.0, 0.0), (12.3, 3.5)]:
        section = sectioner.add_section(f"S{station}", station, offset)

        found = sectioner.find_section(section.point.coords[0])

        assert found is not None
        assert found.identifier == section.identifier


def test_ring_round_trip_across_zero_chainage() -> None:
    sectioner = BaselineSectioner(
        LinearRing([(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)]),
    )
    start = sectioner.add_section("zero", 1, 0)
    end = sectioner.add_section("nearfull", 39, 0)

    assert sectioner.find_section(start.point.coords[0]).identifier == "zero"
    assert sectioner.find_section(end.point.coords[0]).identifier == "nearfull"


def test_find_sections_without_registrations() -> None:
    sectioner = BaselineSectioner(LineString([(0, 0), (10, 0)]))

    assert sectioner.find_sections((5, 0)) == ()
    assert sectioner.find_section((5, 0)) is None
