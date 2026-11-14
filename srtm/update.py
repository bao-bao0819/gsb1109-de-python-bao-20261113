# -*- coding: utf-8 -*-

"""
Tools to decide which collected profiles must be recomputed when a
corrected elevation tile is delivered under the same file name.

The typical workflow is:

 * The supplier delivers a new version of a tile (same file name).
 * `diff_tile_data` compares the *sampled content* of the old and the new
   tile grid point by grid point. Files that only changed their packaging
   (zipped vs. unzipped) or not at all produce an empty update, so the
   whole batch of profiles is *not* requeued.
 * `plan_recompute` checks every profile point against the grid points
   that actually changed. A profile is only added to the recompute plan
   if it reads a changed grid point directly or uses one for
   interpolation (nearest-neighbour approximation or IDW). Grid points
   that were void (missing) in the old version and are filled in the new
   one are tracked, too: profiles that previously fell back to another
   source because of the hole get a `void_filled` entry.
 * Every `RecomputeEntry` keeps the latitude/longitude of the changed
   grid point that caused the inclusion, so each recompute ticket can be
   verified against the tile.
"""

import logging as mod_logging

from . import data as mod_data
from . import utils as mod_utils

from typing import *

REASON_DIRECT = 'direct'
REASON_INTERPOLATED = 'interpolated'
REASON_VOID_FILLED = 'void_filled'

class ChangedCell(NamedTuple):
    """ A single grid point whose sampled value changed between versions. """
    row: int
    column: int
    latitude: float
    longitude: float
    old_elevation: Optional[float]
    new_elevation: Optional[float]

class RecomputeEntry(NamedTuple):
    """
    One recompute ticket. `latitude`/`longitude` are the coordinates of
    the changed grid point that affects the profile (or of the profile
    point itself for a brand new tile), so the ticket can be verified.
    """
    profile_id: Any
    file_name: str
    latitude: float
    longitude: float
    reason: str

def maybe_unzip(data: bytes) -> bytes:
    """
    Return the raw .hgt content of `data`, unzipping it first if needed.
    Used so that a tile which only changed its packaging (zip vs. raw)
    compares equal to the previous version.
    """
    if data[:4] == b'PK\x03\x04':
        return mod_utils.unzip(data)
    return data

class TileUpdate:
    """
    The result of comparing two versions of the same tile file.
    """

    def __init__(self, file_name: str, changed_cells: List[ChangedCell], is_new_file: bool=False) -> None:
        self.file_name = file_name
        self.changed_cells = changed_cells
        self.is_new_file = is_new_file
        self._cells = dict(((cell.row, cell.column), cell) for cell in changed_cells)

    @property
    def has_changes(self) -> bool:
        return self.is_new_file or bool(self.changed_cells)

    def contains(self, row: int, column: int) -> bool:
        return (row, column) in self._cells

    def get(self, row: int, column: int) -> Optional[ChangedCell]:
        return self._cells.get((row, column))

    def __str__(self) -> str:
        return f'[{self.__class__}:{self.file_name}:{len(self.changed_cells)} changed cells]'

def diff_tile_data(file_name: str, old_data: Optional[bytes], new_data: bytes) -> TileUpdate:
    """
    Compare the sampled content of two versions of the tile `file_name`.

    Both versions are normalised with `maybe_unzip` first, so a delivery
    that only changed the packaging (or nothing at all) results in an
    update without changes and no profile needs to be recomputed.

    If `old_data` is None the tile did not exist before and every profile
    inside its bounds is potentially affected.
    """
    new_data = maybe_unzip(new_data)

    if old_data is None:
        return TileUpdate(file_name, [], is_new_file=True)

    old_data = maybe_unzip(old_data)

    if old_data == new_data:
        return TileUpdate(file_name, [])

    geo_elevation_data = mod_data.GeoElevationData({}, {}, file_handler=None) # type: ignore
    old_file = mod_data.GeoElevationFile(file_name, old_data, geo_elevation_data)
    new_file = mod_data.GeoElevationFile(file_name, new_data, geo_elevation_data)

    if old_file.square_side != new_file.square_side:
        raise Exception('Cannot diff {0}: grid size changed from {1} to {2}'.format(
            file_name, old_file.square_side, new_file.square_side))

    changed_cells = []
    for row in range(new_file.square_side):
        for column in range(new_file.square_side):
            old_elevation = old_file.get_elevation_from_row_and_column(row, column)
            new_elevation = new_file.get_elevation_from_row_and_column(row, column)
            if old_elevation != new_elevation:
                latitude, longitude = new_file.get_lat_and_long(row, column)
                changed_cells.append(ChangedCell(row, column, latitude, longitude,
                                                 old_elevation, new_elevation))

    mod_logging.info('%s: %s grid points changed', file_name, len(changed_cells))

    return TileUpdate(file_name, changed_cells)

