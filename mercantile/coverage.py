"""Spherical-area coverage accounting for XYZ tiles.

The functions in this module answer questions such as "what fraction of
this survey region has been surveyed, measured on the sphere?" They also
produce a reconciliation against the traditional equal-weight tile count,
which is what a daily report gets when it divides surveyed tiles by total
tiles.

Tile sets may mix zoom levels and records may be clipped at a latitude, so
the input is normalized to latitude bands and longitude intervals before
measures are summed. This avoids enumerating the descendants of a coarse
tile.
"""

from __future__ import division

import math
from collections import namedtuple

from . import (
    RE,
    LngLatBbox,
    MercantileError,
    _parse_tile_arg,
    bounds as _bounds,
    children as _children,
)


_EPS = 1e-12


class CoverageError(MercantileError):
    """Raised when coverage accounting input is invalid."""


class CoverageTile(
    namedtuple("CoverageTile", ["tile", "south", "north", "label", "order"])
):
    """A tile, optionally clipped to a latitude range.

    Parameters
    ----------
    tile : Tile or sequence of int
        An XYZ tile.
    south, north : float, optional
        Latitude limits in decimal degrees. The limits are intersected
        with the tile's own latitude bounds. If omitted, the tile's
        bounds are used.
    label : hashable, optional
        Name of the region used in coverage contribution reports. The
        default is ``"z/x/y"``.
    order : int, optional
        Input order, used to break attribution ties.

    Attributes
    ----------
    tile : Tile
        The underlying XYZ tile.
    south, north : float
        The actual latitude extent after clipping.
    label : hashable
        Region label.
    """

    __slots__ = ()

    def __new__(cls, tile, south=None, north=None, label=None, order=0):
        # Accept both CoverageTile(x, y, z) and CoverageTile((x, y, z)).
        if (
            isinstance(tile, int)
            and isinstance(south, int)
            and isinstance(north, int)
        ):
            tile, south, north = (tile, south, north), None, None

        tile = _parse_tile_arg(tile)
        tile_bounds = _bounds(tile)

        if south is None:
            south = tile_bounds.south
        else:
            south = float(south)
            if south < tile_bounds.south:
                south = tile_bounds.south

        if north is None:
            north = tile_bounds.north
        else:
            north = float(north)
            if north > tile_bounds.north:
                north = tile_bounds.north

        if not south < north:
            raise CoverageError(
                "latitude extent is empty for tile {0}: south={1!r}, "
                "north={2!r}".format(tile, south, north)
            )

        if label is None:
            label = "{0}/{1}/{2}".format(tile.z, tile.x, tile.y)

        return tuple.__new__(cls, [tile, south, north, label, order])

    @property
    def west(self):
        """Western longitude of the tile."""
        return _bounds(self.tile).west

    @property
    def east(self):
        """Eastern longitude of the tile."""
        return _bounds(self.tile).east

    @property
    def bounds(self):
        """Geographic bounding box after latitude clipping."""
        return LngLatBbox(self.west, self.south, self.east, self.north)

    @property
    def is_partial(self):
        """True if the record is clipped by a latitude limit."""
        tile_bounds = _bounds(self.tile)
        return self.south > tile_bounds.south or self.north < tile_bounds.north

    def area(self, radius=RE):
        """Spherical area in square units of ``radius``.

        Parameters
        ----------
        radius : float, optional
            Sphere radius in metres. The default is the Web Mercator
            sphere radius, 6378137.0 m.

        Returns
        -------
        float
            Area in square metres when using the default radius.

        Examples
        --------
        >>> round(CoverageTile(0, 0, 0).area() / 1e6)
        509302149
        """
        return _spherical_area_bounds(
            self.west, self.south, self.east, self.north, radius
        )

    def count(self, count_zoom, clipped=False):
        """Equal-weight tile count at ``count_zoom``.

        A full tile at ``count_zoom`` has a count of 1. Coarser tiles
        count as their number of descendants and finer tiles count as a
        fraction of a tile.

        Parameters
        ----------
        count_zoom : int
            Reference zoom level.
        clipped : bool, optional
            If False (the default), a latitude-clipped record counts as
            its whole tile, which is how a daily tile count treats a
            partially surveyed block. If True, count only the clipped
            latitude fraction, measured in Web Mercator.

        Returns
        -------
        float
        """
        _validate_zoom(count_zoom)
        if clipped:
            west, south, east, north = self.bounds
        else:
            west, south, east, north = _bounds(self.tile)
        return _count_bounds(west, south, east, north, count_zoom)

    def children(self):
        """Return the four children, clipped to this record's latitude range.

        The four children partition the parent exactly in tile space.
        Children outside a latitude-clipped record are omitted.

        Returns
        -------
        list
            List of :class:`CoverageTile` objects.
        """
        result = []
        for child in _children(self.tile):
            child_bounds = _bounds(child)
            south = max(child_bounds.south, self.south)
            north = min(child_bounds.north, self.north)
            if south < north:
                result.append(CoverageTile(child, south, north, self.label))
        return result

    def to_dict(self):
        """Return a JSON-serializable representation."""
        return {
            "tile": list(self.tile),
            "south": self.south,
            "north": self.north,
            "label": self.label,
        }


