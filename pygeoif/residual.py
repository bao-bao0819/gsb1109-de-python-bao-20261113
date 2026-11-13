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
"""Infer the composite centroid of the unreported residual area.

The survey gives the whole investigation area as one rounded record (total
area and area-weighted centroid) while only some subareas have submitted
records.  Every reported quantity is rounded and the submitted areas carry an
additional measurement error.  This module puts *all* of those tolerances
into a single linear feasibility model and decides whether the centroid of
the missing residual can lie inside each declared candidate parcel.

The decision variables are physical, non-negative areas and signed first area
moments.  For a non-negative area ``a`` and a centroid coordinate confined to
``[lo, hi]``, the moment ``m = a * x`` is bounded exactly by

    ``a * lo <= m <= a * hi``

so every product stays linear.  The conservation laws are then linear as
well:

    ``A0 + sum(a_i) = T``
    ``Ux + sum(Mx_i) = Qx``   (and likewise for y)

where ``Qx = T * Cx`` is the total first moment.  A convex candidate polygon
is enforced on the residual centroid through homogenised half planes

    ``a_x * Ux + a_y * Uy <= c * A0``

which are exact for ``A0 > 0`` and collapse to ``0 <= 0`` when the residual
area vanishes.  A zero-area residual therefore yields an *undefined* centroid
(returned as ``None``) rather than an infinite coordinate.
"""

from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field
from fractions import Fraction

from pygeoif._simplex import EQ
from pygeoif._simplex import LE
from pygeoif._simplex import solve_lp
from pygeoif.types import Point2D

Ring = Sequence[Point2D]


def _frac(value: float | Fraction) -> Fraction:
    """Return an exact Fraction for a decimal number."""
    return value if isinstance(value, Fraction) else Fraction(str(value))


def rounding_interval(
    value: float | Fraction,
    step: float | Fraction,
) -> tuple[Fraction, Fraction]:
    """Closed interval a value rounded to multiples of ``step`` is consistent with."""
    centre = _frac(value)
    half = _frac(step) / 2
    return centre - half, centre + half


@dataclass(frozen=True)
class Subarea:
    """A submitted subarea record.

    ``area_step``, ``x_step`` and ``y_step`` are the rounding increments used
    for the corresponding reported value.  ``area_rel_tol`` is the optional
    relative measurement error of the reported area, e.g. ``0.01`` for one
    percent.
    """

    area: float
    x: float
    y: float
    area_step: float
    x_step: float
    y_step: float
    area_rel_tol: float = 0.0


@dataclass(frozen=True)
class Total:
    """The rounded record covering the whole investigation area."""

    area: float
    x: float
    y: float
    area_step: float
    x_step: float
    y_step: float


@dataclass(frozen=True)
class Witness:
    """A single self-consistent interpretation of every quantity.

    All values are exact fractions.  ``residual_x``/``residual_y`` are
    ``None`` when the residual area is zero, because a centroid is then not
    defined rather than infinitely far away.
    """

    total_area: Fraction
    total_x: Fraction
    total_y: Fraction
    residual_area: Fraction
    residual_x: Fraction | None
    residual_y: Fraction | None
    subarea_areas: tuple[Fraction, ...]
    subarea_x: tuple[Fraction, ...]
    subarea_y: tuple[Fraction, ...]


@dataclass(frozen=True)
class CandidateResult:
    """Classification of one declared candidate parcel."""

    name: str
    possible: bool
    convex: bool
    detail: str
    witness: Witness | None


@dataclass(frozen=True)
class ResidualReport:
    """Result of the residual-centroid analysis."""

    residual_area_range: tuple[Fraction, Fraction]
    zero_area_possible: bool
    candidates: tuple[CandidateResult, ...]

    @property
    def excluded(self) -> tuple[str, ...]:
        """Names of the candidates no feasible interpretation can reach."""
        return tuple(c.name for c in self.candidates if not c.possible)

    @property
    def possible(self) -> tuple[str, ...]:
        """Names of the candidates that remain consistent with the data."""
        return tuple(c.name for c in self.candidates if c.possible)


