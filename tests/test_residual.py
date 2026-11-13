"""Tests for residual centroid feasibility analysis."""

import random
from fractions import Fraction

import pytest

from pygeoif.residual import CandidateResult
from pygeoif.residual import Subarea
from pygeoif.residual import Total
from pygeoif.residual import Witness
from pygeoif.residual import _area_box
from pygeoif.residual import analyze
from pygeoif.residual import rounding_interval

STEP = 1


def _total() -> Total:
    # True total: area 100, centroid (10, 10).
    return Total(area=100, x=10, y=10, area_step=STEP, x_step=STEP, y_step=STEP)


def _subareas() -> list[Subarea]:
    # True subareas: 60 at (0, 0) and 25 at (4, 20); residual is 15 at (60, 100/3).
    return [
        Subarea(60, 0, 0, STEP, STEP, STEP),
        Subarea(25, 4, 20, STEP, STEP, STEP),
    ]


def _candidates() -> dict[str, list[tuple[float, float]]]:
    return {
        "near": [(55, 30), (65, 30), (65, 36), (55, 36)],
        "far": [(-5, 45), (5, 45), (5, 55), (-5, 55)],
        "huge": [(-1000, -1000), (1000, -1000), (1000, 1000), (-1000, 1000)],
    }


def _inside(point: tuple[Fraction, Fraction], ring: list[tuple[float, float]]) -> bool:
    x0, y0 = point
    points = list(ring)
    if points[0] == points[-1]:
        points = points[:-1]
    for (px, py), (qx, qy) in zip(points, [*points[1:], points[0]], strict=True):
        ax, ay = qx - px, qy - py
        if ax * (y0 - py) - ay * (x0 - px) < 0:
            return False
    return True


def _assert_consistent(report, total: Total, subareas: list[Subarea]) -> None:
    for candidate in report.candidates:
        if candidate.witness is None:
            continue
        witness = candidate.witness
        assert witness.residual_area + sum(witness.subarea_areas) == witness.total_area

        total_mx = witness.total_area * witness.total_x
        total_my = witness.total_area * witness.total_y
        sub_mx = sum(
            a * x for a, x in zip(witness.subarea_areas, witness.subarea_x, strict=True)
        )
        sub_my = sum(
            a * y for a, y in zip(witness.subarea_areas, witness.subarea_y, strict=True)
        )
        residual_mx = (
            witness.residual_area * witness.residual_x
            if witness.residual_x is not None
            else Fraction(0)
        )
        residual_my = (
            witness.residual_area * witness.residual_y
            if witness.residual_y is not None
            else Fraction(0)
        )
        assert total_mx == sub_mx + residual_mx
        assert total_my == sub_my + residual_my

        t_ax = rounding_interval(total.area, total.area_step)
        t_x = rounding_interval(total.x, total.x_step)
        t_y = rounding_interval(total.y, total.y_step)
        assert t_ax[0] <= witness.total_area <= t_ax[1]
        assert t_x[0] <= witness.total_x <= t_x[1]
        assert t_y[0] <= witness.total_y <= t_y[1]
        for sub, area, x, y in zip(
            subareas,
            witness.subarea_areas,
            witness.subarea_x,
            witness.subarea_y,
            strict=True,
        ):
            a_lo, a_hi = _area_box(sub)
            x_lo, x_hi = rounding_interval(sub.x, sub.x_step)
            y_lo, y_hi = rounding_interval(sub.y, sub.y_step)
            assert a_lo <= area <= a_hi
            assert x_lo <= x <= x_hi
            assert y_lo <= y <= y_hi


def test_rounding_interval():
    assert rounding_interval(10, 1) == (Fraction("9.5"), Fraction("10.5"))
    assert rounding_interval(0, 2) == (Fraction(-1), Fraction(1))


def test_classifies_candidates():
    report = analyze(_total(), _subareas(), _candidates())

    assert report.possible == ("near", "huge")
    assert report.excluded == ("far",)
    assert report.zero_area_possible is False
    assert report.residual_area_range == (Fraction(27, 2), Fraction(33, 2))
    _assert_consistent(report, _total(), _subareas())


def test_witness_centroid_lies_inside_possible_parcel():
    report = analyze(_total(), _subareas(), _candidates())
    rings = _candidates()
    for candidate in report.candidates:
        if not candidate.possible:
            continue
        witness = candidate.witness
        assert witness is not None and witness.residual_x is not None
        assert _inside(
            (witness.residual_x, witness.residual_y),
            rings[candidate.name],
        )


def test_unreachable_parcel_is_excluded():
    candidates = {"tiny_far": [(900, 900), (901, 900), (901, 901), (900, 901)]}
    report = analyze(_total(), _subareas(), candidates)

    assert report.excluded == ("tiny_far",)
    result = report.candidates[0]
    assert isinstance(result, CandidateResult)
    assert result.witness is None