class Contribution(
    namedtuple(
        "Contribution",
        [
            "label",
            "area",
            "count",
            "area_share",
            "count_share",
            "difference",
            "difference_pp",
            "rank",
        ],
    )
):
    """One region's contribution to the coverage difference.

    ``difference`` is ``area_share - count_share`` and ``difference_pp``
    is the same value in percentage points. A negative value means the
    traditional tile-count basis gives this region a larger share than
    the spherical-area basis.
    """

    __slots__ = ()

    def as_dict(self):
        """Return a JSON-serializable representation."""
        return {
            "label": self.label,
            "area_m2": self.area,
            "area_km2": self.area / 1e6,
            "count": self.count,
            "area_share": self.area_share,
            "count_share": self.count_share,
            "difference": self.difference,
            "difference_pp": self.difference_pp,
            "rank": self.rank,
        }


class CoverageReport(
    namedtuple(
        "CoverageReport",
        [
            "total_area",
            "surveyed_area",
            "total_count",
            "surveyed_count",
            "area_coverage",
            "count_coverage",
            "difference",
            "difference_pp",
            "relative_difference",
            "count_zoom",
            "count_partial",
            "radius",
            "outside_area",
            "outside_count",
            "contributions",
        ],
    )
):
    """Reconciliation of spherical-area and tile-count coverage.

    ``surveyed_area`` and ``surveyed_count`` are clipped to the total
    region. ``outside_area`` and ``outside_count`` report surveyed
    records that fall outside the total region.

    ``contributions`` is sorted by absolute ``difference_pp``, largest
    first. The sum of the contributions' ``difference_pp`` equals
    ``difference_pp``.
    """

    __slots__ = ()

    def as_dict(self, top=None):
        """Return a JSON-serializable representation.

        Parameters
        ----------
        top : int, optional
            Limit ``contributions`` to the first ``top`` entries. The
            default is to include all contributions.
        """
        contributions = self.contributions
        if top is not None:
            contributions = contributions[:top]

        return {
            "count_zoom": self.count_zoom,
            "count_partial": self.count_partial,
            "radius": self.radius,
            "total_area_m2": self.total_area,
            "total_area_km2": self.total_area / 1e6,
            "surveyed_area_m2": self.surveyed_area,
            "surveyed_area_km2": self.surveyed_area / 1e6,
            "uncovered_area_m2": self.total_area - self.surveyed_area,
            "uncovered_area_km2": (
                self.total_area - self.surveyed_area
            ) / 1e6,
            "outside_area_m2": self.outside_area,
            "outside_area_km2": self.outside_area / 1e6,
            "total_count": self.total_count,
            "surveyed_count": self.surveyed_count,
            "outside_count": self.outside_count,
            "area_coverage": self.area_coverage,
            "area_coverage_pct": self.area_coverage * 100.0,
            "count_coverage": self.count_coverage,
            "count_coverage_pct": self.count_coverage * 100.0,
            "difference": self.difference,
            "difference_pp": self.difference_pp,
            "relative_difference": self.relative_difference,
            "contributions": [item.as_dict() for item in contributions],
        }

    def top_contributions(self, n=5):
        """Return the ``n`` largest absolute contributors."""
        return self.contributions[:n]


