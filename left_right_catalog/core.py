"""
Left/right parcel catalog along shared boundaries.

Convention (约定朝向): outer rings run counter-clockwise (CCW) and holes
run clockwise (CW), i.e. a ring and its holes have opposite directions.
Walking along any ring, the parcel interior is always on the *left*, so
adjacent parcels traverse their common boundary in opposite directions.

All arithmetic uses :class:`fractions.Fraction`, therefore collinearity
checks and edge splitting are exact and the results are reproducible.
"""

from dataclasses import dataclass
from fractions import Fraction
from itertools import pairwise
from math import sqrt
from typing import TypeAlias

Number: TypeAlias = int | float | str | Fraction
RawPoint: TypeAlias = tuple[Number, Number]
Point: TypeAlias = tuple[Fraction, Fraction]
Ring: TypeAlias = tuple[Point, ...]
Signature: TypeAlias = tuple[
    tuple[Point, Point, frozenset[str], frozenset[str]],
    ...,
]

CCW = "CCW"
CW = "CW"
MIN_RING_POINTS = 3


@dataclass(frozen=True)
class Parcel:
    """A cadastral parcel: one outer ring and optional holes."""

    parcel_id: str
    outer: tuple[RawPoint, ...]
    holes: tuple[tuple[RawPoint, ...], ...] = ()


@dataclass(frozen=True)
class OrientationFix:
    """A ring whose orientation violated the convention and was flipped."""

    parcel_id: str
    ring_name: str
    was: str


@dataclass(frozen=True)
class NormalizedParcel:
    """A parcel whose rings follow the orientation convention."""

    parcel_id: str
    outer: Ring
    holes: tuple[Ring, ...]

    def rings(self) -> tuple[Ring, ...]:
        """Return the outer ring followed by all holes."""
        return (self.outer, *self.holes)


@dataclass(frozen=True)
class NormalizedParcels:
    """Parcels after orientation normalization, plus the applied fixes."""

    parcels: tuple[NormalizedParcel, ...]
    fixes: tuple[OrientationFix, ...]


@dataclass(frozen=True)
class SegmentInfo:
    """
    An atomic segment with the parcels claiming its left/right side.

    The canonical direction of a segment runs from the lexicographically
    smaller endpoint (``start``) to the larger one (``end``).
    """

    start: Point
    end: Point
    left: frozenset[str]
    right: frozenset[str]

    @property
    def length2(self) -> Fraction:
        """Return the exact squared length of the segment."""
        return (self.end[0] - self.start[0]) ** 2 + (self.end[1] - self.start[1]) ** 2

    @property
    def length(self) -> float:
        """Return the segment length."""
        return sqrt(float(self.length2))


@dataclass(frozen=True)
class SideConflict:
    """Parcels claiming the same side of the same atomic segment."""

    start: Point
    end: Point
    side: str
    parcels: tuple[str, ...]

    @property
    def length2(self) -> Fraction:
        """Return the exact squared length of the conflicting segment."""
        return (self.end[0] - self.start[0]) ** 2 + (self.end[1] - self.start[1]) ** 2

    @property
    def length(self) -> float:
        """Return the length of the conflicting segment."""
        return sqrt(float(self.length2))


@dataclass(frozen=True)
class CollinearInsertion:
    """A vertex of one parcel absorbed into another parcel's edge."""

    vertex_of: str
    point: Point
    edge_of: str


@dataclass(frozen=True)
class SideClaim:
    """The parcels on the left and right when walking a queried edge."""

    left: frozenset[str]
    right: frozenset[str]


def _to_point(raw: RawPoint) -> Point:
    x, y = raw
    return (Fraction(x), Fraction(y))


def _clean_ring(raw: tuple[RawPoint, ...]) -> list[Point]:
    ring: list[Point] = []
    for raw_point in raw:
        point = _to_point(raw_point)
        if not ring or ring[-1] != point:
            ring.append(point)
    if len(ring) > 1 and ring[0] == ring[-1]:
        ring.pop()
    return ring


def ring_edges(ring: Ring) -> list[tuple[Point, Point]]:
    """Return the directed edges (p_i, p_{i+1}) of a closed ring."""
    return list(zip(ring, ring[1:] + ring[:1], strict=True))


def signed_area2(ring: Ring) -> Fraction:
    """Return twice the signed area; positive means counter-clockwise."""
    total = Fraction(0)
    for (x1, y1), (x2, y2) in ring_edges(ring):
        total += x1 * y2 - x2 * y1
    return total


