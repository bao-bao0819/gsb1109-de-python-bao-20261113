"""Exact area-overlap accounting between two cadastral deliveries.

The plane is cut into vertical slabs at every vertex x-coordinate and every
edge/edge intersection x-coordinate of both batches. Inside a slab no two
edge endpoints cross, so for any vertical line the coverage pattern of all
parcels is fixed and every coverage interval boundary moves linearly in x.
The area of any Boolean combination of parcel memberships inside a slab is
therefore exactly ``width * measure(at mid-x)`` — no rasterisation, no
approximation. Edge-only contact has zero width and never counts as a
transfer.

Per old parcel ``o`` the ledger partitions its area exactly:

    area(o) = sum_j transfer(o, j)   # clean overlap with exactly one new parcel
            + retreat(o)             # covered by no new parcel (退界)
            + dispute(o)             # also claimed by other old parcels (争议)
            + new_overlap(o)         # claimed by 2+ new parcels (normally 0)

Per new parcel ``n``:

    area(n) = sum_i transfer(i, n)   # clean sources
            + fill(n)                # no old parcel underneath (新增填土)
            + old_dispute(n)         # sits on an old-batch disputed zone
            + new_dispute(n)         # overlapped by sibling new parcels
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations, groupby

from .parcels import Parcel


@dataclass
class AreaLedger:
    """Measured area flows from the old batch to the new batch."""

    old_ids: list[str]
    new_ids: list[str]
    old_area: dict[str, float]
    new_area: dict[str, float]
    transfer: dict[tuple[str, str], float] = field(default_factory=dict)
    retreat: dict[str, float] = field(default_factory=dict)
    dispute_old: dict[str, float] = field(default_factory=dict)
    new_overlap_on_old: dict[str, float] = field(default_factory=dict)
    fill: dict[str, float] = field(default_factory=dict)
    old_dispute_on_new: dict[str, float] = field(default_factory=dict)
    dispute_new: dict[str, float] = field(default_factory=dict)
    old_pairs: dict[tuple[str, str], float] = field(default_factory=dict)
    new_pairs: dict[tuple[str, str], float] = field(default_factory=dict)


def _segment_crossings(
    e1: tuple[float, float, float, float],
    e2: tuple[float, float, float, float],
    tol: float,
) -> list[tuple[float, float]]:
    """Intersection points of two segments (proper, touching or collinear)."""
    x1, y1, x2, y2 = e1
    x3, y3, x4, y4 = e2
    den = (x2 - x1) * (y4 - y3) - (y2 - y1) * (x4 - x3)
    if abs(den) > tol:
        t = ((x3 - x1) * (y4 - y3) - (y3 - y1) * (x4 - x3)) / den
        u = ((x3 - x1) * (y2 - y1) - (y3 - y1) * (x2 - x1)) / den
        if -tol <= t <= 1 + tol and -tol <= u <= 1 + tol:
            return [(x1 + t * (x2 - x1), y1 + t * (y2 - y1))]
        return []
    # Parallel: only collinear overlaps matter.
    if abs((x3 - x1) * (y2 - y1) - (y3 - y1) * (x2 - x1)) > tol:
        return []

    def on_segment(px: float, py: float, e: tuple[float, float, float, float]) -> bool:
        ex1, ey1, ex2, ey2 = e
        return (
            min(ex1, ex2) - tol <= px <= max(ex1, ex2) + tol
            and min(ey1, ey2) - tol <= py <= max(ey1, ey2) + tol
        )

    points = []
    for px, py in ((x1, y1), (x2, y2)):
        if on_segment(px, py, e2):
            points.append((px, py))
    for px, py in ((x3, y3), (x4, y4)):
        if on_segment(px, py, e1):
            points.append((px, py))
    return points


def _slab_boundaries(parcels: list[Parcel], tol: float) -> list[float]:
    """All x-coordinates where the vertical coverage pattern may change."""
    edges: list[tuple[int, tuple[float, float, float, float]]] = []
    xs: list[float] = []
    for idx, parcel in enumerate(parcels):
        for ring in parcel.rings:
            xs.extend(x for x, _ in ring)
        for edge in parcel.edges:
            edges.append((idx, edge))
    for k in range(len(edges)):
        i, e1 = edges[k]
        for m in range(k + 1, len(edges)):
            j, e2 = edges[m]
            if i == j:
                continue
            if max(e1[0], e1[2]) < min(e2[0], e2[2]) - tol or max(
                e2[0], e2[2]
            ) < min(e1[0], e1[2]) - tol:
                continue
            for px, _ in _segment_crossings(e1, e2, tol):
                xs.append(px)
    xs.sort()
    merged: list[float] = []
    scale = 1.0 + max((abs(x) for x in xs), default=0.0)
    for x in xs:
        if merged and x - merged[-1] <= tol * scale:
            continue
        merged.append(x)
    return merged


def _slice_intervals(parcel: Parcel, xm: float) -> list[tuple[float, float]]:
    """Intervals of the parcel's cross-section at x = xm (even-odd fill)."""
    ys: list[float] = []
    for x1, y1, x2, y2 in parcel.edges:
        if (x1 < xm < x2) or (x2 < xm < x1):
            ys.append(y1 + (y2 - y1) * (xm - x1) / (x2 - x1))
    ys.sort()
    if len(ys) % 2:
        msg = f"odd crossing count for parcel {parcel.parcel_id!r} at x={xm}"
        raise ValueError(msg)
    return [(ys[k], ys[k + 1]) for k in range(0, len(ys), 2) if ys[k + 1] > ys[k]]


