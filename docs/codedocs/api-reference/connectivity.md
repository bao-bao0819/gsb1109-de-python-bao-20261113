---
title: "Connectivity"
description: "Export three dimensional road lines to two dimensions while keeping node connectivity decided from the original elevations."
---

`pygeoif.connectivity` keeps the elevation information that is lost when road lines are projected to two dimensions. Planar crossings are computed on the XY projection, but the Z value at each crossing is interpolated separately along both lines from their original three dimensional coordinates. A crossing that falls in the middle of a segment therefore uses the interpolated elevation at that position, not the elevation of the nearest vertex.

Import path: `from pygeoif import ConnectionStatus, ConnectivityExport, classify_connection, export_connectivity, line_intersections, node_connectivity`

## Public Functions

### `classify_connection`

```python
classify_connection(
    z_a: float,
    z_b: float,
    *,
    connect_tolerance: float = 0.0,
    separation_tolerance: float | None = None,
) -> ConnectionStatus
```

Classifies the absolute elevation difference. A difference within `connect_tolerance` is `CONNECTED` (明确可接). A difference greater than `separation_tolerance` is `SEPARATED` (明确分离). Everything in between is `REVIEW` (待核查). When `separation_tolerance` is omitted it defaults to `2 * connect_tolerance`, which models two elevations that each carry the same symmetric error band.

### `line_intersections`

```python
line_intersections(
    first: LineType,
    second: LineType,
    *,
    line_a: str = "a",
    line_b: str = "b",
    xy_tolerance: float = 1e-9,
) -> tuple[Intersection, ...]
```

Returns planar intersections with `z_a` and `z_b` interpolated along the corresponding segments. Each intersection also carries the chainage (`distance_a`, `distance_b`) and the normalized fraction along its line. Collinear overlaps produce their two overlap endpoints with `kind="overlap"`; endpoint contacts use `kind="touch"`.

### `node_connectivity`

```python
node_connectivity(
    first: LineType,
    second: LineType,
    *,
    line_a: str = "a",
    line_b: str = "b",
    connect_tolerance: float = 0.0,
    separation_tolerance: float | None = None,
    xy_tolerance: float = 1e-9,
) -> tuple[NodeConnection, ...]
```

Combines `line_intersections()` with `classify_connection()`. Each `NodeConnection` names the node on both lines and exposes `status`, `z_difference`, `is_connected`, `is_separated`, and `needs_review`.

### `export_connectivity`

```python
export_connectivity(
    lines: Mapping[str, LineType] | Sequence[LineType],
    *,
    connect_tolerance: float = 0.0,
    separation_tolerance: float | None = None,
    xy_tolerance: float = 1e-9,
) -> ConnectivityExport
```

Builds a `ConnectivityExport`. The `features` property contains GeoJSON-like LineString features with the Z values projected away, which is the payload the statistics backend consumes. The `nodes`, `edges`, and `connections` properties carry the graph. Every crossing splits its lines into graph edges and adds a `transfer` edge whose `status` is the elevation decision.

## ConnectivityExport

| Name | Type | Description |
|---|---|---|
| `features` | `tuple[dict, ...]` | Two dimensional GeoJSON-like features with a line `id`. |
| `nodes` | `tuple[Node, ...]` | Line starts, line ends, and crossings with their original Z value. |
| `edges` | `tuple[Edge, ...]` | `line` edges inside one line and `transfer` edges between lines. |
| `connections` | `tuple[NodeConnection, ...]` | The elevation classification for every planar crossing. |
| `line_ids` | `tuple[str, ...]` | Identifiers of the exported lines. |
| `line_adjacency(allow_review=False)` | `dict[str, set[str]]` | Line level adjacency graph. |
| `can_travel(start_line, end_line, allow_review=False)` | `bool` | Whether a path exists through traversable crossings. |
| `__geo_interface__` | `dict` | FeatureCollection plus a `connectivity` payload. |
| `as_dict()` | `dict` | JSON friendly mapping of the geo interface. |

## Examples

A ramp crosses a highway at mid-segment. The highway rises from 10 m to 20 m, so the elevation at the crossing is 15 m on both lines and the nodes are clearly connectable.

```python
from pygeoif import export_connectivity

export = export_connectivity(
    {
        "highway": [(0, 0, 10), (10, 0, 20)],
        "ramp": [(5, -5, 15), (5, 5, 15)],
    },
    connect_tolerance=0.5,
)

print(export.connections[0].status.value)
print(export.connections[0].z_a, export.connections[0].z_b)
print(export.can_travel("highway", "ramp"))
```

Expected output:

```text
connected
15.0 15.0
True
```

A crossing with an elevation difference inside the uncertainty band is not traversable by default:

```python
export = export_connectivity(
    {
        "a": [(0, 0, 10), (10, 0, 10)],
        "b": [(5, -5, 10.7), (5, 5, 10.7)],
    },
    connect_tolerance=0.5,
    separation_tolerance=1.0,
)

print(export.connections[0].status.value)
print(export.can_travel("a", "b"))
print(export.can_travel("a", "b", allow_review=True))
```

Expected output:

```text
review
False
True
```

## Direction Independence

Line coordinates are normalized to a canonical direction before any chainage, node identifier, or graph edge is calculated. Reversing the coordinate order of one or both input lines produces the same connections, the same node identifiers, and the same reachable paths. The exported features use the canonical direction as well, so reversing an input line does not change the export payload.

## Notes

- Input lines must be three dimensional; 2D input raises `DimensionError` instead of silently treating missing Z values as zero.
- Elevations are interpolated along the XY segment. Vertical or near vertical segments are supported as long as their XY projection has a non-zero length.
- `xy_tolerance` controls planar matching. It is deliberately separate from the elevation tolerances, so a tight elevation band does not make geometric snapping stricter.
- Only `CONNECTED` transfers are traversable by default. `REVIEW` transfers require `allow_review=True`, and `SEPARATED` transfers are never traversable.

## Related API

- [LineString](/docs/api-reference/linestring)
- [Factories And Conversion](/docs/factories-and-conversion)
- [Geometry Model](/docs/geometry-model)