def _orientation(ring: Ring) -> str:
    area = signed_area2(ring)
    if area > 0:
        return CCW
    if area < 0:
        return CW
    msg = "degenerate ring with zero signed area"
    raise ValueError(msg)


def _normalize_ring(raw: tuple[RawPoint, ...], want: str) -> tuple[Ring, str | None]:
    ring = _clean_ring(raw)
    if len(ring) < MIN_RING_POINTS:
        msg = "a ring needs at least 3 distinct points"
        raise ValueError(msg)
    normalized = tuple(ring)
    actual = _orientation(normalized)
    if actual == want:
        return normalized, None
    return tuple(reversed(normalized)), actual


def normalize_parcels(parcels: tuple[Parcel, ...]) -> NormalizedParcels:
    """
    Flip rings so outer rings are CCW and holes CW (opposite directions).

    Because normalization always enforces the agreed orientation, the
    resulting catalog is invariant under flipping any or all input rings.
    """
    normalized = []
    fixes = []
    for parcel in parcels:
        outer, was = _normalize_ring(parcel.outer, CCW)
        if was is not None:
            fixes.append(OrientationFix(parcel.parcel_id, "outer", was))
        holes = []
        for index, hole in enumerate(parcel.holes, start=1):
            ring, was = _normalize_ring(hole, CW)
            if was is not None:
                fixes.append(
                    OrientationFix(parcel.parcel_id, f"hole#{index}", was),
                )
            holes.append(ring)
        normalized.append(
            NormalizedParcel(parcel.parcel_id, outer, tuple(holes)),
        )
    return NormalizedParcels(tuple(normalized), tuple(fixes))


def _split_points(a: Point, b: Point, points: set[Point]) -> list[Point]:
    """Return points strictly between a and b on the segment, sorted a->b."""
    dx = b[0] - a[0]
    dy = b[1] - a[1]
    len2 = dx * dx + dy * dy
    interior = []
    for point in points:
        if point in (a, b):
            continue
        cross = dx * (point[1] - a[1]) - dy * (point[0] - a[0])
        if cross != 0:
            continue
        dot = (point[0] - a[0]) * dx + (point[1] - a[1]) * dy
        if 0 < dot < len2:
            interior.append((dot, point))
    interior.sort(key=lambda item: item[0])
    return [point for _, point in interior]


class Catalog:
    """Left/right parcel lookup over atomic boundary segments."""

    def __init__(
        self,
        segments: list[SegmentInfo],
        insertions: list[CollinearInsertion],
    ) -> None:
        """Store segments sorted for deterministic output."""
        self._segments = tuple(
            sorted(segments, key=lambda seg: (seg.start, seg.end)),
        )
        self._by_key = {(seg.start, seg.end): seg for seg in self._segments}
        self.insertions = tuple(
            sorted(
                set(insertions),
                key=lambda ins: (ins.vertex_of, ins.point, ins.edge_of),
            ),
        )

    @property
    def segments(self) -> tuple[SegmentInfo, ...]:
        """Return all atomic segments in canonical order."""
        return self._segments

    def signature(self) -> Signature:
        """Return a hashable fingerprint of the whole left/right catalog."""
        return tuple(
            (seg.start, seg.end, seg.left, seg.right) for seg in self._segments
        )

    def shared_boundaries(self) -> tuple[SegmentInfo, ...]:
        """Return segments with exactly one distinct parcel on each side."""
        return tuple(
            seg
            for seg in self._segments
            if len(seg.left) == 1 and len(seg.right) == 1 and seg.left != seg.right
        )

    def boundary_segments(self) -> tuple[SegmentInfo, ...]:
        """Return segments claimed from one side only (outer/hole boundary)."""
        return tuple(seg for seg in self._segments if bool(seg.left) != bool(seg.right))

    def self_conflicts(self) -> tuple[SegmentInfo, ...]:
        """Return segments claimed by the same parcel from both sides."""
        return tuple(seg for seg in self._segments if seg.left & seg.right)

    def conflicts(self, min_claimants: int = 2) -> tuple[SideConflict, ...]:
        """Return sides claimed by at least ``min_claimants`` parcels."""
        found = []
        for seg in self._segments:
            for side, parcels in (("left", seg.left), ("right", seg.right)):
                if len(parcels) >= min_claimants:
                    found.append(
                        SideConflict(
                            seg.start,
                            seg.end,
                            side,
                            tuple(sorted(parcels)),
                        ),
                    )
        return tuple(
            sorted(found, key=lambda c: (c.start, c.end, c.side)),
        )

    def shortest_conflict(self, min_claimants: int = 3) -> SideConflict | None:
        """
        Return the shortest segment claimed by >= min_claimants parcels.

        Used to pinpoint the shortest edge segment where three or more
        different parcels all claim the same side.
        """
        candidates = self.conflicts(min_claimants)
        if not candidates:
            return None
        return min(candidates, key=lambda c: (c.length2, c.start, c.end))

    def query(self, a: RawPoint, b: RawPoint) -> SideClaim:
        """
        Look up the parcels on the left/right when walking from a to b.

        The queried edge may span several atomic segments (e.g. when the
        other side inserted extra collinear vertices); the claims of all
        covering atomic segments are merged.
        """
        start = _to_point(a)
        end = _to_point(b)
        dx = end[0] - start[0]
        dy = end[1] - start[1]
        if dx == 0 and dy == 0:
            msg = "a query needs two distinct points"
            raise ValueError(msg)
        left: set[str] = set()
        right: set[str] = set()
        for seg in self._covering(start, end):
            sx = seg.end[0] - seg.start[0]
            sy = seg.end[1] - seg.start[1]
            if dx * sx + dy * sy > 0:
                left |= seg.left
                right |= seg.right
            else:
                left |= seg.right
                right |= seg.left
        return SideClaim(frozenset(left), frozenset(right))

    def _covering(self, a: Point, b: Point) -> list[SegmentInfo]:
        """Return the atomic segments covering a->b, in walking order."""
        dx = b[0] - a[0]
        dy = b[1] - a[1]
        len2 = dx * dx + dy * dy
        parts = []
        for seg in self._segments:
            t_start = _parameter(a, dx, dy, seg.start)
            t_end = _parameter(a, dx, dy, seg.end)
            if t_start is None or t_end is None:
                continue
            lo = min(t_start, t_end)
            hi = max(t_start, t_end)
            if 0 <= lo < hi <= len2:
                parts.append((lo, hi, seg))
        parts.sort(key=lambda item: item[0])
        _check_coverage(parts, len2)
        return [seg for _, _, seg in parts]


