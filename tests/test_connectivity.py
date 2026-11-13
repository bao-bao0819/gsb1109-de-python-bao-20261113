"""Test road connectivity between three dimensional lines."""

import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from pygeoif import LineString
from pygeoif import connectivity
from pygeoif.exceptions import DimensionError


def test_intersection_is_interpolated_along_both_lines() -> None:
    first = [(0, 0, 10), (10, 0, 10)]
    second = [(5, -5, 12), (5, 5, 8)]

    intersection = connectivity.line_intersections(first, second)[0]

    assert intersection.xy == (5.0, 0.0)
    assert intersection.z_a == 10.0
    assert intersection.z_b == 10.0
    assert intersection.distance_a == 5.0
    assert intersection.distance_b == 5.0
    assert intersection.fraction_a == 0.5
    assert intersection.fraction_b == 0.5


def test_mid_segment_z_is_not_nearest_vertex() -> None:
    first = [(0, 0, 0), (10, 0, 10)]
    second = [(5, -1, 0), (5, 1, 0)]

    intersection = connectivity.line_intersections(first, second)[0]

    assert intersection.z_a == 5.0
    assert intersection.z_b == 0.0


def test_intersection_at_vertex() -> None:
    first = [(0, 0, 1), (10, 0, 2)]
    second = [(0, 0, 3), (0, 10, 4)]

    intersection = connectivity.line_intersections(first, second)[0]

    assert intersection.kind == "touch"
    assert intersection.z_a == 1.0
    assert intersection.z_b == 3.0


def test_collinear_overlap_returns_endpoints_of_overlap() -> None:
    first = [(0, 0, 1), (10, 0, 1)]
    second = [(2, 0, 1), (8, 0, 1)]

    intersections = connectivity.line_intersections(first, second)

    assert [i.distance_a for i in intersections] == [2.0, 8.0]
    assert all(i.kind == "overlap" for i in intersections)


def test_no_intersection() -> None:
    first = [(0, 0, 1), (10, 0, 1)]
    second = [(0, 5, 1), (10, 5, 1)]

    assert connectivity.line_intersections(first, second) == ()


def test_parallel_lines_without_distance() -> None:
    first = [(0, 0, 1), (10, 0, 2)]
    second = [(0, 1, 1), (10, 1, 2)]

    assert connectivity.line_intersections(first, second) == ()


@pytest.mark.parametrize(
    ("z_b", "expected"),
    [
        (10.0, connectivity.ConnectionStatus.CONNECTED),
        (10.4, connectivity.ConnectionStatus.CONNECTED),
        (10.5, connectivity.ConnectionStatus.CONNECTED),
        (10.6, connectivity.ConnectionStatus.REVIEW),
        (11.0, connectivity.ConnectionStatus.REVIEW),
        (11.5, connectivity.ConnectionStatus.REVIEW),
        (11.6, connectivity.ConnectionStatus.SEPARATED),
        (14.0, connectivity.ConnectionStatus.SEPARATED),
    ],
)
def test_classify_connection_band(
    z_b: float,
    expected: connectivity.ConnectionStatus,
) -> None:
    assert (
        connectivity.classify_connection(
            10.0,
            z_b,
            connect_tolerance=0.5,
            separation_tolerance=1.5,
        )
        == expected
    )


def test_classify_uses_absolute_difference() -> None:
    assert (
        connectivity.classify_connection(10.0, 9.6, connect_tolerance=0.5)
        == connectivity.ConnectionStatus.CONNECTED
    )


def test_separation_tolerance_defaults_to_twice_connect_tolerance() -> None:
    assert (
        connectivity.classify_connection(10.0, 12.0, connect_tolerance=0.5)
        == connectivity.ConnectionStatus.SEPARATED
    )
    assert (
        connectivity.classify_connection(10.0, 11.0, connect_tolerance=0.5)
        == connectivity.ConnectionStatus.REVIEW
    )


