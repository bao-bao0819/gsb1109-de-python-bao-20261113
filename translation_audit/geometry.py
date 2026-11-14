"""Geometry helpers: vertex extraction, shape signatures, translation boxes."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Union

from pygeoif.factories import from_wkt
from pygeoif.factories import shape

Vec = tuple[float, float]
# (min_dx, min_dy, max_dx, max_dy): every admissible displacement lies inside.
Box = tuple[float, float, float, float]
GeometrySpec = Union[str, dict]


def parse_geometry(spec: GeometrySpec):
    """Build a pygeoif geometry from WKT text or a GeoJSON-like mapping."""
    if isinstance(spec, str):
        geometry = from_wkt(spec)
    elif isinstance(spec, dict):
        geometry = shape(spec)
    else:
        msg = f"Unsupported geometry spec: {type(spec)!r}"
        raise TypeError(msg)
    if geometry is None:
        msg = "Empty geometry spec"
        raise ValueError(msg)
    return geometry


def _iter_coords(coords) -> Iterable[tuple[float, ...]]:
    if not coords:
        return
    first = coords[0]
    if isinstance(first, (int, float)):
        yield tuple(float(v) for v in coords)
        return
    for item in coords:
        yield from _iter_coords(item)


def all_vertices(geometry) -> list[Vec]:
    """All (x, y) control vertices of a geometry, duplicates and order kept."""
    coords = geometry.__geo_interface__["coordinates"]
    return [(x, y) for x, y, *_ in _iter_coords(coords)]


def _part_count(coords) -> int:
    """Number of linear parts (rings/line strings/points) in a coordinate tree."""
    if not coords:
        return 0
    first = coords[0]
    if isinstance(first, (int, float)):
        return 1
    if first and isinstance(first[0], (int, float)):
        return len(coords)
    return sum(_part_count(item) for item in coords)


def shape_signature(geometry) -> tuple[str, int, int]:
    """Coarse shape key: geometry type, linear part count, vertex count.

    Outlines with the same key are candidate translation pairs; the per-vertex
    tolerance box then decides whether a common translation actually exists.
    Exact coordinate equality is deliberately not required because the error
    bound permits small shape deviations.
    """
    interface = geometry.__geo_interface__
    coords = interface["coordinates"]
    return interface["type"], _part_count(coords), len(all_vertices(geometry))


def as_tolerance_xy(tolerance: float | tuple[float, float]) -> Vec:
    if isinstance(tolerance, (int, float)):
        return float(tolerance), float(tolerance)
    ex, ey = tolerance
    return float(ex), float(ey)


def translation_box(old: Vec, new: Vec, tolerance: float | tuple[float, float]) -> Box:
    """Feasible displacement box for one matched control-vertex pair."""
    ex, ey = as_tolerance_xy(tolerance)
    dx = new[0] - old[0]
    dy = new[1] - old[1]
    return dx - ex, dy - ey, dx + ex, dy + ey


def intersect_boxes(boxes: Iterable[Box]) -> Box | None:
    """Intersection of axis-aligned displacement boxes, or None when empty."""
    boxes = list(boxes)
    if not boxes:
        msg = "Cannot intersect an empty set of boxes"
        raise ValueError(msg)
    min_dx = max(box[0] for box in boxes)
    min_dy = max(box[1] for box in boxes)
    max_dx = min(box[2] for box in boxes)
    max_dy = min(box[3] for box in boxes)
    if min_dx > max_dx or min_dy > max_dy:
        return None
    return min_dx, min_dy, max_dx, max_dy


def box_center(box: Box) -> Vec:
    return (box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0


def box_half_extents(box: Box) -> Vec:
    return (box[2] - box[0]) / 2.0, (box[3] - box[1]) / 2.0


def box_contains(box: Box, point: Vec) -> bool:
    return box[0] <= point[0] <= box[2] and box[1] <= point[1] <= box[3]


def within_tolerance(
    old: Vec,
    new: Vec,
    translation: Vec,
    tolerance: float | tuple[float, float],
) -> bool:
    ex, ey = as_tolerance_xy(tolerance)
    dx = new[0] - old[0] - translation[0]
    dy = new[1] - old[1] - translation[1]
    return abs(dx) <= ex and abs(dy) <= ey


def perfect_bipartite_match(feasible_edges: dict[str, set[str]]) -> bool:
    """Augmenting-path check: every left node gets a distinct right node."""
    match_right: dict[str, str] = {}

    def augment(left: str, seen: set[str]) -> bool:
        for right in feasible_edges.get(left, ()):
            if right in seen:
                continue
            seen.add(right)
            if right not in match_right or augment(match_right[right], seen):
                match_right[right] = left
                return True
        return False

    for left in feasible_edges:
        if not augment(left, set()):
            return False
    return True
