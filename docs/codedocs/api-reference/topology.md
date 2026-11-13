---
title: "Topology"
description: "Reference for the topology preserving boundary simplification in pygeoif.topology."
---

`pygeoif.topology` simplifies the boundaries of neighbouring polygons without
breaking their shared topology. It is meant for cleaning cadastral parcels and
similar boundary networks where adjacent polygons must keep describing the same
common edge.

Import path: `from pygeoif.topology import simplify_polygons`

## Public Functions

### `simplify_polygons`

```python
simplify_polygons(
    polygons: Sequence[PolygonType],
    tolerance: float,
    preserve: Iterable[PointType] | None = None,
) -> tuple[PolygonType, ...]
```

Simplifies every ring of the input polygons while keeping their topology
consistent.

Boundaries shared between two polygons are detected even when one side carries
additional sampling points or when the boundary is registered in the opposite
direction. Each shared boundary is merged into one coordinate sequence and
simplified exactly once, so neighbouring polygons keep identical, reversely
oriented coordinate sequences along their common edge.

Coordinates passed as `preserve` (for example survey markers that must be
kept) are never removed. Private stretches are simplified with the same
Douglas-Peucker `tolerance`. If a replacement chord would make a ring
self-intersecting, removed points are restored one by one until every ring is
simple again, so a narrow corridor cannot collapse into a crossing.

## Example

```python
from pygeoif.topology import simplify_polygons

# parcel A walks the shared edge bottom to top,
# parcel B registers the same edge top to bottom
parcel_a = (((0, 0), (0, 1), (1, 2), (2, 1), (2, 0), (0, 0)),)
parcel_b = (((2, 1), (1, 2), (0, 1), (0, 3), (2, 3), (2, 1)),)

simplified = simplify_polygons([parcel_a, parcel_b], tolerance=0.1)
```

## Notes

- The function only removes interior coordinates; it never adds or moves points.
- Shared edges are matched geometrically, so direction and sampling density do
  not have to match between adjacent polygons.
- Output rings stay closed and keep the starting coordinate of their input ring.
- See [Functions](/docs/api-reference/functions) for the underlying
  `simplify_line` and `is_simple_ring` primitives.

## Related API

- [Functions](/docs/api-reference/functions)
- [Polygon](/docs/api-reference/polygon)
- [Types And Protocols](/docs/api-reference/types-and-protocols)