def spherical_area(tile, south=None, north=None, radius=RE):
    """Area of a tile or latitude-clipped tile on a sphere.

    Parameters
    ----------
    tile : Tile or sequence of int
        An XYZ tile.
    south, north : float, optional
        Latitude limits in decimal degrees, intersected with the tile.
    radius : float, optional
        Sphere radius in metres.

    Returns
    -------
    float
        Area in square metres when using the default radius.

    Examples
    --------
    >>> round(spherical_area((0, 0, 0)) / 1e6)
    509302149
    >>> round(spherical_area((0, 0, 1), north=0.0) / 1e6)
    127325537
    """
    tile_bounds = _bounds(_parse_tile_arg(tile))
    if south is not None:
        south = max(float(south), tile_bounds.south)
    else:
        south = tile_bounds.south
    if north is not None:
        north = min(float(north), tile_bounds.north)
    else:
        north = tile_bounds.north

    if not south < north:
        return 0.0

    return CoverageTile(tile, south, north).area(radius)


def coverage_report(
    total, surveyed, count_zoom=None, count_partial="tile", radius=RE
):
    """Reconcile tile-count and spherical-area coverage.

    Parameters
    ----------
    total : iterable
        Records defining the full survey region. A record may be a
        :class:`CoverageTile`, a 3-item tile, or a mapping with ``tile``,
        optional ``south``, optional ``north``, and optional ``label``.
    surveyed : iterable
        Records defining the surveyed part of the region. Same forms as
        ``total``. Surveyed records may overlap each other and may use
        different zoom levels; the union is computed.
    count_zoom : int, optional
        Zoom level for the equal-weight count basis. The default is the
        maximum zoom level in ``total``.
    count_partial : {"tile", "clip"}, optional
        How a latitude-clipped surveyed record contributes to the tile
        count. ``"tile"`` (the default) counts the whole tile, matching a
        daily report that counts a partially surveyed block as surveyed.
        ``"clip"`` counts only the clipped latitude fraction in Web
        Mercator.
    radius : float, optional
        Sphere radius in metres.

    Returns
    -------
    CoverageReport

    Notes
    -----
    The traditional count basis is the number of equal-weight tiles at
    ``count_zoom``. This is the continuous Web Mercator planar measure
    normalized so that one ``count_zoom`` tile has weight 1. For full
    tiles at ``count_zoom`` it is exactly the tile count used by a daily
    report. Fractional counts arise only for mixed zoom levels or when
    ``count_partial="clip"``.

    Overlapping records are counted once. For contribution attribution,
    the finest tile containing a point wins; ties are resolved by input
    order.
    """
    total_records = [
        _as_coverage_tile(obj, index) for index, obj in enumerate(total)
    ]
    surveyed_records = [
        _as_coverage_tile(obj, index) for index, obj in enumerate(surveyed)
    ]

    if not total_records:
        raise CoverageError("total must contain at least one tile")

    if count_zoom is None:
        count_zoom = max(record.tile.z for record in total_records)
    _validate_zoom(count_zoom)

    if count_partial not in ("tile", "clip"):
        raise CoverageError("count_partial must be 'tile' or 'clip'")

    if radius <= 0:
        raise CoverageError("radius must be positive")

    area = _measure(
        total_records,
        surveyed_records,
        kind="area",
        radius=radius,
        clipped=True,
    )
    count = _measure(
        total_records,
        surveyed_records,
        kind="count",
        count_zoom=count_zoom,
        clipped=(count_partial == "clip"),
    )

    if area[0] <= 0:
        raise CoverageError("total region has zero area")
    if count[0] <= 0:
        raise CoverageError("total region has zero tile count")

    total_area, surveyed_area, outside_area, label_area = area
    total_count, surveyed_count, outside_count, label_count = count

    area_coverage = surveyed_area / total_area
    count_coverage = surveyed_count / total_count
    difference = area_coverage - count_coverage

    if count_coverage:
        relative_difference = difference / count_coverage
    else:
        relative_difference = None

    contributions = []
    for label in set(label_area) | set(label_count):
        label_measure = label_area.get(label, 0.0)
        label_tiles = label_count.get(label, 0.0)
        area_share = label_measure / total_area
        count_share = label_tiles / total_count
        label_difference = area_share - count_share
        contributions.append(
            Contribution(
                label=label,
                area=label_measure,
                count=label_tiles,
                area_share=area_share,
                count_share=count_share,
                difference=label_difference,
                difference_pp=label_difference * 100.0,
                rank=0,
            )
        )

    contributions.sort(
        key=lambda item: (-abs(item.difference_pp), str(item.label))
    )
    contributions = [
        item._replace(rank=index + 1)
        for index, item in enumerate(contributions)
    ]

    return CoverageReport(
        total_area=total_area,
        surveyed_area=surveyed_area,
        total_count=total_count,
        surveyed_count=surveyed_count,
        area_coverage=area_coverage,
        count_coverage=count_coverage,
        difference=difference,
        difference_pp=difference * 100.0,
        relative_difference=relative_difference,
        count_zoom=count_zoom,
        count_partial=count_partial,
        radius=radius,
        outside_area=max(0.0, outside_area),
        outside_count=max(0.0, outside_count),
        contributions=tuple(contributions),
    )


