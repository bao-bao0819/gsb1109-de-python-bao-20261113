#
#   Copyright (C) 2026 Christian Ledermann
#
#   This library is free software; you can redistribute it and/or
#   modify it under the terms of the GNU Lesser General Public
#   License as published by the Free Software Foundation; either
#   version 2.1 of the License, or (at your option) any later version.

#   This library is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
#   Lesser General Public License for more details.

#   You should have received a copy of the GNU Lesser General Public
#   License along with this library; if not, write to the Free Software
#   Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301  USA
#
"""A tiny exact two-phase simplex solver built only on the standard library.

The residual-centroid problem is a linear feasibility/optimisation problem.
Using :class:`fractions.Fraction` keeps every pivot exact, so a feasible
interpretation is never rejected (and an infeasible one is never accepted)
because of floating point rounding.  All decision variables are assumed to be
non-negative; free variables are expressed as the difference of two
non-negative variables by the caller.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction

Number = int | float | Fraction
Row = list[Fraction]

LE = "<="
GE = ">="
EQ = "="


@dataclass(frozen=True)
class LPResult:
    """Outcome of a linear program.

    Attributes
    ----------
    status:
        ``"optimal"`` when an optimum was found, ``"infeasible"`` when the
        feasible region is empty and ``"unbounded"`` when the objective can
        grow without bound.
    fun:
        The optimal objective value (``None`` unless ``status == "optimal"``).
    x:
        An optimal vertex, one :class:`~fractions.Fraction` per original
        variable (``None`` unless ``status == "optimal"``).

    """

    status: str
    fun: Fraction | None
    x: tuple[Fraction, ...] | None


def _as_fraction(value: Number) -> Fraction:
    """Convert a number to an exact Fraction via its decimal representation."""
    if isinstance(value, Fraction):
        return value
    return Fraction(str(value))


def _pivot(tableau: list[Row], basis: list[int], row: int, col: int) -> None:
    """Pivot ``tableau`` on ``(row, col)`` and update the basis."""
    pivot_element = tableau[row][col]
    tableau[row] = [element / pivot_element for element in tableau[row]]
    for other_row in range(len(tableau)):
        if other_row == row:
            continue
        factor = tableau[other_row][col]
        if factor == 0:
            continue
        tableau[other_row] = [
            element - factor * pivot_row_element
            for element, pivot_row_element in zip(
                tableau[other_row],
                tableau[row],
                strict=True,
            )
        ]
    basis[row] = col


def _entering_column(objective: Row, artificial: frozenset[int]) -> int | None:
    """Bland's rule: the smallest index with a negative reduced cost."""
    for col in range(len(objective) - 1):
        if col in artificial:
            continue
        if objective[col] < 0:
            return col
    return None


def _leaving_row(tableau: list[Row], basis: list[int], col: int) -> int | None:
    """Minimum-ratio test, ties broken by the smallest basic variable index."""
    leaving: int | None = None
    best_ratio: Fraction | None = None
    for row in range(len(tableau) - 1):
        coefficient = tableau[row][col]
        if coefficient <= 0:
            continue
        ratio = tableau[row][-1] / coefficient
        if best_ratio is None or ratio < best_ratio or (
            ratio == best_ratio and leaving is not None and basis[row] < basis[leaving]
        ):
            best_ratio = ratio
            leaving = row
    return leaving