def get_source_cells(geo_elevation_data: mod_data.GeoElevationData, latitude: float, longitude: float,
                     approximate: bool=False, idw_radius: Optional[int]=None) -> List[Tuple[str, int, int]]:
    """
    Return the list of (file_name, row, column) grid points a query at
    (latitude, longitude) reads its elevation from.

    With `approximate` the four neighbour positions used by
    GeoElevationFile.approximation are included -- note that they may lie
    in a neighbouring tile. With `idw_radius` the cells of the inverse
    distance weighted matrix of that radius are included.
    """
    result: List[Tuple[str, int, int]] = []

    def add(file_name: str, row: int, column: int) -> None:
        cell = (file_name, row, column)
        if cell not in result:
            result.append(cell)

    file_name = geo_elevation_data.get_file_name(latitude, longitude)
    if not file_name:
        return result

    geo_file = geo_elevation_data.get_file(latitude, longitude)
    if geo_file is None:
        return result

    row, column = geo_file.get_row_and_column(latitude, longitude)
    row, column = int(row), int(column)
    add(file_name, row, column)

    if approximate:
        # Same neighbour positions as GeoElevationFile.approximation:
        d = 1. / geo_file.square_side
        for neighbour_latitude, neighbour_longitude in (
                (latitude + d, longitude), (latitude - d, longitude),
                (latitude, longitude + d), (latitude, longitude - d)):
            neighbour_file_name = geo_elevation_data.get_file_name(neighbour_latitude, neighbour_longitude)
            if not neighbour_file_name:
                continue
            neighbour_file = geo_elevation_data.get_file(neighbour_latitude, neighbour_longitude)
            if neighbour_file is None:
                continue
            neighbour_row, neighbour_column = neighbour_file.get_row_and_column(neighbour_latitude, neighbour_longitude)
            add(neighbour_file_name, int(neighbour_row), int(neighbour_column))

    if idw_radius is not None:
        if idw_radius not in mod_data.IDW_OFFSETS:
            raise ValueError('Radius {} invalid, expected 1 or 2'.format(idw_radius))
        for offset in mod_data.IDW_OFFSETS[idw_radius]:
            if offset is not None and \
                    0 <= row + offset[0] < geo_file.square_side and \
                    0 <= column + offset[1] < geo_file.square_side:
                add(file_name, row + offset[0], column + offset[1])

    return result

def plan_recompute(geo_elevation_data: mod_data.GeoElevationData,
                   tile_updates: List[TileUpdate],
                   profiles: Iterable[Tuple[Any, Iterable[Tuple[float, float]]]],
                   approximate: bool=False, idw_radius: Optional[int]=None) -> List[RecomputeEntry]:
    """
    Decide which profiles must be recomputed given the changed tiles.

    `profiles` is an iterable of (profile_id, points) where points are
    (latitude, longitude) tuples. A profile yields at most one entry per
    updated tile, carrying the location of the first changed grid point
    found to affect it. Tiles without content changes (e.g. only
    repackaged) are ignored, so they never requeue a profile.
    """
    updates_by_file = dict((update.file_name, update) for update in tile_updates if update.has_changes)

    entries: List[RecomputeEntry] = []
    if not updates_by_file:
        return entries

    planned = set() # type: Set[Tuple[Any, str]]
    for profile_id, points in profiles:
        for latitude, longitude in points:
            source_cells = get_source_cells(geo_elevation_data, latitude, longitude,
                                            approximate=approximate, idw_radius=idw_radius)
            direct_cell = source_cells[0] if source_cells else None
            for file_name, row, column in source_cells:
                update = updates_by_file.get(file_name)
                if update is None or (profile_id, file_name) in planned:
                    continue
                changed_cell = update.get(row, column)
                if not update.is_new_file and changed_cell is None:
                    continue
                if changed_cell is not None and changed_cell.old_elevation is None \
                        and changed_cell.new_elevation is not None:
                    # A hole was filled: the previous value came from a
                    # fallback (e.g. a neighbouring tile), so the source
                    # of this profile changed.
                    reason = REASON_VOID_FILLED
                    impact_latitude, impact_longitude = changed_cell.latitude, changed_cell.longitude
                elif update.is_new_file:
                    reason = REASON_VOID_FILLED
                    impact_latitude, impact_longitude = latitude, longitude
                elif (file_name, row, column) == direct_cell:
                    reason = REASON_DIRECT
                    impact_latitude, impact_longitude = changed_cell.latitude, changed_cell.longitude # type: ignore
                else:
                    reason = REASON_INTERPOLATED
                    impact_latitude, impact_longitude = changed_cell.latitude, changed_cell.longitude # type: ignore
                entries.append(RecomputeEntry(profile_id, file_name,
                                              impact_latitude, impact_longitude, reason))
                planned.add((profile_id, file_name))

    return entries