def _as_coverage_tile(obj, order=0):
    if isinstance(obj, CoverageTile):
        return obj

    if isinstance(obj, dict):
        if "tile" in obj:
            tile = obj["tile"]
        elif all(key in obj for key in ("x", "y", "z")):
            tile = (obj["x"], obj["y"], obj["z"])
        else:
            raise CoverageError(
                "record must contain a 'tile' array or 'x', 'y', and 'z'"
            )
        try:
            return CoverageTile(
                tile,
                obj.get("south"),
                obj.get("north"),
                obj.get("label"),
                order,
            )
        except (TypeError, ValueError) as exc:
            raise CoverageError(str(exc))

    if isinstance(obj, (list, tuple)) and len(obj) == 3:
        try:
            return CoverageTile(obj, order=order)
        except (TypeError, ValueError) as exc:
            raise CoverageError(str(exc))

    raise CoverageError(
        "record must be a CoverageTile, a 3-item tile, or a mapping"
    )


def _validate_zoom(zoom):
    if int(zoom) != zoom or zoom < 0:
        raise CoverageError("count_zoom must be a non-negative integer")


def _spherical_area_bounds(west, south, east, north, radius=RE):
    if east <= west or north <= south:
        return 0.0
    return (
        radius * radius * math.radians(east - west)
        * (math.sin(math.radians(north)) - math.sin(math.radians(south)))
    )


def _mercator_y(lat):
    sinlat = math.sin(math.radians(lat))
    return 0.5 - 0.25 * math.log((1.0 + sinlat) / (1.0 - sinlat)) / math.pi


def _count_bounds(west, south, east, north, count_zoom):
    if east <= west or north <= south:
        return 0.0
    return (
        (east - west) / 360.0
        * (_mercator_y(south) - _mercator_y(north))
        * math.pow(4.0, count_zoom)
    )


