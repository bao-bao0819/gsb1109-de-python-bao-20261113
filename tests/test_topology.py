"""Test topology preserving simplification of shared polygon boundaries."""

from pygeoif.functions import is_simple_ring
from pygeoif.functions import simplify_line
from pygeoif.topology import simplify_polygons


def _arc(
    ring: tuple[tuple[float, float], ...],
    start: tuple[float, float],
    end: tuple[float, float],
) -> tuple[tuple[float, float], ...]:
    points = ring[:-1]
    first = points.index(start)
    last = points.index(end)
    if first < last:
        return tuple(points[first : last + 1])
    return tuple(points[first:] + points[: last + 1])


def test_node_count_is_reduced() -> None:
    parcel_a = (
        (
            (0.0, 0.0),
            (0.0, 1.0),
            (0.5, 1.0),
            (1.0, 1.0),
            (2.0, 1.0),
            (2.0, 0.0),
            (0.0, 0.0),
        ),
    )
    parcel_b = (
        ((2.0, 2.0), (2.0, 1.0), (1.0, 1.0), (0.0, 1.0), (0.0, 2.0), (2.0, 2.0)),
    )

    result = simplify_polygons([parcel_a, parcel_b], 0.1)

    assert len(result[0][0]) < len(parcel_a[0])
    assert len(result[1][0]) < len(parcel_b[0])


def test_shared_edge_simplified_once_in_opposite_direction() -> None:
    start = (0.0, 1.0)
    middle = (1.0, 2.0)
    extra = (0.5, 1.5)
    end = (2.0, 1.0)
    parcel_a = (((0.0, 0.0), start, extra, middle, end, (2.0, 0.0), (0.0, 0.0)),)
    parcel_b = (((2.0, 1.0), middle, extra, start, (0.0, 3.0), (2.0, 3.0), (2.0, 1.0)),)

    result = simplify_polygons([parcel_a, parcel_b], 0.1)
    ring_a = result[0][0]
    ring_b = result[1][0]

    arc_a = _arc(ring_a, start, end)
    arc_b = _arc(ring_b, end, start)

    assert arc_a == tuple(reversed(arc_b))
    assert arc_a == (start, middle, end)
    assert is_simple_ring(ring_a)
    assert is_simple_ring(ring_b)


def test_extra_sampling_on_one_side_stays_consistent() -> None:
    start = (0.0, 1.0)
    end = (2.0, 1.0)
    parcel_a = (
        (
            (0.0, 0.0),
            start,
            (0.5, 1.0),
            (1.0, 1.0),
            (1.5, 1.0),
            end,
            (2.0, 0.0),
            (0.0, 0.0),
        ),
    )
    parcel_b = (((2.0, 2.0), end, start, (0.0, 2.0), (2.0, 2.0)),)

    result = simplify_polygons([parcel_a, parcel_b], 0.1)

    arc_a = _arc(result[0][0], start, end)
    arc_b = _arc(result[1][0], end, start)

    assert arc_a == tuple(reversed(arc_b))
    assert arc_a == (start, end)


def test_reversed_registration_matches_coordinate_for_coordinate() -> None:
    start = (0.0, 1.0)
    middle = (1.0, 2.0)
    end = (2.0, 1.0)
    parcel_a = (((0.0, 0.0), start, middle, end, (2.0, 0.0), (0.0, 0.0)),)
    parcel_b = (((2.0, 1.0), end, middle, start, (0.0, 3.0), (2.0, 3.0), (2.0, 1.0)),)

    result = simplify_polygons([parcel_a, parcel_b], 0.1)

    assert _arc(result[0][0], start, end) == (
        start,
        middle,
        end,
    )
    assert _arc(result[1][0], end, start) == (
        end,
        middle,
        start,
    )


def test_shared_edge_landmark_is_never_removed() -> None:
    start = (0.0, 1.0)
    marker = (1.0, 1.0)
    end = (2.0, 1.0)
    parcel_a = (((0.0, 0.0), start, marker, end, (2.0, 0.0), (0.0, 0.0)),)
    parcel_b = (((2.0, 2.0), end, marker, start, (0.0, 2.0), (2.0, 2.0)),)

    result = simplify_polygons([parcel_a, parcel_b], 5.0, preserve=(marker,))

    all_points = [point for polygon in result for ring in polygon for point in ring]
    assert marker in all_points
    arc_a = _arc(result[0][0], start, end)
    arc_b = _arc(result[1][0], end, start)
    assert arc_a == tuple(reversed(arc_b))


