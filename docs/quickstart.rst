Quick start
===========

In the XYZ tiling system, the region of the world from 85.0511 (more precisely:
``arctan(sinh(π))``) degrees south of the Equator to 85.0511 degrees north is
covered at zoom level 0 by a single tile. The number of tiles at each zoom
level is ``4 ** Z``. At zoom level 1, we have 4 tiles.

.. code-block:: none


                    +-------------+-------------+  85.0511 deg N
                    |             |             |
                    |    x: 0     |    x: 1     |
                    |    y: 0     |    y: 0     |
                    |    z: 1     |    z: 1     |
                    |             |             |
                    +-------------+-------------+   0.0 deg N
                    |             |             |
                    |    x: 0     |    x: 1     |
                    |    y: 1     |    y: 1     |
                    |    z: 1     |    z: 1     |
                    |             |             |
                    +-------------+-------------+  85.0511 deg S

                180.0 deg W               180.0 deg E


You can get the tile containing a longitude and latitude pair from the
``mercantile.tile`` function.

.. code-block:: pycon

    >>> import mercantile
    >>> mercantile.tile(-105.0, 40.0, 1)
    Tile(x=0, y=0, z=1)

You can get the geographic (longitude and latitude) bounds of a tile from the
``mercantile.bounds`` function.

.. code-block:: pycon

    >>> mercantile.bounds(mercantile.Tile(x=0, y=0, z=1))
    LngLatBbox(west=-180.0, south=0.0, east=0.0, north=85.0511287798066)


Spherical area and coverage
===========================

Web Mercator tiles have equal planar area, not equal spherical area. A
tile count is therefore a poor proxy for area coverage: at zoom 2 the two
northernmost tiles cover 50 percent of the tiles but less than 8 percent
of the region's area. Use ``mercantile.spherical_area`` to measure a tile
or a tile clipped at a latitude, and ``mercantile.coverage_report`` to
reconcile the two bases.

.. code-block:: pycon

    >>> import mercantile
    >>> round(mercantile.spherical_area((0, 0, 0)) / 1e6)
    509302149
    >>> round(mercantile.spherical_area((0, 0, 0), north=0.0) / 1e6)
    254651075

A coverage record may be a tile, a ``CoverageTile``, or a mapping with a
``tile``, an optional ``south`` and ``north`` (a block surveyed only up to
a latitude), and an optional ``label`` naming the region.

.. code-block:: pycon

    >>> total = [
    ...     {"tile": [0, 0, 2], "label": "north"},
    ...     {"tile": [1, 0, 2], "label": "north"},
    ...     {"tile": [0, 1, 2], "label": "south"},
    ...     {"tile": [1, 1, 2], "label": "south"},
    ... ]
    >>> surveyed = [
    ...     {"tile": [0, 0, 2], "label": "north"},
    ...     {"tile": [1, 0, 2], "label": "north"},
    ... ]
    >>> report = mercantile.coverage_report(total, surveyed)
    >>> round(report.count_coverage, 3)
    0.5
    >>> round(report.area_coverage, 3)
    0.079
    >>> round(report.difference_pp, 1)
    -42.1

The report's ``contributions`` are sorted by absolute difference in
percentage points. Each contribution shows its share of the area and of
the tile count, so the largest gap can be traced back to a named region.

.. code-block:: pycon

    >>> [(c.label, round(c.difference_pp, 1)) for c in report.contributions]
    [('north', -42.1)]

Records may mix zoom levels and may overlap; the union is measured once.
By default a latitude-clipped block counts as a whole tile on the count
basis, matching a daily report that counts a partially surveyed block as
surveyed. Pass ``count_partial="clip"`` to count only the clipped part
instead. The count basis is normalized at ``count_zoom`` (the maximum zoom
in ``total`` by default); one full tile at that zoom has weight 1.

Replacing a whole tile with its four children does not change the total
area or the surveyed area. With ``count_partial="tile"`` a partially
surveyed block is counted as whole, so its four children will count
differently unless they are also clipped. Use ``count_partial="clip"`` if
you need the count basis to be invariant under that replacement.