def test_classify_rejects_negative_tolerance() -> None:
    with pytest.raises(ValueError, match=r"connect_tolerance"):
        connectivity.classify_connection(1.0, 2.0, connect_tolerance=-1.0)


def test_classify_rejects_inverted_band() -> None:
    with pytest.raises(ValueError, match=r"separation_tolerance"):
        connectivity.classify_connection(
            1.0,
            2.0,
            connect_tolerance=1.0,
            separation_tolerance=0.5,
        )


def test_classify_rejects_nan_tolerance() -> None:
    with pytest.raises(ValueError, match=r"connect_tolerance"):
        connectivity.classify_connection(1.0, 2.0, connect_tolerance=math.nan)


def test_node_connectivity_uses_interpolated_elevation() -> None:
    first = [(0, 0, 0), (10, 0, 10)]
    second = [(5, -1, 0), (5, 1, 0)]

    connection = connectivity.node_connectivity(
        first,
        second,
        connect_tolerance=0.5,
    )[0]

    assert connection.z_a == 5.0
    assert connection.z_b == 0.0
    assert connection.status == connectivity.ConnectionStatus.SEPARATED
    assert connection.kind == "crossing"


def test_node_connectivity_interpolates_both_lines() -> None:
    first = [(0, 0, 0), (10, 0, 20)]
    second = [(5, -1, 0), (5, 1, 20)]

    connection = connectivity.node_connectivity(first, second)[0]

    assert connection.z_a == 10.0
    assert connection.z_b == 10.0
    assert connection.status == connectivity.ConnectionStatus.CONNECTED


def test_node_connection_helper_properties() -> None:
    first = [(0, 0, 0), (10, 0, 0)]
    second = [(5, -1, 0), (5, 1, 4)]

    connection = connectivity.node_connectivity(
        first,
        second,
        connect_tolerance=0.5,
        separation_tolerance=10.0,
    )[0]

    assert connection.z_difference == 2.0
    assert not connection.is_connected
    assert not connection.is_separated
    assert connection.needs_review


def test_reversing_either_line_does_not_change_connections() -> None:
    first = [(0, 0, 0), (10, 0, 10)]
    second = [(5, -5, 12), (5, 5, 8)]

    forward = connectivity.node_connectivity(
        first,
        second,
        connect_tolerance=0.5,
    )
    reversed_first = connectivity.node_connectivity(
        first[::-1],
        second,
        connect_tolerance=0.5,
    )
    reversed_second = connectivity.node_connectivity(
        first,
        second[::-1],
        connect_tolerance=0.5,
    )
    both_reversed = connectivity.node_connectivity(
        first[::-1],
        second[::-1],
        connect_tolerance=0.5,
    )

    assert forward == reversed_first == reversed_second == both_reversed


def test_reversing_does_not_change_export() -> None:
    first = [(0, 0, 0), (10, 0, 10)]
    second = [(5, -5, 12), (5, 5, 8)]

    forward = connectivity.export_connectivity(
        {"first": first, "second": second},
        connect_tolerance=0.5,
    )
    reversed_first = connectivity.export_connectivity(
        {"first": first[::-1], "second": second},
        connect_tolerance=0.5,
    )
    reversed_second = connectivity.export_connectivity(
        {"first": first, "second": second[::-1]},
        connect_tolerance=0.5,
    )

    assert forward.as_dict() == reversed_first.as_dict() == reversed_second.as_dict()


def test_export_projects_coordinates_to_two_dimensions() -> None:
    export = connectivity.export_connectivity(
        {"first": [(0, 0, 1), (10, 0, 2)], "second": [(0, 5, 1), (10, 5, 2)]},
    )

    for feature in export.features:
        assert feature["type"] == "Feature"
        assert all(len(coord) == 2 for coord in feature["geometry"]["coordinates"])


def test_export_keeps_original_z_in_nodes() -> None:
    export = connectivity.export_connectivity(
        {"first": [(0, 0, 1), (10, 0, 11)], "second": [(5, -5, 0), (5, 5, 20)]},
    )

    crossing = next(node for node in export.nodes if node.kind == "crossing")
    assert crossing.z == 6.0
    assert crossing.fraction == 0.5