@dataclass
class _Model:
    """A bounded linear program solved in original variable values."""

    columns: list[str] = field(default_factory=list)
    lows: list[Fraction] = field(default_factory=list)
    rows: list[list[tuple[int, Fraction]]] = field(default_factory=list)
    relations: list[str] = field(default_factory=list)
    rhs: list[Fraction] = field(default_factory=list)

    def var(self, name: str, low: Fraction, high: Fraction) -> int:
        """Create a variable confined to the finite interval ``[low, high]``."""
        if high < low:
            msg = f"empty interval for {name}: [{low}, {high}]"
            raise ValueError(msg)
        index = len(self.columns)
        self.columns.append(name)
        self.lows.append(low)
        if high > low:
            self.rows.append([(index, Fraction(1))])
            self.relations.append(LE)
            self.rhs.append(high - low)
        return index

    def add(
        self,
        expression: Sequence[tuple[int, Fraction]],
        relation: str,
        rhs: Fraction,
    ) -> None:
        """Add ``expression @ x relation rhs`` expressed in original values."""
        shifted = rhs - sum(coef * self.lows[idx] for idx, coef in expression)
        self.rows.append(list(expression))
        self.relations.append(relation)
        self.rhs.append(shifted)

    def solve(
        self,
        objective: Sequence[tuple[int, Fraction]] | None = None,
    ) -> dict[str, Fraction] | None:
        """Return a feasible point, minimising the objective when supplied."""
        n_vars = len(self.columns)
        matrix = [[Fraction(0)] * n_vars for _ in self.rows]
        for row_index, row in enumerate(self.rows):
            for idx, coef in row:
                matrix[row_index][idx] = coef
        cost = [Fraction(0)] * n_vars
        for idx, coef in objective or []:
            cost[idx] = coef
        result = solve_lp(cost, matrix, self.relations, self.rhs)
        if result.status != "optimal" or result.x is None:
            return None
        return {
            name: self.lows[idx] + value
            for idx, (name, value) in enumerate(
                zip(self.columns, result.x, strict=True),
            )
        }


@dataclass(frozen=True)
class _Variables:
    """Column indices of the built model."""

    total: int
    moment_x: int
    moment_y: int
    residual_area: int
    residual_mx: int
    residual_my: int
    areas: tuple[int, ...]
    moments_x: tuple[int, ...]
    moments_y: tuple[int, ...]


def _is_convex(ring: Ring) -> bool:
    """Whether the (closed or open) ring defines a convex polygon."""
    points = list(ring)
    if points[0] == points[-1]:
        points = points[:-1]
    if len(points) < 3:  # noqa: PLR2004
        return False
    signs: set[int] = set()
    for i, point in enumerate(points):
        prev = points[(i - 1) % len(points)]
        nxt = points[(i + 1) % len(points)]
        cross = (point[0] - prev[0]) * (nxt[1] - point[1]) - (
            point[1] - prev[1]
        ) * (nxt[0] - point[0])
        if cross != 0:
            signs.add(1 if cross > 0 else -1)
    return len(signs) <= 1


def _halfplanes(ring: Ring) -> list[tuple[Fraction, Fraction, Fraction]]:
    """Return ``(a_x, a_y, c)`` so ``a_x*x + a_y*y <= c`` for a CCW ring."""
    points = [(_frac(x), _frac(y)) for x, y in ring]
    if points[0] == points[-1]:
        points = points[:-1]
    signed = sum(
        x1 * y2 - x2 * y1
        for (x1, y1), (x2, y2) in zip(points, [*points[1:], points[0]], strict=True)
    )
    if signed < 0:
        points.reverse()
    planes: list[tuple[Fraction, Fraction, Fraction]] = []
    for (px, py), (qx, qy) in zip(points, [*points[1:], points[0]], strict=True):
        edge_x = qx - px
        edge_y = qy - py
        planes.append((edge_y, -edge_x, edge_y * px - edge_x * py))
    return planes


def _area_box(sub: Subarea) -> tuple[Fraction, Fraction]:
    """Interval the true subarea can occupy.

    Rounding and the relative measurement error are independent tolerances, so
    their widths are added: with ``area_rel_tol == 0`` the box collapses to
    the pure rounding interval instead of pinning the area to one value.
    """
    reported = _frac(sub.area)
    half_step = _frac(sub.area_step) / 2
    tolerance = reported * _frac(sub.area_rel_tol)
    return (
        max(Fraction(0), reported - half_step - tolerance),
        reported + half_step + tolerance,
    )


