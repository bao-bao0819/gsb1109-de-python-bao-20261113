"""Property tests for station/offset sectioning."""

import math

import hypothesis.strategies as st
from hypothesis import given

from pygeoif import LinearRing
from pygeoif import LineString
from pygeoif.sectioning import BaselineSectioner
from pygeoif.sectioning import Tangent

_STEPS = st.sampled_from(
    [
        (1, 0),
        (0, 1),
        (-1, 0),
        (0, -1),
        (1, 1),
        (1, -1),
        (-1, 1),
        (-1, -1),
    ],
)
_OFFSETS = st.floats(
    min_value=-5.0,
    max_value=5.0,
    allow_nan=False,
    allow_subnormal=False,
    width=32,
)
_INTERIOR = st.floats(
    min_value=0.0,
    max_value=1.0,
    allow_nan=False,
    allow_subnormal=False,
    width=32,
    exclude_min=True,
    exclude_max=True,
)


def _walk(steps: list[tuple[int, int]]) -> list[tuple[int, int]]:
    points = [(0, 0)]
    for dx, dy in steps:
        points.append((points[-1][0] + dx, points[-1][1] + dy))
    return points


@given(st.lists(_STEPS, min_size=1, max_size=6), _INTERIOR)
def test_zero_offset_lies_on_baseline(
    steps: list[tuple[int, int]],
    fraction: float,
) -> None:
    sectioner = BaselineSectioner(LineString(_walk(steps)))

    on_line = sectioner.locate(sectioner.length * fraction, 0.0)

    assert math.isclose(sectioner.nearest_point(on_line).distance, 0.0, abs_tol=1e-6)


@given(st.lists(_STEPS, min_size=1, max_size=6), _INTERIOR, _OFFSETS)
def test_offset_is_perpendicular_to_its_segment(
    steps: list[tuple[int, int]],
    fraction: float,
    offset: float,
) -> None:
    sectioner = BaselineSectioner(LineString(_walk(steps)))
    station = sectioner.length * fraction
    section = sectioner.add_section("S", station, offset)
    base = sectioner.locate(station, 0.0)

    assert math.isclose(
        math.hypot(section.point.x - base.x, section.point.y - base.y),
        abs(offset),
        rel_tol=1e-5,
        abs_tol=1e-9,
    )
    on_segment = [
        projection
        for projection in sectioner.nearest_points(section.point.coords[0])
        if projection.segment_index == section.segment_index
    ]
    assert math.isclose(
        on_segment[0].distance,
        abs(offset),
        rel_tol=1e-5,
        abs_tol=1e-9,
    )


@given(st.lists(_STEPS, min_size=1, max_size=6), st.booleans())
def test_every_section_remains_a_tenable_match_of_its_coordinate(
    steps: list[tuple[int, int]],
    closed: bool,
) -> None:
    points = _walk(steps)
    geometry = (
        LinearRing((*points, points[0])) if closed else LineString(points)
    )
    sectioner = BaselineSectioner(geometry)
    added = []
    for index in range(4):
        fraction = (index + 1) / 5.0
        try:
            added.append(
                sectioner.add_section(
                    f"S{index}",
                    sectioner.length * fraction,
                    float(index - 2),
                ),
            )
        except ValueError:
            continue

    assert added
    for section in added:
        matches = sectioner.find_sections(section.point.coords[0])
        identifiers = {match.section.identifier for match in matches}
        assert section.identifier in identifiers
        own = next(
            match
            for match in matches
            if match.section.identifier == section.identifier
        )
        assert math.isclose(own.distance, 0.0, abs_tol=1e-6)


@given(st.lists(_STEPS, min_size=2, max_size=6))
def test_ring_chainage_wraps_to_same_point(steps: list[tuple[int, int]]) -> None:
    points = _walk(steps)
    sectioner = BaselineSectioner(LinearRing((*points, points[0])))

    start = sectioner.locate(0.0, 1.0, Tangent.OUTGOING)
    wrapped = sectioner.locate(sectioner.length, 1.0, Tangent.OUTGOING)

    assert start.coords == wrapped.coords


@given(st.lists(_STEPS, min_size=1, max_size=6))
def test_matches_are_ordered_by_distance(steps: list[tuple[int, int]]) -> None:
    sectioner = BaselineSectioner(LineString(_walk(steps)))
    for index in range(3):
        sectioner.add_section(
            f"S{index}",
            sectioner.length * (index + 1) / 4.0,
            float(index),
        )

    matches = sectioner.find_sections((100.0, 100.0))

    distances = [match.distance for match in matches]
    assert distances == sorted(distances)
    assert len(matches) == len(sectioner)