def test_export_splits_line_at_crossing() -> None:
    export = connectivity.export_connectivity(
        {"first": [(0, 0, 1), (10, 0, 1)], "second": [(5, -5, 1), (5, 5, 1)]},
    )

    first_edges = [
        edge
        for edge in export.edges
        if edge.kind == "line" and edge.source.startswith("first")
    ]
    assert len(first_edges) == 2

    transfer = [edge for edge in export.edges if edge.kind == "transfer"]
    assert len(transfer) == 1
    assert transfer[0].status == connectivity.ConnectionStatus.CONNECTED


def test_can_travel_across_connected_crossing() -> None:
    export = connectivity.export_connectivity(
        {
            "a": [(0, 0, 0), (10, 0, 0)],
            "b": [(5, -5, 0), (5, 5, 0)],
            "c": [(15, -5, 0), (15, 5, 0)],
        },
        connect_tolerance=0.1,
    )

    assert export.can_travel("a", "b")
    assert not export.can_travel("a", "c")


def test_review_crossings_are_not_traversable_by_default() -> None:
    export = connectivity.export_connectivity(
        {
            "a": [(0, 0, 0), (10, 0, 0)],
            "b": [(5, -5, 0.3), (5, 5, 0.3)],
        },
        connect_tolerance=0.1,
        separation_tolerance=1.0,
    )

    assert not export.can_travel("a", "b")
    assert export.can_travel("a", "b", allow_review=True)
    assert export.line_adjacency() == {"a": set(), "b": set()}
    assert export.line_adjacency(allow_review=True) == {"a": {"b"}, "b": {"a"}}


def test_travel_through_review_chain() -> None:
    export = connectivity.export_connectivity(
        {
            "a": [(0, 0, 0), (10, 0, 0)],
            "b": [(5, -5, 0.3), (5, 5, 0.3)],
            "c": [(-5, 5, 0), (15, 5, 0)],
        },
        connect_tolerance=0.1,
        separation_tolerance=1.0,
    )

    assert not export.can_travel("a", "c")
    assert export.can_travel("a", "c", allow_review=True)


def test_can_travel_rejects_unknown_line() -> None:
    export = connectivity.export_connectivity(
        {"a": [(0, 0, 0), (10, 0, 0)], "b": [(5, -5, 0), (5, 5, 0)]},
    )

    with pytest.raises(KeyError, match=r"unknown"):
        export.can_travel("a", "unknown")


def test_geo_interface_contains_connectivity_payload() -> None:
    export = connectivity.export_connectivity(
        {"a": [(0, 0, 0), (10, 0, 0)], "b": [(5, -5, 2), (5, 5, 2)]},
        connect_tolerance=0.1,
    )

    interface = export.__geo_interface__

    assert interface["type"] == "FeatureCollection"
    assert interface["connectivity"]["connections"][0]["status"] == "separated"
    assert interface["connectivity"]["connections"][0]["z_difference"] == 2.0
    assert interface["features"][0]["properties"]["id"] == "a"


def test_sequence_input_gets_generated_line_ids() -> None:
    export = connectivity.export_connectivity(
        [[(0, 0, 0), (10, 0, 0)], [(5, -5, 0), (5, 5, 0)]],
    )

    assert export.line_ids == ("line-0", "line-1")
    assert export.can_travel("line-0", "line-1")


def test_requires_z_value() -> None:
    with pytest.raises(DimensionError, match=r"three dimensional"):
        connectivity.node_connectivity(
            [(0, 0), (10, 0)],
            [(5, -5), (5, 5)],
        )


def test_requires_two_distinct_coordinates() -> None:
    with pytest.raises(ValueError, match=r"two distinct"):
        connectivity.node_connectivity(
            [(0, 0, 0), (0, 0, 0)],
            [(5, -5, 0), (5, 5, 0)],
        )