def solve_lp(
    objective: Sequence[Number],
    constraints: Sequence[Sequence[Number]],
    relations: Sequence[str],
    rhs: Sequence[Number],
) -> LPResult:
    """Minimise ``objective @ x`` subject to linear constraints.

    All decision variables are non-negative.  ``constraints`` is a sequence of
    coefficient rows, ``relations`` holds one of ``"<="``, ``">="`` or ``"="``
    per row and ``rhs`` the corresponding right hand side.

    The solver runs the two-phase simplex method with Bland's anti-cycling
    rule and exact rational arithmetic.
    """
    n_vars = len(objective)
    rows = [
        [_as_fraction(value) for value in row] for row in constraints
    ]
    relations = list(relations)
    right = [_as_fraction(value) for value in rhs]

    # Normalise every row so the right hand side is non-negative.
    for i in range(len(rows)):
        if right[i] < 0:
            rows[i] = [-value for value in rows[i]]
            right[i] = -right[i]
            relations[i] = {LE: GE, GE: LE, EQ: EQ}[relations[i]]

    tableau: list[Row] = [
        row + [rhs_value] for row, rhs_value in zip(rows, right, strict=True)
    ]
    basis: list[int] = []
    artificial: list[int] = []
    width = n_vars

    for i, relation in enumerate(relations):
        if relation == LE:
            for row_index in range(len(tableau)):
                tableau[row_index].insert(width, Fraction(1 if row_index == i else 0))
            basis.append(width)
            width += 1
        elif relation == GE:
            for row_index in range(len(tableau)):
                tableau[row_index].insert(width, Fraction(-1 if row_index == i else 0))
            width += 1
            for row_index in range(len(tableau)):
                tableau[row_index].insert(width, Fraction(1 if row_index == i else 0))
            artificial.append(width)
            basis.append(width)
            width += 1
        else:  # EQ
            for row_index in range(len(tableau)):
                tableau[row_index].insert(width, Fraction(1 if row_index == i else 0))
            artificial.append(width)
            basis.append(width)
            width += 1

    artificial_set = frozenset(artificial)

    # Phase I: minimise the sum of the artificial variables.
    phase_one = [Fraction(0)] * width + [Fraction(0)]
    for col in artificial:
        phase_one[col] = Fraction(-1)
    for row_index, basic_col in enumerate(basis):
        if basic_col in artificial_set:
            phase_one = [
                reduced - tableau[row_index][col]
                for col, reduced in enumerate(phase_one)
            ]
    tableau.append(phase_one)

    while True:
        entering = _entering_column(tableau[-1], artificial_set)
        if entering is None:
            break
        leaving = _leaving_row(tableau, basis, entering)
        if leaving is None:  # pragma: no cover - phase I is bounded below by 0
            break
        _pivot(tableau, basis, leaving, entering)

    if tableau[-1][-1] != 0:
        return LPResult(status="infeasible", fun=None, x=None)

    # Remove artificial variables that are still basic at zero level.
    for row_index in range(len(basis)):
        if basis[row_index] not in artificial_set:
            continue
        pivot_col = next(
            (
                col
                for col in range(n_vars)
                if tableau[row_index][col] != 0
            ),
            None,
        )
        if pivot_col is None:
            continue  # redundant row: its artificial stays harmlessly at zero
        _pivot(tableau, basis, row_index, pivot_col)

    # Phase II: optimise the real objective.  The tableau maximises
    # ``-objective @ x``, so structural column j starts with coefficient
    # ``+objective[j]`` and the optimised (minimised) value is ``-RHS``.
    phase_two = [Fraction(0)] * (width + 1)
    for col, cost in enumerate(objective):
        phase_two[col] = _as_fraction(cost)
    for row_index, basic_col in enumerate(basis):
        if basic_col in artificial_set:
            continue
        cost = phase_two[basic_col]
        if cost == 0:
            continue
        phase_two = [
            reduced - cost * tableau[row_index][col]
            for col, reduced in enumerate(phase_two)
        ]
    tableau[-1] = phase_two

    while True:
        entering = _entering_column(tableau[-1], artificial_set)
        if entering is None:
            break
        leaving = _leaving_row(tableau, basis, entering)
        if leaving is None:
            return LPResult(status="unbounded", fun=None, x=None)
        _pivot(tableau, basis, leaving, entering)

    solution = [Fraction(0) for _ in range(n_vars)]
    for row_index, basic_col in enumerate(basis):
        if basic_col < n_vars:
            solution[basic_col] = tableau[row_index][-1]
    return LPResult(
        status="optimal",
        fun=-tableau[-1][-1],
        x=tuple(solution),
    )


__all__ = [
    "EQ",
    "GE",
    "LE",
    "LPResult",
    "solve_lp",
]
