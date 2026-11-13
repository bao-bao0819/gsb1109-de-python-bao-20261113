"""Tests for the exact two-phase simplex solver."""

from fractions import Fraction

import pytest

from pygeoif._simplex import EQ
from pygeoif._simplex import GE
from pygeoif._simplex import LE
from pygeoif._simplex import solve_lp


def test_bounded_minimum():
    # min -3x - 2y s.t. 2x + y <= 18, 2x + 3y <= 42, 3x + y <= 24
    result = solve_lp(
        [-3, -2],
        [[2, 1], [2, 3], [3, 1]],
        [LE, LE, LE],
        [18, 42, 24],
    )

    assert result.status == "optimal"
    assert result.fun == Fraction(-33)
    assert result.x == (Fraction(3), Fraction(12))


def test_infeasible():
    result = solve_lp([1], [[1], [1]], [GE, LE], [5, 3])

    assert result.status == "infeasible"
    assert result.fun is None
    assert result.x is None


def test_unbounded():
    result = solve_lp([-1], [], [], [])

    assert result.status == "unbounded"
    assert result.x is None


def test_equality_and_greater_equal():
    # min x s.t. x + y = 4 and y >= 1: x can fall to 0 with y = 4.
    result = solve_lp(
        [1, 0],
        [[1, 1], [0, 1]],
        [EQ, GE],
        [4, 1],
    )

    assert result.status == "optimal"
    assert result.fun == Fraction(0)
    assert result.x == (Fraction(0), Fraction(4))


def test_negative_rhs_is_normalised():
    # -x <= -2 means x >= 2, together with x <= 5: min x = 2
    result = solve_lp([1], [[-1], [1]], [LE, LE], [-2, 5])

    assert result.status == "optimal"
    assert result.fun == Fraction(2)
    assert result.x == (Fraction(2),)


def test_redundant_equality_rows():
    result = solve_lp(
        [1, 1],
        [[1, 1], [1, 1]],
        [EQ, EQ],
        [2, 2],
    )

    assert result.status == "optimal"
    assert result.fun == Fraction(2)


def test_fraction_inputs_are_exact():
    result = solve_lp(
        [Fraction(1, 2)],
        [[Fraction(1, 3)]],
        [LE],
        [Fraction(1, 6)],
    )

    assert result.status == "optimal"
    assert result.fun == Fraction(0)


def test_decimal_strings_avoid_float_error():
    result = solve_lp([0.1], [[0.1]], [LE], [0.1])

    assert result.status == "optimal"
    assert result.fun == Fraction(0)


def test_greater_equal_drives_solution():
    # min x s.t. 2x >= 7 -> x = 3.5
    result = solve_lp([1], [[2]], [GE], [7])

    assert result.status == "optimal"
    assert result.fun == Fraction(7, 2)


@pytest.mark.parametrize(
    ("objective", "expect"),
    [
        ([1, 0], Fraction(0)),
        ([0, 1], Fraction(0)),
    ],
)
def test_zero_objective_feasibility(objective, expect):
    result = solve_lp(
        objective,
        [[1, 1]],
        [LE],
        [1],
    )

    assert result.status == "optimal"
    assert result.fun == expect