def test_rejects_finite_coordinates() -> None:
    with pytest.raises(DimensionError, match=r"finite"):
        connectivity.node_connectivity(
            [(0, 0, math.nan), (10, 0, 0)],
            [(5, -5, 0), (5, 5, 0)],
        )


def test_accepts_linestring_objects() -> None:
    first = LineString([(0, 0, 0), (10, 0, 0)])
    second = LineString([(5, -5, 0), (5, 5, 0)])

    connection = connectivity.node_connectivity(first, second)[0]

    assert connection.status == connectivity.ConnectionStatus.CONNECTED


def test_custom_line_ids_appear_in_nodes() -> None:
    connection = connectivity.node_connectivity(
        [(0, 0, 0), (10, 0, 0)],
        [(5, -5, 0), (5, 5, 0)],
        line_a="highway",
        line_b="ramp",
    )[0]

    assert connection.node_a.startswith("highway")
    assert connection.node_b.startswith("ramp")


def test_empty_line_sequence_is_rejected() -> None:
    with pytest.raises(ValueError, match=r"At least one line"):
        connectivity.export_connectivity([])


@given(
    z_a=st.floats(allow_nan=False, allow_infinity=False, width=32),
    z_b=st.floats(allow_nan=False, allow_infinity=False, width=32),
    tolerance=st.floats(
        min_value=0,
        max_value=1000,
        allow_nan=False,
        allow_infinity=False,
        width=32,
    ),
)
def test_classify_is_symmetric(
    z_a: float,
    z_b: float,
    tolerance: float,
) -> None:
    assert connectivity.classify_connection(
        z_a,
        z_b,
        connect_tolerance=tolerance,
    ) == connectivity.classify_connection(
        z_b,
        z_a,
        connect_tolerance=tolerance,
    )


@given(
    distance=st.floats(min_value=0, max_value=10, width=32),
    rise=st.floats(min_value=-100, max_value=100, width=32),
)
def test_intersection_z_matches_linear_interpolation(
    distance: float,
    rise: float,
) -> None:
    first = [(0, 0, 0), (10, 0, rise)]
    second = [(distance, -1, 0), (distance, 1, 0)]

    intersection = connectivity.line_intersections(first, second)[0]

    assert math.isclose(intersection.z_a, rise * distance / 10, abs_tol=1e-6)
    assert math.isclose(intersection.distance_a, distance, abs_tol=1e-6)


def test_vertical_line_touches_planar_line_at_start() -> None:
    vertical = [(0, 0, 0), (0, 0, 10)]
    planar = [(0, 0, 0), (5, 0, 0)]

    connection = connectivity.node_connectivity(vertical, planar)[0]

    assert connection.kind == "touch"
    assert connection.node_a == "a:start"
    assert connection.node_b == "b:start"


def test_near_collinear_but_not_parallel_is_a_crossing() -> None:
    first = [(0, 0, 0), (10, 0, 0)]
    second = [(5, 1e-10, 0), (5, 20, 0)]

    intersections = connectivity.line_intersections(first, second)

    assert intersections == ()


def test_transfer_edges_reference_exported_nodes() -> None:
    export = connectivity.export_connectivity(
        {"a": [(0, 0, 0), (10, 0, 0)], "b": [(10, 0, 0), (10, 10, 0)]},
    )

    node_ids = {node.id for node in export.nodes}
    for edge in export.edges:
        assert edge.source in node_ids
        assert edge.target in node_ids


def test_crossing_snapped_to_start_keeps_transfer_valid() -> None:
    export = connectivity.export_connectivity(
        {"a": [(0, 0, 0), (10, 0, 0)], "b": [(0, 5, 0.5), (0, -5, 0.5)]},
        connect_tolerance=0.1,
        separation_tolerance=1.0,
    )

    transfer = next(edge for edge in export.edges if edge.kind == "transfer")
    assert transfer.source == "a:start"
    assert transfer.status == connectivity.ConnectionStatus.REVIEW
