"""Test the auditable geometric identity comparison."""

import pytest

from pygeoif import IdentityMatch
from pygeoif import IdentityVerdict
from pygeoif import compare_identity
from pygeoif.geometry import GeometryCollection
from pygeoif.geometry import LinearRing
from pygeoif.geometry import LineString
from pygeoif.geometry import MultiLineString
from pygeoif.geometry import MultiPoint
from pygeoif.geometry import MultiPolygon
from pygeoif.geometry import Point
from pygeoif.geometry import Polygon
from pygeoif.identity import MAX_CORRESPONDENCES


def test_parcel_transcribed_from_another_vertex() -> None:
    """The same parcel, copied starting at a different vertex."""
    first = Polygon([(0, 0), (4, 0), (4, 4), (0, 4), (0, 0)])
    second = Polygon([(4, 4), (0, 4), (0, 0), (4, 0), (4, 4)])

    match = compare_identity(first, second)

    assert match.verdict is IdentityVerdict.IDENTICAL
    assert bool(match)
    exterior = match.correspondences[0].rings[0]
    assert exterior.first_ring == ("exterior",)
    assert exterior.second_ring == ("exterior",)
    assert len(exterior.alignments) == 1
    alignment = exterior.alignments[0]
    assert not alignment.is_reversed
    assert alignment.vertex_map == ((0, 2), (1, 3), (2, 0), (3, 1))


def test_parcel_with_reversed_exterior() -> None:
    """The same parcel with the exterior ring traversed the other way."""
    first = Polygon([(0, 0), (4, 0), (4, 4), (0, 4), (0, 0)])
    second = Polygon([(4, 4), (4, 0), (0, 0), (0, 4), (4, 4)])

    match = compare_identity(first, second)

    assert match.verdict is IdentityVerdict.IDENTICAL
    alignment = match.correspondences[0].rings[0].alignments[0]
    assert alignment.is_reversed
    assert alignment.vertex_map == ((0, 2), (1, 1), (2, 0), (3, 3))


def test_parcel_with_permuted_holes() -> None:
    """The same parcel with the holes listed in another order."""
    first = Polygon(
        [(0, 0), (8, 0), (8, 8), (0, 8), (0, 0)],
        holes=[
            [(1, 1), (2, 1), (2, 2), (1, 2), (1, 1)],
            [(5, 5), (6, 5), (6, 6), (5, 6), (5, 5)],
        ],
    )
    second = Polygon(
        [(0, 0), (0, 8), (8, 8), (8, 0), (0, 0)],
        holes=[
            [(5, 5), (5, 6), (6, 6), (6, 5), (5, 5)],
            [(2, 2), (2, 1), (1, 1), (1, 2), (2, 2)],
        ],
    )

    match = compare_identity(first, second)

    assert match.verdict is IdentityVerdict.IDENTICAL
    rings = match.correspondences[0].rings
    hole_map = {r.first_ring: r.second_ring for r in rings[1:]}
    assert hole_map == {
        ("interior", 0): ("interior", 1),
        ("interior", 1): ("interior", 0),
    }
    assert any("Interior ring 0" in reason for reason in match.reasons)


def test_different_parcels() -> None:
    first = Polygon([(0, 0), (4, 0), (4, 4), (0, 4), (0, 0)])
    second = Polygon([(0, 0), (5, 0), (5, 5), (0, 5), (0, 0)])

    match = compare_identity(first, second)

    assert match.verdict is IdentityVerdict.DIFFERENT
    assert not match
    assert match.correspondences == ()
    assert match.reasons


def test_different_number_of_holes() -> None:
    first = Polygon(
        [(0, 0), (8, 0), (8, 8), (0, 8), (0, 0)],
        holes=[[(1, 1), (2, 1), (2, 2), (1, 2), (1, 1)]],
    )
    second = Polygon([(0, 0), (8, 0), (8, 8), (0, 8), (0, 0)])

    match = compare_identity(first, second)

    assert match.verdict is IdentityVerdict.DIFFERENT
    assert "interior rings" in match.reasons[0]