def _parameter(
    a: Point,
    dx: Fraction,
    dy: Fraction,
    point: Point,
) -> Fraction | None:
    """Return the dot product parameter of point on the line through a."""
    cross = dx * (point[1] - a[1]) - dy * (point[0] - a[0])
    if cross != 0:
        return None
    return (point[0] - a[0]) * dx + (point[1] - a[1]) * dy


def _check_coverage(
    parts: list[tuple[Fraction, Fraction, SegmentInfo]],
    len2: Fraction,
) -> None:
    """Ensure the collected atomic segments cover [0, len2] without gaps."""
    msg = "the queried edge is not fully covered by the catalog"
    position = Fraction(0)
    for lo, hi, _seg in parts:
        if lo != position:
            raise ValueError(msg)
        position = hi
    if position != len2:
        raise ValueError(msg)


def build_catalog(normalized: NormalizedParcels) -> Catalog:
    """Split all ring edges at collinear vertices and record side claims."""
    points: set[Point] = set()
    owners: dict[Point, set[str]] = {}
    for parcel in normalized.parcels:
        for ring in parcel.rings():
            for point in ring:
                points.add(point)
                owners.setdefault(point, set()).add(parcel.parcel_id)
    claims: dict[tuple[Point, Point], dict[str, set[str]]] = {}
    insertions: list[CollinearInsertion] = []
    for parcel in normalized.parcels:
        for ring in parcel.rings():
            for a, b in ring_edges(ring):
                chain = [a, *_split_points(a, b, points), b]
                for p, q in pairwise(chain):
                    key = (p, q) if p < q else (q, p)
                    sides = claims.setdefault(key, {"left": set(), "right": set()})
                    side = "left" if p == key[0] else "right"
                    sides[side].add(parcel.parcel_id)
                for mid in chain[1:-1]:
                    insertions.extend(
                        CollinearInsertion(owner, mid, parcel.parcel_id)
                        for owner in owners[mid] - {parcel.parcel_id}
                    )
    segments = [
        SegmentInfo(key[0], key[1], frozenset(sides["left"]), frozenset(sides["right"]))
        for key, sides in claims.items()
    ]
    return Catalog(segments, insertions)


def flip_rings(parcels: tuple[Parcel, ...]) -> tuple[Parcel, ...]:
    """Reverse every ring of every parcel (整体翻转所有输入环)."""
    return tuple(
        Parcel(
            parcel.parcel_id,
            tuple(reversed(parcel.outer)),
            tuple(tuple(reversed(hole)) for hole in parcel.holes),
        )
        for parcel in parcels
    )
