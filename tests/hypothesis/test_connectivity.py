"""Hypothesis test cases for the connectivity module."""

import math

from hypothesis import assume
from hypothesis import given
from hypothesis import strategies as st

from pygeoif import connectivity

finite_float = st.floats(
    allow_nan=False,
    allow_infinity=False,
    width=32,
    max_value=1e6,
    min_value=-1e6,
)


def line3d() -> st.SearchStrategy[list[tuple[float, float, float]]]:
    """Generate a simple three dimensional line."""
    return st.lists(
        st.tuples(finite_float, finite_float, finite_float),
        min_size=2,
        max_size=6,
    ).filter(lambda coords: len(set(coords)) >= 2)


@given(
    first=line3d(),
    second=line3d(),
    tolerance=st.floats(
        min_value=0,
        max_value=100,
        allow_nan=False,
        allow_infinity=False,
        width=32,
    ),
)
def test_connections_are_direction_independent(
    first: list[tuple[float, float, float]],
    second: list[tuple[float, float, float]],
    tolerance: float,
) -> None:
    forward = connectivity.node_connectivity(
        first,
        second,
        connect_tolerance=tolerance,
    )
    reversed_first = connectivity.node_connectivity(
        first[::-1],
        second,
        connect_tolerance=tolerance,
    )
    reversed_second = connectivity.node_connectivity(
        first,
        second[::-1],
        connect_tolerance=tolerance,
    )

    assert forward == reversed_first == reversed_second


@given(
    first=line3d(),
    second=line3d(),
    tolerance=st.floats(
        min_value=0,
        max_value=100,
        allow_nan=False,
        allow_infinity=False,
        width=32,
    ),
)
def test_export_is_direction_independent(
    first: list[tuple[float, float, float]],
    second: list[tuple[float, float, float]],
    tolerance: float,
) -> None:
    forward = connectivity.export_connectivity(
        {"first": first, "second": second},
        connect_tolerance=tolerance,
    )
    reversed_first = connectivity.export_connectivity(
        {"first": first[::-1], "second": second},
        connect_tolerance=tolerance,
    )

    assert forward.as_dict() == reversed_first.as_dict()


@given(
    z_a=finite_float,
    z_b=finite_float,
    connect=st.floats(
        min_value=0,
        max_value=100,
        allow_nan=False,
        allow_infinity=False,
        width=32,
    ),
    separation=st.floats(
        min_value=0,
        max_value=100,
        allow_nan=False,
        allow_infinity=False,
        width=32,
    ),
)
def test_classify_follows_the_band(
    z_a: float,
    z_b: float,
    connect: float,
    separation: float,
) -> None:
    assume(separation >= connect)
    status = connectivity.classify_connection(
        z_a,
        z_b,
        connect_tolerance=connect,
        separation_tolerance=separation,
    )
    difference = abs(z_a - z_b)

    if difference <= connect:
        assert status is connectivity.ConnectionStatus.CONNECTED
    elif difference > separation:
        assert status is connectivity.ConnectionStatus.SEPARATED
    else:
        assert status is connectivity.ConnectionStatus.REVIEW


@given(
    z=finite_float,
    tolerance=st.floats(
        min_value=0,
        max_value=100,
        allow_nan=False,
        allow_infinity=False,
        width=32,
    ),
)
def test_same_elevation_is_always_connected(z: float, tolerance: float) -> None:
    assert (
        connectivity.classify_connection(z, z, connect_tolerance=tolerance)
        is connectivity.ConnectionStatus.CONNECTED
    )


@given(
    distance=st.floats(
        min_value=0.01,
        max_value=9.99,
        allow_nan=False,
        allow_infinity=False,
    ),
    rise=finite_float,
)
def test_crossing_z_is_linear(distance: float, rise: float) -> None:
    first = [(0.0, 0.0, 0.0), (10.0, 0.0, rise)]
    second = [(distance, -1.0, 0.0), (distance, 1.0, 0.0)]

    intersection = connectivity.line_intersections(first, second)[0]

    assert math.isclose(intersection.z_a, rise * distance / 10, abs_tol=1e-4)


@given(
    offset=st.floats(
        min_value=-100,
        max_value=100,
        allow_nan=False,
        allow_infinity=False,
        width=32,
    ),
)
def test_crossing_xy_is_on_both_lines(offset: float) -> None:
    first = [(0.0, 0.0, 0.0), (10.0, 0.0, 10.0)]
    second = [(5.0, -10.0, offset), (5.0, 10.0, offset)]

    intersection = connectivity.line_intersections(first, second)[0]

    assert intersection.xy == (5.0, 0.0)
    assert intersection.fraction_a == 0.5
    assert intersection.fraction_b == 0.5