def compute_area_ledger(
    old_parcels: list[Parcel],
    new_parcels: list[Parcel],
    eps: float = 1e-9,
) -> AreaLedger:
    """Measure all area flows between the two batches, purely geometrically."""
    parcels = list(old_parcels) + list(new_parcels)
    n_old = len(old_parcels)
    ledger = AreaLedger(
        old_ids=[p.parcel_id for p in old_parcels],
        new_ids=[p.parcel_id for p in new_parcels],
        old_area={p.parcel_id: p.area for p in old_parcels},
        new_area={p.parcel_id: p.area for p in new_parcels},
    )
    xs = _slab_boundaries(parcels, eps)
    for x0, x1 in zip(xs, xs[1:]):
        width = x1 - x0
        if width <= 0:
            continue
        xm = 0.5 * (x0 + x1)
        events: list[tuple[float, int, int]] = []
        for idx, parcel in enumerate(parcels):
            for lo, hi in _slice_intervals(parcel, xm):
                events.append((lo, idx, 1))
                events.append((hi, idx, -1))
        events.sort(key=lambda event: event[0])
        counts = [0] * len(parcels)
        prev_y: float | None = None
        for y, group in groupby(events, key=lambda event: event[0]):
            if prev_y is not None and y > prev_y:
                _attribute(
                    ledger, counts, n_old, parcels, width * (y - prev_y)
                )
            for _, idx, delta in group:
                counts[idx] += delta
            prev_y = y
    return ledger


def _attribute(
    ledger: AreaLedger,
    counts: list[int],
    n_old: int,
    parcels: list[Parcel],
    area: float,
) -> None:
    """Assign one elementary rectangle to the ledger buckets."""
    old_cover = [i for i in range(n_old) if counts[i] > 0]
    new_cover = [i for i in range(n_old, len(counts)) if counts[i] > 0]
    old_ids = [parcels[i].parcel_id for i in old_cover]
    new_ids = [parcels[i].parcel_id for i in new_cover]
    if len(old_cover) == 1:
        old_id = old_ids[0]
        if len(new_cover) == 0:
            ledger.retreat[old_id] = ledger.retreat.get(old_id, 0.0) + area
        elif len(new_cover) == 1:
            key = (old_id, new_ids[0])
            ledger.transfer[key] = ledger.transfer.get(key, 0.0) + area
        else:
            key = old_id
            ledger.new_overlap_on_old[key] = (
                ledger.new_overlap_on_old.get(key, 0.0) + area
            )
    elif len(old_cover) >= 2:
        for old_id in old_ids:
            ledger.dispute_old[old_id] = ledger.dispute_old.get(old_id, 0.0) + area
        for pair in combinations(sorted(old_ids), 2):
            ledger.old_pairs[pair] = ledger.old_pairs.get(pair, 0.0) + area
    if len(new_cover) == 1:
        new_id = new_ids[0]
        if len(old_cover) == 0:
            ledger.fill[new_id] = ledger.fill.get(new_id, 0.0) + area
        elif len(old_cover) >= 2:
            ledger.old_dispute_on_new[new_id] = (
                ledger.old_dispute_on_new.get(new_id, 0.0) + area
            )
    elif len(new_cover) >= 2:
        for new_id in new_ids:
            ledger.dispute_new[new_id] = ledger.dispute_new.get(new_id, 0.0) + area
        for pair in combinations(sorted(new_ids), 2):
            ledger.new_pairs[pair] = ledger.new_pairs.get(pair, 0.0) + area