def test_unmatchable_holes() -> None:
    first = Polygon(
        [(0, 0), (8, 0), (8, 8), (0, 8), (0, 0)],
        holes=[[(1, 1), (2, 1), (2, 2), (1, 2), (1, 1)]],
    )
    second = Polygon(
        [(0, 0), (8, 0), (8, 8), (0, 8), (0, 0)],
        holes=[[(3, 3), (4, 3), (4, 4), (3, 4), (3, 3)]],
    )

    match = compare_identity(first, second)

    assert match.verdict is IdentityVerdict.DIFFERENT


def test_exact_comparison_without_grid() -> None:
    """Without a grid even tiny deviations are reported as different."""
    first = Polygon([(0, 0), (4, 0), (4, 4), (0, 4), (0, 0)])
    second = Polygon([(0, 0), (4, 0), (4, 4), (0, 4 + 1e-9), (0, 0)])

    match = compare_identity(first, second)

    assert match.verdict is IdentityVerdict.DIFFERENT


def test_grid_absorbs_transcription_noise() -> None:
    first = Polygon([(0, 0), (4, 0), (4, 4), (0, 4), (0, 0)])
    second = Polygon(
        [(0.01, 0), (4, -0.01), (4.01, 4), (0, 3.99), (0.01, 0)],
    )

    match = compare_identity(first, second, grid_size=0.5)

    assert match.verdict is IdentityVerdict.IDENTICAL
    assert match.grid_size == 0.5


def test_narrow_hole_collapsed_to_a_point_is_undecidable() -> None:
    """A narrow hole pressed into a grid point must not prove identity."""
    with_hole = Polygon(
        [(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)],
        holes=[[(5.1, 5.1), (5.4, 5.1), (5.1, 5.4), (5.1, 5.1)]],
    )
    without_hole = Polygon([(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)])

    match = compare_identity(with_hole, without_hole, grid_size=1.0)

    assert match.verdict is IdentityVerdict.UNDECIDABLE
    assert not match
    assert match.correspondences == ()
    assert any("collapses interior ring 0" in reason for reason in match.reasons)


def test_narrow_hole_collapsed_to_a_line_is_undecidable() -> None:
    """A narrow hole pressed into a line must not prove identity."""
    with_hole = Polygon(
        [(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)],
        holes=[[(5.1, 5.1), (6.4, 5.1), (7.6, 5.2), (5.1, 5.1)]],
    )
    without_hole = Polygon([(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)])

    match = compare_identity(with_hole, without_hole, grid_size=1.0)

    assert match.verdict is IdentityVerdict.UNDECIDABLE
    assert any("zero area" in reason for reason in match.reasons)


def test_collapsed_exterior_is_undecidable() -> None:
    tiny = Polygon([(0.1, 0.1), (0.2, 0.1), (0.1, 0.2), (0.1, 0.1)])
    other = Polygon([(0.1, 0.1), (0.2, 0.1), (0.1, 0.2), (0.1, 0.1)])

    match = compare_identity(tiny, other, grid_size=1.0)

    assert match.verdict is IdentityVerdict.UNDECIDABLE
    assert any("exterior ring" in reason for reason in match.reasons)


def test_collapsed_line_is_undecidable() -> None:
    first = LineString([(0.1, 0.1), (0.2, 0.15)])
    second = LineString([(0.1, 0.1), (0.2, 0.15)])

    match = compare_identity(first, second, grid_size=1.0)

    assert match.verdict is IdentityVerdict.UNDECIDABLE
    assert any("single grid point" in reason for reason in match.reasons)


def test_merged_points_are_undecidable() -> None:
    first = MultiPoint([(0.1, 0.1), (0.4, 0.1)])
    second = MultiPoint([(0.1, 0.1), (0.4, 0.1)])

    match = compare_identity(first, second, grid_size=1.0)

    assert match.verdict is IdentityVerdict.UNDECIDABLE
    assert any("merges points" in reason for reason in match.reasons)


def test_repeated_vertices_allow_multiple_alignments() -> None:
    """Repeated vertices make several vertex correspondences possible."""
    first = Polygon([(0, 0), (4, 0), (4, 4), (0, 0), (4, 0), (4, 4)])
    second = Polygon([(4, 4), (0, 0), (4, 0), (4, 4), (0, 0), (4, 0)])

    match = compare_identity(first, second)

    assert match.verdict is IdentityVerdict.IDENTICAL
    alignments = match.correspondences[0].rings[0].alignments
    assert len(alignments) > 1
    offsets = {alignment.offset for alignment in alignments}
    assert offsets == {1, 4}
    for alignment in alignments:
        assert sorted(j for _, j in alignment.vertex_map) == [0, 1, 2, 3, 4, 5]


