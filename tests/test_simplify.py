"""Test line simplification and ring simplicity primitives."""

import math

import pytest

from pygeoif.functions import _point_segment_distance
from pygeoif.functions import _segments_intersect
from pygeoif.functions import is_simple_ring
from pygeoif.functions import simplify_line


def test_simplify_keeps_endpoints_and_removes_flat_points() -> None:
    line = ((0.0, 0.0), (1.0, 0.05), (2.0, 0.0), (3.0, 0.0), (4.0, 0.0))

    assert simplify_line(line, 0.1) == ((0.0, 0.0), (4.0, 0.0))


def test_simplify_keeps_points_outside_tolerance() -> None:
    line = ((0.0, 0.0), (1.0, 1.0), (2.0, 0.0), (3.0, 1.0), (4.0, 0.0))

    assert simplify_line(line, 0.5) == line


def test_simplify_zero_or_negative_tolerance_is_identity() -> None:
    line = ((0.0, 0.0), (1.0, 0.05), (2.0, 0.0))

    assert simplify_line(line, 0.0) == line
    assert simplify_line(line, -1.0) == line


def test_simplify_short_line_unchanged() -> None:
    line = ((0.0, 0.0), (1.0, 1.0))

    assert simplify_line(line, 5.0) == line


def test_simplify_preserves_named_landmark() -> None:
    line = ((0.0, 0.0), (1.0, 0.05), (2.0, 0.0), (3.0, 0.0), (4.0, 0.0))

    result = simplify_line(line, 0.1, preserve=((1.0, 0.05),))

    assert result == ((0.0, 0.0), (1.0, 0.05), (4.0, 0.0))


def test_simplify_keeps_multiple_landmarks() -> None:
    line = ((0.0, 0.0), (1.0, 0.05), (2.0, 0.05), (3.0, 0.0), (4.0, 0.0))

    result = simplify_line(
        line,
        0.1,
        preserve=((1.0, 0.05), (2.0, 0.05)),
    )

    assert result[0] == (0.0, 0.0)
    assert result[-1] == (4.0, 0.0)
    assert (1.0, 0.05) in result
    assert (2.0, 0.05) in result


def test_simplify_preserves_third_dimension() -> None:
    line = ((0.0, 0.0, 9.0), (1.0, 0.1, 8.0), (2.0, 0.0, 7.0), (3.0, 0.0, 6.0))

    result = simplify_line(line, 0.2)

    assert result == ((0.0, 0.0, 9.0), (3.0, 0.0, 6.0))


def test_simplify_removed_points_stay_within_tolerance() -> None:
    line = (
        (0.0, 0.0),
        (1.0, 0.2),
        (2.0, -0.3),
        (3.0, 0.4),
        (4.0, 0.1),
        (5.0, 0.0),
    )
    tolerance = 0.5
    result = simplify_line(line, tolerance)
    kept = set(result)

    for point in line:
        if point in kept:
            continue
        distance = min(
            _point_segment_distance(point, result[seg], result[seg + 1])
            for seg in range(len(result) - 1)
        )
        assert distance <= tolerance


def test_simplify_douglas_peucker_known_result() -> None:
    line = ((0.0, 0.0), (1.0, 0.1), (2.0, -0.1), (3.0, 5.0), (4.0, 0.0))

    assert simplify_line(line, 1.0) == (
        (0.0, 0.0),
        (2.0, -0.1),
        (3.0, 5.0),
        (4.0, 0.0),
    )


def test_point_segment_distance_on_projection() -> None:
    assert _point_segment_distance((0.0, 1.0), (0.0, 0.0), (2.0, 0.0)) == 1.0


def test_point_segment_distance_clamped_to_endpoints() -> None:
    assert _point_segment_distance((3.0, 0.0), (0.0, 0.0), (2.0, 0.0)) == 1.0


def test_point_segment_distance_degenerate_segment() -> None:
    assert _point_segment_distance((3.0, 4.0), (0.0, 0.0), (0.0, 0.0)) == 5.0


def test_simple_square_ring() -> None:
    ring = ((0.0, 0.0), (0.0, 2.0), (2.0, 2.0), (2.0, 0.0), (0.0, 0.0))

    assert is_simple_ring(ring) is True


def test_simple_open_ring_accepted() -> None:
    ring = ((0.0, 0.0), (0.0, 2.0), (2.0, 2.0), (2.0, 0.0))

    assert is_simple_ring(ring) is True


def test_bowtie_ring_is_not_simple() -> None:
    ring = ((0.0, 0.0), (2.0, 2.0), (0.0, 2.0), (2.0, 0.0), (0.0, 0.0))

    assert is_simple_ring(ring) is False


def test_ring_touching_itself_is_not_simple() -> None:
    ring = (
        (0.0, 0.0),
        (4.0, 0.0),
        (4.0, 4.0),
        (2.0, 2.0),
        (0.0, 4.0),
        (0.0, 2.0),
        (2.0, 2.0),
        (0.0, 0.0),
    )

    assert is_simple_ring(ring) is False


def test_segments_intersect_detects_crossing() -> None:
    assert _segments_intersect(
        (0.0, 0.0),
        (2.0, 2.0),
        (0.0, 2.0),
        (2.0, 0.0),
    )


def test_segments_intersect_detects_collinear_overlap() -> None:
    assert _segments_intersect(
        (0.0, 0.0),
        (2.0, 0.0),
        (1.0, 0.0),
        (3.0, 0.0),
    )


def test_segments_meeting_at_endpoint_intersect() -> None:
    assert _segments_intersect(
        (0.0, 0.0),
        (1.0, 1.0),
        (1.0, 1.0),
        (2.0, 0.0),
    )


def test_disjoint_segments_do_not_intersect() -> None:
    assert not _segments_intersect(
        (0.0, 0.0),
        (1.0, 1.0),
        (3.0, 3.0),
        (4.0, 4.0),
    )


@pytest.mark.parametrize(
    "ring",
    [
        (),
        ((0.0, 0.0),),
        ((0.0, 0.0), (1.0, 1.0)),
    ],
)
def test_degenerate_rings_are_simple(ring: tuple[tuple[float, float], ...]) -> None:
    assert is_simple_ring(ring) is True


def test_distance_uses_only_two_dimensions() -> None:
    assert math.isclose(
        _point_segment_distance(
            (0.0, 1.0, 99.0),
            (0.0, 0.0, 0.0),
            (2.0, 0.0, 0.0),
        ),
        1.0,
    )
