"""Tests for the left/right parcel catalog (宗地左右归属核对小样)."""

from fractions import Fraction

from left_right_catalog.core import CCW
from left_right_catalog.core import CW
from left_right_catalog.core import Catalog
from left_right_catalog.core import CollinearInsertion
from left_right_catalog.core import build_catalog
from left_right_catalog.core import flip_rings
from left_right_catalog.core import normalize_parcels
from left_right_catalog.core import signed_area2
from left_right_catalog.report import format_report
from left_right_catalog.sample import SAMPLE_PARCELS
from left_right_catalog.sample import SAMPLE_QUERIES


def _point(x: int, y: int) -> tuple[Fraction, Fraction]:
    return (Fraction(x), Fraction(y))


def _sample_catalog() -> Catalog:
    return build_catalog(normalize_parcels(SAMPLE_PARCELS))


class TestOrientationConvention:
    """Outer rings CCW, holes CW: a ring and its holes run opposite."""

    def test_outer_rings_are_ccw_after_normalization(self) -> None:
        normalized = normalize_parcels(SAMPLE_PARCELS)
        for parcel in normalized.parcels:
            assert signed_area2(parcel.outer) > 0

    def test_holes_are_cw_after_normalization(self) -> None:
        normalized = normalize_parcels(SAMPLE_PARCELS)
        for parcel in normalized.parcels:
            for hole in parcel.holes:
                assert signed_area2(hole) < 0

    def test_convention_violations_are_reported(self) -> None:
        normalized = normalize_parcels(SAMPLE_PARCELS)
        fixes = {(f.parcel_id, f.ring_name): f.was for f in normalized.fixes}
        assert fixes == {("B", "outer"): CW, ("C", "hole#1"): CCW}


class TestSharedEdges:
    """Adjacent parcels traverse their common edge in opposite directions."""

    def test_shared_boundaries_have_one_parcel_per_side(self) -> None:
        catalog = _sample_catalog()
        shared = {
            (seg.start, seg.end): (seg.left, seg.right)
            for seg in catalog.shared_boundaries()
        }
        assert shared[(_point(2, 0), _point(2, 1))] == (
            frozenset({"A"}),
            frozenset({"B"}),
        )
        assert shared[(_point(2, 1), _point(2, 2))] == (
            frozenset({"A"}),
            frozenset({"B"}),
        )
        assert shared[(_point(0, 2), _point(2, 2))] == (
            frozenset({"C"}),
            frozenset({"A"}),
        )
        assert shared[(_point(2, 2), _point(4, 2))] == (
            frozenset({"C"}),
            frozenset({"B"}),
        )

    def test_extra_collinear_vertex_is_absorbed(self) -> None:
        catalog = _sample_catalog()
        insertion = CollinearInsertion("B", _point(2, 1), "A")
        assert insertion in catalog.insertions
        # The split edge A|B raises no same-side conflict at all.
        for conflict in catalog.conflicts(min_claimants=2):
            assert not (conflict.start[0] == conflict.end[0] == Fraction(2))


class TestConflicts:
    """Same-side claims by several parcels are reported as conflicts."""

    def test_two_parcel_same_side_conflicts(self) -> None:
        catalog = _sample_catalog()
        found = {
            (c.start, c.end, c.side): c.parcels
            for c in catalog.conflicts(min_claimants=2)
        }
        assert found[(_point(11, 0), _point(12, 0), "left")] == ("X", "Y")
        assert found[(_point(13, 0), _point(14, 0), "left")] == ("X", "Y")
        assert found[(_point(11, 2), _point(14, 2), "right")] == ("X", "Y")

    def test_shortest_segment_with_three_same_side_claimants(self) -> None:
        catalog = _sample_catalog()
        shortest = catalog.shortest_conflict(min_claimants=3)
        assert shortest is not None
        assert shortest.start == _point(12, 0)
        assert shortest.end == _point(13, 0)
        assert shortest.side == "left"
        assert shortest.parcels == ("X", "Y", "Z")
        assert shortest.length2 == 1


class TestQuery:
    """Left/right parcel numbers are directly queryable along an edge."""

    def test_left_and_right_along_shared_edge(self) -> None:
        catalog = _sample_catalog()
        claim = catalog.query((2, 0), (2, 2))
        assert claim.left == frozenset({"A"})
        assert claim.right == frozenset({"B"})

    def test_reverse_direction_swaps_sides(self) -> None:
        catalog = _sample_catalog()
        claim = catalog.query((2, 2), (2, 0))
        assert claim.left == frozenset({"B"})
        assert claim.right == frozenset({"A"})

    def test_long_edge_aggregates_atomic_segments(self) -> None:
        catalog = _sample_catalog()
        claim = catalog.query((0, 2), (4, 2))
        assert claim.left == frozenset({"C"})
        assert claim.right == frozenset({"A", "B"})


class TestFlipInvariance:
    """Flipping every input ring keeps the confirmable left/right catalog."""

    def test_catalog_is_identical_after_flipping_all_rings(self) -> None:
        catalog = _sample_catalog()
        flipped = build_catalog(normalize_parcels(flip_rings(SAMPLE_PARCELS)))
        assert flipped.signature() == catalog.signature()

    def test_query_results_follow_the_agreed_orientation(self) -> None:
        flipped = build_catalog(normalize_parcels(flip_rings(SAMPLE_PARCELS)))
        claim = flipped.query((2, 0), (2, 2))
        assert claim.left == frozenset({"A"})
        assert claim.right == frozenset({"B"})


class TestReport:
    """The printable report mentions the key findings."""

    def test_report_mentions_shortest_triple_conflict(self) -> None:
        report = format_report(SAMPLE_PARCELS, SAMPLE_QUERIES)
        assert "(12,0)->(13,0)" in report
        assert "最短边段" in report

    def test_report_confirms_flip_invariance(self) -> None:
        report = format_report(SAMPLE_PARCELS, SAMPLE_QUERIES)
        assert "左右归属完全一致" in report