def _moment_outer_box(
    area_high: Fraction,
    low: Fraction,
    high: Fraction,
) -> tuple[Fraction, Fraction]:
    """Finite outer interval of ``area * coordinate`` over the two boxes."""
    candidates = (area_high * low, area_high * high)
    return min(Fraction(0), *candidates), max(Fraction(0), *candidates)


def _bind_moment(
    model: _Model,
    area: int,
    moment: int,
    low: Fraction,
    high: Fraction,
) -> None:
    """Enforce ``area * low <= moment <= area * high`` (``area >= 0``)."""
    model.add([(area, low), (moment, -Fraction(1))], LE, Fraction(0))
    model.add([(moment, Fraction(1)), (area, -high)], LE, Fraction(0))


def _build(
    total: Total,
    subareas: Sequence[Subarea],
    polygon: Ring,
) -> tuple[_Model, _Variables]:
    """Construct the physical-variable feasibility model for one parcel."""
    model = _Model()

    t_low, t_high = rounding_interval(total.area, total.area_step)
    cx_low, cx_high = rounding_interval(total.x, total.x_step)
    cy_low, cy_high = rounding_interval(total.y, total.y_step)
    area_boxes = [_area_box(sub) for sub in subareas]

    residual_low = max(Fraction(0), t_low - sum(high for _l, high in area_boxes))
    residual_high = max(Fraction(0), t_high - sum(low for low, _h in area_boxes))

    total_idx = model.var("T", t_low, t_high)
    qx = model.var("Qx", *_moment_outer_box(t_high, cx_low, cx_high))
    qy = model.var("Qy", *_moment_outer_box(t_high, cy_low, cy_high))
    _bind_moment(model, total_idx, qx, cx_low, cx_high)
    _bind_moment(model, total_idx, qy, cy_low, cy_high)

    residual_idx = model.var("A0", residual_low, residual_high)

    poly_x = [_frac(point[0]) for point in polygon]
    poly_y = [_frac(point[1]) for point in polygon]
    x_min, x_max = min(poly_x), max(poly_x)
    y_min, y_max = min(poly_y), max(poly_y)

    rmx = model.var("Ux", *_moment_outer_box(residual_high, x_min, x_max))
    rmy = model.var("Uy", *_moment_outer_box(residual_high, y_min, y_max))
    _bind_moment(model, residual_idx, rmx, x_min, x_max)
    _bind_moment(model, residual_idx, rmy, y_min, y_max)

    area_idx: list[int] = []
    mx_idx: list[int] = []
    my_idx: list[int] = []
    for index, (sub, (a_low, a_high)) in enumerate(
        zip(subareas, area_boxes, strict=True),
    ):
        x_low, x_high = rounding_interval(sub.x, sub.x_step)
        y_low, y_high = rounding_interval(sub.y, sub.y_step)

        a_idx = model.var(f"a{index}", a_low, a_high)
        mx = model.var(f"mx{index}", *_moment_outer_box(a_high, x_low, x_high))
        my = model.var(f"my{index}", *_moment_outer_box(a_high, y_low, y_high))
        _bind_moment(model, a_idx, mx, x_low, x_high)
        _bind_moment(model, a_idx, my, y_low, y_high)

        area_idx.append(a_idx)
        mx_idx.append(mx)
        my_idx.append(my)

    # Conservation of area and of the two first moments.
    model.add(
        [
            (residual_idx, Fraction(1)),
            *[(idx, Fraction(1)) for idx in area_idx],
            (total_idx, -Fraction(1)),
        ],
        EQ,
        Fraction(0),
    )
    model.add(
        [
            (rmx, Fraction(1)),
            *[(idx, Fraction(1)) for idx in mx_idx],
            (qx, -Fraction(1)),
        ],
        EQ,
        Fraction(0),
    )
    model.add(
        [
            (rmy, Fraction(1)),
            *[(idx, Fraction(1)) for idx in my_idx],
            (qy, -Fraction(1)),
        ],
        EQ,
        Fraction(0),
    )

    if polygon is not None and _is_convex(polygon):
        for ax, ay, c in _halfplanes(polygon):
            model.add(
                [
                    (rmx, ax),
                    (rmy, ay),
                    (residual_idx, -c),
                ],
                LE,
                Fraction(0),
            )

    return model, _Variables(
        total=total_idx,
        moment_x=qx,
        moment_y=qy,
        residual_area=residual_idx,
        residual_mx=rmx,
        residual_my=rmy,
        areas=tuple(area_idx),
        moments_x=tuple(mx_idx),
        moments_y=tuple(my_idx),
    )