def test_private_landmark_is_kept() -> None:
    marker = (1.0, 0.1)
    parcel = (((0.0, 0.0), marker, (2.0, 0.0), (2.0, 2.0), (0.0, 2.0), (0.0, 0.0)),)

    result = simplify_polygons([parcel], 5.0, preserve=(marker,))

    assert marker in result[0][0]


def test_self_intersecting_simplification_is_repaired() -> None:
    ring = (
        (3, 10),
        (10, 1),
        (6, 2),
        (12, 0),
        (5, 2),
        (2, 4),
        (3, 10),
    )
    naive = simplify_line(ring, 1.0)
    assert is_simple_ring(naive) is False

    result = simplify_polygons([(ring,)], 1.0)[0][0]

    assert is_simple_ring(result)
    assert (5, 2) in result
    assert len(result) > len(naive)


def test_narrow_corridor_does_not_become_self_intersecting() -> None:
    corridor = (
        (0.0, 0.0),
        (10.0, 0.0),
        (10.0, 2.0),
        (1.0, 2.0),
        (1.0, 4.0),
        (10.0, 4.0),
        (10.0, 6.0),
        (0.0, 6.0),
        (0.0, 0.0),
    )

    result = simplify_polygons([(corridor,)], 3.0)[0][0]

    assert is_simple_ring(result)
    assert len(result) < len(corridor)


def test_shared_hole_ring_is_reversed_identically() -> None:
    outer = ((0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0), (0.0, 0.0))
    hole = ((3.0, 3.0), (7.0, 3.0), (7.0, 7.0), (3.0, 7.0), (3.0, 3.0))
    parcel_with_hole = (outer, [hole])
    neighbour = (((3.0, 3.0), (3.0, 7.0), (7.0, 7.0), (7.0, 3.0), (3.0, 3.0)),)

    result = simplify_polygons([parcel_with_hole, neighbour], 0.5)

    hole_ring = result[0][1]
    neighbour_ring = result[1][0]
    assert tuple(reversed(hole_ring)) == neighbour_ring
    assert is_simple_ring(hole_ring)
    assert is_simple_ring(neighbour_ring)


def test_three_parcels_keep_pairwise_shared_edges_consistent() -> None:
    left = (((0.0, 0.0), (0.0, 2.0), (1.0, 2.0), (1.0, 0.0), (0.0, 0.0)),)
    middle = (((1.0, 0.0), (1.0, 2.0), (2.0, 2.0), (2.0, 0.0), (1.0, 0.0)),)
    right = (((2.0, 0.0), (2.0, 2.0), (3.0, 2.0), (3.0, 0.0), (2.0, 0.0)),)

    result = simplify_polygons([left, middle, right], 0.1)
    ring_left, ring_middle, ring_right = (polygon[0] for polygon in result)

    assert _arc(ring_left, (1.0, 2.0), (1.0, 0.0)) == tuple(
        reversed(_arc(ring_middle, (1.0, 0.0), (1.0, 2.0))),
    )
    assert _arc(ring_middle, (2.0, 2.0), (2.0, 0.0)) == tuple(
        reversed(_arc(ring_right, (2.0, 0.0), (2.0, 2.0))),
    )
    for polygon in result:
        assert is_simple_ring(polygon[0])


def test_disjoint_polygons_simplify_independently() -> None:
    first = (((0.0, 0.0), (1.0, 0.05), (2.0, 0.0), (2.0, 1.0), (0.0, 1.0), (0.0, 0.0)),)
    second = (
        ((5.0, 5.0), (6.0, 5.05), (7.0, 5.0), (7.0, 6.0), (5.0, 6.0), (5.0, 5.0)),
    )

    result = simplify_polygons([first, second], 0.1)

    assert len(result[0][0]) < len(first[0])
    assert len(result[1][0]) < len(second[0])
    assert is_simple_ring(result[0][0])
    assert is_simple_ring(result[1][0])


def test_zero_tolerance_returns_identical_rings() -> None:
    parcel_a = (
        ((0.0, 0.0), (0.0, 1.0), (1.0, 1.0), (2.0, 1.0), (2.0, 0.0), (0.0, 0.0)),
    )
    parcel_b = (
        ((2.0, 2.0), (2.0, 1.0), (1.0, 1.0), (0.0, 1.0), (0.0, 2.0), (2.0, 2.0)),
    )

    result = simplify_polygons([parcel_a, parcel_b], 0.0)

    assert result[0][0] == parcel_a[0]
    assert result[1][0] == parcel_b[0]
