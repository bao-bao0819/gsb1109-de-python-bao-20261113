---
title: "Identity"
description: "Auditable geometric identity comparison with pygeoif.identity.compare_identity."
---

`pygeoif.identity` decides whether two geometries describe the *same shape*, even when their coordinates were transcribed differently, and it explains its verdict so the result can be audited.

Import path: `from pygeoif.identity import compare_identity, IdentityMatch, IdentityVerdict, Correspondence, RingCorrespondence, VertexAlignment`

## The Problem

The same parcel of land can be written down in many equivalent ways:

- a ring may start at any vertex (cyclic rotation),
- a ring may be traversed in either direction (reversal),
- the holes of a polygon may be listed in any order,
- the parts of a multi-part geometry may be listed in any order.

A coordinate-by-coordinate comparison (`__eq__`) treats those transcriptions as different objects, so a ledger would record the same parcel twice.

## `compare_identity`

```python
compare_identity(first, second, *, grid_size: float | None = None) -> IdentityMatch
```

Compares two geometries (pygeoif objects, anything implementing `__geo_interface__`, or GeoJSON-like mappings) and returns an `IdentityMatch`.

- Rings compare up to rotation and reversal.
- Interior rings (holes) and multi-part members compare up to permutation.
- Without `grid_size`, coordinates must match exactly.
- With `grid_size`, coordinates are snapped to the nearest grid point first, which absorbs re-transcription noise.

### Quantization can be undecidable

Quantization is lossy. If snapping to the grid collapses a ring — for example a narrow hole pressed into a line or a point — the information needed to decide was destroyed. In that case the verdict is `IdentityVerdict.UNDECIDABLE`, never a false equivalence. The same applies when quantization merges distinct points of a multi-point or collapses a line to a single grid point.

### The audit trail

`IdentityMatch` carries:

- `verdict`: `IDENTICAL`, `DIFFERENT`, or `UNDECIDABLE` (`bool(match)` is `True` only for `IDENTICAL`).
- `reasons`: plain-language explanations of the verdict.
- `correspondences`: for identical geometries, how the parts correspond. Each `Correspondence` has a `geometry_map` (part index pairs for multi-part geometries) and `rings`, a tuple of `RingCorrespondence` locating each ring by path (for example `("exterior",)`, `("interior", 1)`, or `(2, "interior", 0)` for a hole of the third polygon of a multi-polygon).
- `truncated`: `True` when more than `MAX_CORRESPONDENCES` (64) correspondences exist.

Each `RingCorrespondence` holds every consistent `VertexAlignment`: the rotation `offset`, whether the second ring `is_reversed`, and an explicit `vertex_map` of `(first_index, second_index)` pairs (vertices are indexed without the duplicated closing vertex). Repeated vertices or repeated rings produce several alignments or several correspondences — all of them are reported.

## Example

```python
from pygeoif import Polygon, compare_identity

a = Polygon(
    [(0, 0), (4, 0), (4, 4), (0, 4), (0, 0)],
    holes=[[(1, 1), (2, 1), (2, 2), (1, 2), (1, 1)]],
)
# Same parcel: different starting vertex, reversed exterior, reversed hole.
b = Polygon(
    [(4, 4), (4, 0), (0, 0), (0, 4), (4, 4)],
    holes=[[(2, 2), (2, 1), (1, 1), (1, 2), (2, 2)]],
)

match = compare_identity(a, b)
assert match.verdict == "identical"
print(match.reasons[0])
# Exterior rings coincide (1 vertex alignment(s)).
alignment = match.correspondences[0].rings[0].alignments[0]
assert alignment.is_reversed
```