def _witness(
    model: _Model,
    variables: _Variables,
    point: Mapping[str, Fraction],
    subareas: Sequence[Subarea],
) -> Witness:
    """Convert a feasible LP point into an exact interpretation."""
    def value(index: int) -> Fraction:
        return point[model.columns[index]]

    total_area = value(variables.total)
    residual_area = value(variables.residual_area)
    residual_x = (
        value(variables.residual_mx) / residual_area
        if residual_area > 0
        else None
    )
    residual_y = (
        value(variables.residual_my) / residual_area
        if residual_area > 0
        else None
    )
    sub_areas = tuple(value(variables.areas[i]) for i in range(len(subareas)))
    sub_x = tuple(
        value(variables.moments_x[i]) / sub_areas[i]
        if sub_areas[i] > 0
        else _frac(subareas[i].x)
        for i in range(len(subareas))
    )
    sub_y = tuple(
        value(variables.moments_y[i]) / sub_areas[i]
        if sub_areas[i] > 0
        else _frac(subareas[i].y)
        for i in range(len(subareas))
    )
    return Witness(
        total_area=total_area,
        total_x=value(variables.moment_x) / total_area,
        total_y=value(variables.moment_y) / total_area,
        residual_area=residual_area,
        residual_x=residual_x,
        residual_y=residual_y,
        subarea_areas=sub_areas,
        subarea_x=sub_x,
        subarea_y=sub_y,
    )


def analyze(
    total: Total,
    subareas: Sequence[Subarea],
    candidates: Mapping[str, Ring],
) -> ResidualReport:
    """Classify each candidate parcel against the rounded records.

    Parameters
    ----------
    total:
        The rounded record for the whole investigation area.
    subareas:
        The submitted subarea records; the residual is whatever is missing.
    candidates:
        Mapping of candidate name to its exterior ring, a sequence of
        ``(x, y)`` points.  Convex rings are handled exactly; non-convex rings
        fall back to their bounding box, a one-sided relaxation that can only
        keep a candidate open and never exclude it wrongly.

    Returns
    -------
    ResidualReport
        Residual area range, a zero-area flag and a per-candidate verdict with
        a finite witness interpretation whenever one can be constructed.
    """
    t_low, t_high = rounding_interval(total.area, total.area_step)
    area_boxes = [_area_box(sub) for sub in subareas]
    residual_low = max(Fraction(0), t_low - sum(high for _l, high in area_boxes))
    residual_high = max(Fraction(0), t_high - sum(low for low, _h in area_boxes))

    results: list[CandidateResult] = []
    for name, ring in candidates.items():
        convex = _is_convex(ring)
        model, variables = _build(total, subareas, ring)
        # Maximise the residual area: a parcel only counts when a strictly
        # positive residual centroid can actually be placed inside it.
        point = model.solve([(variables.residual_area, -Fraction(1))])
        if point is None:
            results.append(
                CandidateResult(
                    name,
                    False,
                    convex,
                    "no feasible interpretation keeps the residual centroid inside",
                    None,
                ),
            )
            continue

        witness = _witness(model, variables, point, subareas)
        if witness.residual_area == 0:
            results.append(
                CandidateResult(
                    name,
                    False,
                    convex,
                    "only the degenerate zero-area residual satisfies the parcel; "
                    "no real centroid can be placed inside",
                    witness,
                ),
            )
            continue

        if not convex:
            detail = "possible (non-convex ring tested via its bounding box)"
        else:
            detail = "a finite feasible interpretation places the centroid inside"
        results.append(CandidateResult(name, True, convex, detail, witness))

    return ResidualReport(
        residual_area_range=(residual_low, residual_high),
        zero_area_possible=residual_low == 0,
        candidates=tuple(results),
    )


__all__ = [
    "CandidateResult",
    "ResidualReport",
    "Ring",
    "Subarea",
    "Total",
    "Witness",
    "analyze",
    "rounding_interval",
]