def test_identical_holes_allow_multiple_correspondences() -> None:
    hole = [(1, 1), (2, 1), (2, 2), (1, 2), (1, 1)]
    first = Polygon([(0, 0), (8, 0), (8, 8), (0, 8), (0, 0)], holes=[hole, hole])
    second = Polygon([(0, 0), (8, 0), (8, 8), (0, 8), (0, 0)], holes=[hole, hole])

    match = compare_identity(first, second)

    assert match.verdict is IdentityVerdict.IDENTICAL
    assert len(match.correspondences) == 2
    hole_maps = {
        tuple(
            (r.first_ring, r.second_ring)
            for r in correspondence.rings
            if r.first_ring[0] == "interior"
        )
        for correspondence in match.correspondences
    }
    assert hole_maps == {
        ((("interior", 0), ("interior", 0)), (("interior", 1), ("interior", 1))),
        ((("interior", 0), ("interior", 1)), (("interior", 1), ("interior", 0))),
    }


def test_correspondences_are_truncated() -> None:
    points = [(0, 0)] * 5
    first = MultiPoint(points)
    second = MultiPoint(points)

    match = compare_identity(first, second)

    assert match.verdict is IdentityVerdict.IDENTICAL
    assert len(match.correspondences) == MAX_CORRESPONDENCES
    assert match.truncated


def test_multipolygon_with_permuted_parts() -> None:
    square1 = [(0, 0), (1, 0), (1, 1), (0, 1), (0, 0)]
    square2 = [(5, 5), (6, 5), (6, 6), (5, 6), (5, 5)]
    first = MultiPolygon([(square1,), (square2,)])
    second = MultiPolygon([(square2,), (square1,)])

    match = compare_identity(first, second)

    assert match.verdict is IdentityVerdict.IDENTICAL
    assert match.correspondences[0].geometry_map == ((0, 1), (1, 0))
    paths = {ring.first_ring for ring in match.correspondences[0].rings}
    assert paths == {(0, "exterior"), (1, "exterior")}


def test_multipoint_with_permuted_points() -> None:
    first = MultiPoint([(0, 0), (1, 1), (2, 2)])
    second = MultiPoint([(2, 2), (0, 0), (1, 1)])

    match = compare_identity(first, second)

    assert match.verdict is IdentityVerdict.IDENTICAL
    assert match.correspondences[0].geometry_map == ((0, 1), (1, 2), (2, 0))


def test_linestring_traversed_backwards() -> None:
    first = LineString([(0, 0), (1, 2), (3, 4)])
    second = LineString([(3, 4), (1, 2), (0, 0)])

    match = compare_identity(first, second)

    assert match.verdict is IdentityVerdict.IDENTICAL
    alignment = match.correspondences[0].rings[0].alignments[0]
    assert alignment.is_reversed
    assert alignment.vertex_map == ((0, 2), (1, 1), (2, 0))


def test_multilinestring_with_permuted_lines() -> None:
    first = MultiLineString([[(0, 0), (1, 1)], [(5, 5), (6, 6)]])
    second = MultiLineString([[(6, 6), (5, 5)], [(1, 1), (0, 0)]])

    match = compare_identity(first, second)

    assert match.verdict is IdentityVerdict.IDENTICAL
    assert match.correspondences[0].geometry_map == ((0, 1), (1, 0))


def test_linearring_rotation_and_reversal() -> None:
    first = LinearRing([(0, 0), (4, 0), (4, 4), (0, 4), (0, 0)])
    second = LinearRing([(4, 0), (0, 0), (0, 4), (4, 4), (4, 0)])

    match = compare_identity(first, second)

    assert match.verdict is IdentityVerdict.IDENTICAL
    assert match.correspondences[0].rings[0].first_ring == ("ring",)


def test_points() -> None:
    assert compare_identity(Point(0, 0), Point(0, 0))
    assert not compare_identity(Point(0, 0), Point(1, 0))