def test_zero_residual_area_never_yields_infinite_coordinate():
    # Submitted areas already account for the whole total area.
    subareas = [
        Subarea(60, 0, 0, STEP, STEP, STEP),
        Subarea(40, 25, 25, STEP, STEP, STEP),
    ]
    report = analyze(_total(), subareas, _candidates())

    assert report.zero_area_possible is True
    assert report.residual_area_range[0] == 0
    for candidate in report.candidates:
        witness = candidate.witness
        assert isinstance(witness, Witness)
        assert witness.residual_area >= 0
        # A centroid is a finite fraction when area is positive and None
        # otherwise -- never an infinity or NaN.
        if witness.residual_area == 0:
            assert witness.residual_x is None
            assert witness.residual_y is None
        else:
            assert witness.residual_x is not None
            assert witness.residual_y is not None


def test_non_convex_ring_is_flagged_and_relaxed():
    l_shape = [(0, 0), (100, 0), (100, 20), (20, 20), (20, 100), (0, 100), (0, 0)]
    report = analyze(_total(), _subareas(), {"L": l_shape})

    result = report.candidates[0]
    assert result.convex is False
    assert result.possible is True
    assert "bounding box" in result.detail


def test_relative_area_tolerance_widens_feasibility():
    strict = analyze(_total(), _subareas(), _candidates())
    tolerant_subs = [
        Subarea(60, 0, 0, STEP, STEP, STEP, area_rel_tol=0.05),
        Subarea(25, 4, 20, STEP, STEP, STEP, area_rel_tol=0.05),
    ]
    tolerant = analyze(_total(), tolerant_subs, _candidates())

    assert tolerant.residual_area_range[0] <= strict.residual_area_range[0]
    assert tolerant.residual_area_range[1] >= strict.residual_area_range[1]
    _assert_consistent(tolerant, _total(), tolerant_subs)


def test_empty_candidates():
    report = analyze(_total(), _subareas(), {})

    assert report.candidates == ()
    assert report.possible == ()
    assert report.excluded == ()


def test_no_subareas_treats_everything_as_residual():
    ring = [(0, 0), (100, 0), (100, 100), (0, 100)]
    report = analyze(_total(), [], {"whole": ring})

    result = report.candidates[0]
    assert result.possible is True
    witness = result.witness
    assert witness is not None
    assert witness.subarea_areas == ()
    assert witness.residual_area == witness.total_area
    assert _inside((witness.residual_x, witness.residual_y), ring)


@pytest.mark.parametrize(
    "ring",
    [
        [(55, 30), (65, 30), (65, 36), (55, 36)],
        [(55, 30), (65, 30), (65, 36), (55, 36), (55, 30)],
    ],
)
def test_open_and_closed_rings_equivalent(ring):
    report = analyze(_total(), _subareas(), {"parcel": ring})

    assert report.possible == ("parcel",)


def test_seeded_soundness_invariants():
    rng = random.Random(1234)
    for _ in range(40):
        n = rng.randint(1, 4)
        true_subs = []
        total_area = 0.0
        total_mx = 0.0
        total_my = 0.0
        for _ in range(n):
            area = rng.uniform(5, 50)
            x = rng.uniform(-50, 50)
            y = rng.uniform(-50, 50)
            true_subs.append((area, x, y))
            total_area += area
            total_mx += area * x
            total_my += area * y
        residual_area = rng.uniform(2, 40)
        residual_x = rng.uniform(-40, 40)
        residual_y = rng.uniform(-40, 40)
        total_area += residual_area
        total_mx += residual_area * residual_x
        total_my += residual_area * residual_y

        total = Total(
            round(total_area),
            round(total_mx / total_area),
            round(total_my / total_area),
            STEP,
            STEP,
            STEP,
        )
        subareas = [
            Subarea(round(a), round(x), round(y), STEP, STEP, STEP)
            for a, x, y in true_subs
        ]
        parcel = [
            (residual_x - 100, residual_y - 100),
            (residual_x + 100, residual_y - 100),
            (residual_x + 100, residual_y + 100),
            (residual_x - 100, residual_y + 100),
        ]
        report = analyze(total, subareas, {"parcel": parcel})

        assert report.possible == ("parcel",)
        witness = report.candidates[0].witness
        assert witness is not None and witness.residual_x is not None
        assert witness.residual_area + sum(witness.subarea_areas) == witness.total_area
        assert witness.total_area * witness.total_x == (
            sum(a * x for a, x in zip(witness.subarea_areas, witness.subarea_x, strict=True))
            + witness.residual_area * witness.residual_x
        )
