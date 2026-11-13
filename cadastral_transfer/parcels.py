"""Parcel model and loading of local geometry directories.

A batch (delivery) is a directory of geometry files. Supported formats:

- ``*.geojson`` / ``*.json``: GeoJSON FeatureCollection, Feature or bare
  geometry. The parcel identifier is taken from the feature ``id`` or the
  properties ``id`` / ``name`` / ``宗地编号``, falling back to the file stem.
- ``*.wkt``: one WKT geometry per line; a single-geometry file uses the file
  stem as identifier, otherwise ``<stem>#<n>``.

Identifiers are only labels for the report. Parcels are never matched by
name across batches: all transfers are measured by geometric overlap, so a
recycled identifier in the new batch does not inherit any identity.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence

from pygeoif.factories import from_wkt, shape
from pygeoif.geometry import Geometry, MultiPolygon, Polygon

Ring = tuple[tuple[float, float], ...]

_ID_PROPERTIES = ("id", "name", "宗地编号", "宗地号", "编号")


@dataclass(frozen=True)
class Parcel:
    """A cadastral parcel: an identifier plus one or more closed rings.

    The first ring of each polygon part is its shell, subsequent rings are
    holes. Rings are closed (last point repeats the first).
    """

    parcel_id: str
    rings: tuple[Ring, ...]
    parts: tuple[int, ...]  # number of rings per polygon part (shell + holes)

    @property
    def area(self) -> float:
        """Planimetric area: shells minus holes, summed over parts."""
        total = 0.0
        start = 0
        for count in self.parts:
            shell = self.rings[start]
            total += abs(_signed_area(shell))
            for ring in self.rings[start + 1 : start + count]:
                total -= abs(_signed_area(ring))
            start += count
        return total

    @property
    def edges(self) -> Iterator[tuple[float, float, float, float]]:
        """Yield all ring edges as (x1, y1, x2, y2)."""
        for ring in self.rings:
            for k in range(len(ring) - 1):
                x1, y1 = ring[k]
                x2, y2 = ring[k + 1]
                yield x1, y1, x2, y2


def _signed_area(ring: Ring) -> float:
    return 0.5 * sum(
        x1 * y2 - x2 * y1
        for (x1, y1), (x2, y2) in zip(ring, ring[1:])
    )


def _close(ring: Sequence[Sequence[float]]) -> Ring:
    points = tuple((float(p[0]), float(p[1])) for p in ring)
    if points and points[0] != points[-1]:
        points = points + (points[0],)
    return points


def _rings_of(geometry: Geometry) -> tuple[tuple[Ring, ...], tuple[int, ...]]:
    """Flatten a Polygon/MultiPolygon into rings and part sizes."""
    if isinstance(geometry, Polygon):
        polygons: Sequence[Polygon] = (geometry,)
    elif isinstance(geometry, MultiPolygon):
        polygons = tuple(geometry.geoms)
    else:
        msg = f"unsupported geometry type: {geometry.geom_type}"
        raise ValueError(msg)
    rings: list[Ring] = []
    parts: list[int] = []
    for polygon in polygons:
        part = [_close(polygon.exterior.coords)]
        part.extend(_close(ring.coords) for ring in polygon.interiors)
        rings.extend(part)
        parts.append(len(part))
    return tuple(rings), tuple(parts)


def _parcels_from_geojson(data: dict, stem: str) -> list[Parcel]:
    def make(geometry: Geometry, parcel_id: str) -> Parcel:
        rings, parts = _rings_of(geometry)
        return Parcel(parcel_id=parcel_id, rings=rings, parts=parts)

    def feature_id(feature: dict, fallback: str) -> str:
        props = feature.get("properties") or {}
        for key in _ID_PROPERTIES:
            if props.get(key) is not None:
                return str(props[key])
        if feature.get("id") is not None:
            return str(feature["id"])
        return fallback

    kind = data.get("type")
    if kind == "FeatureCollection":
        return [
            make(shape(feature["geometry"]), feature_id(feature, f"{stem}#{k}"))
            for k, feature in enumerate(data["features"], start=1)
        ]
    if kind == "Feature":
        return [make(shape(data["geometry"]), feature_id(data, stem))]
    return [make(shape(data), stem)]


def _parcels_from_wkt(text: str, stem: str) -> list[Parcel]:
    try:
        geometry = from_wkt(text.strip())
    except Exception:  # noqa: BLE001 - fall back to one geometry per line
        geometry = None
    geometries = (
        [geometry]
        if geometry is not None
        else [from_wkt(line) for line in text.splitlines() if line.strip()]
    )
    parcels = []
    for k, geom in enumerate(geometries, start=1):
        if geom is None:
            continue
        rings, parts = _rings_of(geom)
        parcel_id = stem if len(geometries) == 1 else f"{stem}#{k}"
        parcels.append(Parcel(parcel_id=parcel_id, rings=rings, parts=parts))
    return parcels


def load_parcels(directory: str | Path) -> list[Parcel]:
    """Load all parcels of one delivery batch from a geometry directory."""
    root = Path(directory)
    if not root.is_dir():
        msg = f"not a directory: {root}"
        raise NotADirectoryError(msg)
    parcels: list[Parcel] = []
    files = sorted(
        p
        for p in root.iterdir()
        if p.suffix.lower() in {".geojson", ".json", ".wkt"}
    )
    if not files:
        msg = f"no .geojson/.json/.wkt files found in {root}"
        raise FileNotFoundError(msg)
    for path in files:
        if path.suffix.lower() == ".wkt":
            batch = _parcels_from_wkt(path.read_text(encoding="utf-8"), path.stem)
        else:
            batch = _parcels_from_geojson(
                json.loads(path.read_text(encoding="utf-8")), path.stem
            )
        parcels.extend(batch)
    seen: set[str] = set()
    for parcel in parcels:
        if parcel.parcel_id in seen:
            msg = f"duplicate parcel id in batch: {parcel.parcel_id!r}"
            raise ValueError(msg)
        seen.add(parcel.parcel_id)
    return parcels