def test_points_with_grid() -> None:
    match = compare_identity(Point(0.01, 0), Point(-0.01, 0), grid_size=0.5)

    assert match.verdict is IdentityVerdict.IDENTICAL


def test_geometry_collection_with_permuted_members() -> None:
    first = GeometryCollection(
        [Point(0, 0), LineString([(0, 0), (1, 1)])],
    )
    second = GeometryCollection(
        [LineString([(1, 1), (0, 0)]), Point(0, 0)],
    )

    match = compare_identity(first, second)

    assert match.verdict is IdentityVerdict.IDENTICAL
    assert match.correspondences[0].geometry_map == ((0, 1), (1, 0))


def test_different_geometry_types() -> None:
    match = compare_identity(
        Point(0, 0),
        LineString([(0, 0), (1, 1)]),
    )

    assert match.verdict is IdentityVerdict.DIFFERENT
    assert "types differ" in match.reasons[0]


def test_empty_geometries_are_undecidable() -> None:
    match = compare_identity(Polygon(()), Polygon(()))

    assert match.verdict is IdentityVerdict.UNDECIDABLE


def test_empty_vs_non_empty_is_different() -> None:
    match = compare_identity(Polygon(()), Point(0, 0))

    assert match.verdict is IdentityVerdict.DIFFERENT


def test_empty_vs_non_empty_same_type() -> None:
    match = compare_identity(Point(0, 0), Point(float("nan"), 0))

    assert match.verdict is IdentityVerdict.DIFFERENT


def test_geo_interface_mappings_are_accepted() -> None:
    first = {
        "type": "Polygon",
        "coordinates": (
            ((0, 0), (4, 0), (4, 4), (0, 4), (0, 0)),
        ),
    }
    second = Polygon([(4, 0), (4, 4), (0, 4), (0, 0), (4, 0)])

    match = compare_identity(first, second)

    assert match.verdict is IdentityVerdict.IDENTICAL


def test_invalid_grid_size() -> None:
    with pytest.raises(ValueError, match="grid_size"):
        compare_identity(Point(0, 0), Point(0, 0), grid_size=0)
    with pytest.raises(ValueError, match="grid_size"):
        compare_identity(Point(0, 0), Point(0, 0), grid_size=-1)
    with pytest.raises(ValueError, match="grid_size"):
        compare_identity(Point(0, 0), Point(0, 0), grid_size=float("nan"))
    with pytest.raises(ValueError, match="grid_size"):
        compare_identity(Point(0, 0), Point(0, 0), grid_size=float("inf"))


def test_reasons_audit_the_verdict() -> None:
    first = Polygon(
        [(0, 0), (8, 0), (8, 8), (0, 8), (0, 0)],
        holes=[[(1, 1), (2, 1), (2, 2), (1, 2), (1, 1)]],
    )
    second = Polygon(
        [(8, 8), (8, 0), (0, 0), (0, 8), (8, 8)],
        holes=[[(2, 2), (2, 1), (1, 1), (1, 2), (2, 2)]],
    )

    match = compare_identity(first, second)

    assert isinstance(match, IdentityMatch)
    assert any("Exterior rings coincide" in reason for reason in match.reasons)
    assert any("Interior ring 0" in reason for reason in match.reasons)


def test_3d_coordinates() -> None:
    first = Polygon(
        [(0, 0, 1), (4, 0, 2), (4, 4, 3), (0, 4, 4), (0, 0, 1)],
    )
    second = Polygon(
        [(4, 0, 2), (4, 4, 3), (0, 4, 4), (0, 0, 1), (4, 0, 2)],
    )

    match = compare_identity(first, second)

    assert match.verdict is IdentityVerdict.IDENTICAL


def test_3d_coordinates_different_z() -> None:
    first = Polygon(
        [(0, 0, 1), (4, 0, 2), (4, 4, 3), (0, 4, 4), (0, 0, 1)],
    )
    second = Polygon(
        [(4, 0, 2), (4, 4, 3), (0, 4, 9), (0, 0, 1), (4, 0, 2)],
    )

    match = compare_identity(first, second)

    assert match.verdict is IdentityVerdict.DIFFERENT