def _measure(
    total_records,
    surveyed_records,
    kind,
    radius=RE,
    count_zoom=None,
    clipped=True,
):
    """Measure total, covered, outside, and per-label covered amounts."""
    all_records = total_records + surveyed_records
    bands = _latitude_bands(all_records, clipped=clipped)

    starts_total = _records_by_boundary(total_records, clipped)
    starts_surveyed = _records_by_boundary(surveyed_records, clipped)

    active_total = []
    active_surveyed = []
    total_measure = 0.0
    covered_measure = 0.0
    universe_measure = 0.0
    label_measure = {}

    for low, high in bands:
        # Sweep: a record covers [low, high] only while north > low.
        active_total = [
            record for record in active_total if record[1] > low + _EPS
        ]
        active_total.extend(starts_total.get(low, ()))
        active_surveyed = [
            record for record in active_surveyed if record[1] > low + _EPS
        ]
        active_surveyed.extend(starts_surveyed.get(low, ()))

        if kind == "area":
            factor = (
                radius * radius * math.radians(1.0)
                * (
                    math.sin(math.radians(high))
                    - math.sin(math.radians(low))
                )
            )
        else:
            factor = (
                (_mercator_y(low) - _mercator_y(high))
                * math.pow(4.0, count_zoom)
                / 360.0
            )

        total_intervals = _merge_intervals(
            [_record_span(record[0], clipped) for record in active_total]
        )
        surveyed_intervals = _merge_intervals(
            [_record_span(record[0], clipped) for record in active_surveyed]
        )

        total_measure += _intervals_width(total_intervals) * factor
        universe_measure += _intervals_width(surveyed_intervals) * factor

        if not total_intervals or not surveyed_intervals:
            continue

        covered_measure += (
            _intersection_width(total_intervals, surveyed_intervals)
            * factor
        )

        _attribute(
            [record[0] for record in active_surveyed],
            total_intervals,
            factor,
            label_measure,
            clipped,
        )

    return (
        total_measure,
        covered_measure,
        max(0.0, universe_measure - covered_measure),
        label_measure,
    )


def _records_by_boundary(records, clipped):
    result = {}
    for record in records:
        low, high = _extent(record, clipped)
        result.setdefault(low, []).append((record, high))
    return result


def _extent(record, clipped):
    if clipped:
        return record.south, record.north
    tile_bounds = _bounds(record.tile)
    return tile_bounds.south, tile_bounds.north


def _record_span(record, clipped):
    if clipped:
        return (record.west, record.east)
    tile_bounds = _bounds(record.tile)
    return (tile_bounds.west, tile_bounds.east)


def _unique_sorted(values):
    values = sorted(values)
    result = []
    for value in values:
        if not result or value - result[-1] > _EPS:
            result.append(value)
    return result


def _latitude_bands(records, clipped=True):
    values = []
    for record in records:
        if clipped:
            values.append(record.south)
            values.append(record.north)
        else:
            tile_bounds = _bounds(record.tile)
            values.append(tile_bounds.south)
            values.append(tile_bounds.north)
    values = _unique_sorted(values)
    return list(zip(values, values[1:]))


def _merge_intervals(intervals):
    if not intervals:
        return []

    intervals = sorted(intervals)
    merged = [list(intervals[0])]
    for west, east in intervals[1:]:
        if west <= merged[-1][1] + _EPS:
            if east > merged[-1][1]:
                merged[-1][1] = east
        else:
            merged.append([west, east])
    return [(west, east) for west, east in merged]


def _intervals_width(intervals):
    return sum(east - west for west, east in intervals)


def _intersection_width(first, second):
    width = 0.0
    i = 0
    j = 0
    while i < len(first) and j < len(second):
        west = max(first[i][0], second[j][0])
        east = min(first[i][1], second[j][1])
        if east > west:
            width += east - west
        if first[i][1] < second[j][1]:
            i += 1
        else:
            j += 1
    return width


def _attribute(records, total_intervals, factor, label_measure, clipped):
    spans = [(record, _record_span(record, clipped)) for record in records]

    for total_west, total_east in total_intervals:
        candidates = [
            (record, span)
            for record, span in spans
            if span[0] < total_east and span[1] > total_west
        ]
        if not candidates:
            continue

        cuts = [total_west, total_east]
        for record, (span_west, span_east) in candidates:
            if total_west < span_west < total_east:
                cuts.append(span_west)
            if total_west < span_east < total_east:
                cuts.append(span_east)
        cuts = _unique_sorted(cuts)

        for west, east in zip(cuts, cuts[1:]):
            if east <= west:
                continue
            midpoint = (west + east) / 2.0
            owners = [
                (record, record.order)
                for record, (span_west, span_east) in candidates
                if span_west <= midpoint < span_east
            ]
            if not owners:
                continue
            owner, order = min(
                owners, key=lambda item: (-item[0].tile.z, item[1])
            )
            label_measure[owner.label] = (
                label_measure.get(owner.label, 0.0)
                + (east - west) * factor
            )
