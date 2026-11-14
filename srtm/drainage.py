# -*- coding: utf-8 -*-

# Copyright 2013 Tomo Krajina
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Multi-outlet drainage analysis on a local elevation grid.

Instead of assigning every cell to a single downstream neighbour (which makes
the watershed depend on neighbour traversal order on flat areas), every cell
is labelled with *all* numbered outlets it may possibly drain to.

Flow convention:

* water first spreads across the connected contour-level surface, i.e. moves
  between adjacent cells of equal elevation in any direction;
* only then is a strict descent to a lower neighbour allowed;
* a move is therefore valid whenever the neighbour elevation is less than or
  equal to the current elevation.

Four- or eight-neighbour connectivity is selected by the caller. Cells with
missing elevation (``None`` or the SRTM void marker) are unknown outside
terrain: they are never traversed, but cells that may spill into them are
flagged separately from enclosed depressions.

The algorithm is order independent: outlets are flooded in reverse over the
union of flat and uphill edges, and labels are accumulated as sets. A cell
reachable from more than one outlet is disputed, and concrete non-increasing
paths to each reachable outlet can be retrieved on demand.
"""

import enum
from collections import deque
from typing import TYPE_CHECKING, Dict, FrozenSet, List, Mapping, Optional, Sequence, Set, Tuple

if TYPE_CHECKING:  # pragma: no cover
    from .data import GeoElevationFile

# SRTM .hgt files encode missing measurements with this signed short value.
VOID_MARKER: int = -32768


class Connectivity(enum.IntEnum):
    """Neighbourhood used when tracing flow between grid cells."""

    FOUR = 4
    EIGHT = 8


class CellKind(enum.Enum):
    """Coarse classification of a grid cell."""

    VOID = "void"
    DRAINED = "drained"
    CLOSED_DEPRESSION = "closed_depression"
    VOID_LEAK = "void_leak"


_OFFSETS: Dict[Connectivity, Tuple[Tuple[int, int], ...]] = {
    Connectivity.FOUR: (
        (-1, 0),
        (1, 0),
        (0, -1),
        (0, 1),
    ),
    Connectivity.EIGHT: (
        (-1, -1),
        (-1, 0),
        (-1, 1),
        (0, -1),
        (0, 1),
        (1, -1),
        (1, 0),
        (1, 1),
    ),
}


class DrainageResult:
    """
    Result of a multi-outlet drainage analysis.

    Cell-level information is queried through the accessor methods; summary
    lists of (row, column) tuples are also available for each special class.
    """

    def __init__(
        self,
        rows: int,
        columns: int,
        connectivity: Connectivity,
        outlets: Mapping[int, int],
        elevations: Sequence[Optional[float]],
        is_void: Sequence[bool],
        labels: Sequence[FrozenSet[int]],
        void_reachable: Sequence[bool],
    ) -> None:
        self.rows = rows
        self.columns = columns
        self.connectivity = connectivity
        self._offsets = _OFFSETS[connectivity]
        # Outlet id -> flattened cell index, and the reverse lookup.
        self._outlet_cells: Dict[int, int] = dict(outlets)
        self._outlet_indices: FrozenSet[int] = frozenset(outlets.values())
        self.outlets: Dict[int, Tuple[int, int]] = {
            outlet_id: self._position(cell_index) for outlet_id, cell_index in outlets.items()
        }
        self._elevations: List[Optional[float]] = list(elevations)
        self._is_void: List[bool] = list(is_void)
        self._labels: List[FrozenSet[int]] = list(labels)
        self._void_reachable: List[bool] = list(void_reachable)

    def _index(self, row: int, column: int) -> int:
        if not (0 <= row < self.rows and 0 <= column < self.columns):
            raise ValueError(f"Cell ({row}, {column}) is outside the {self.rows}x{self.columns} grid")
        return row * self.columns + column

    def _position(self, index: int) -> Tuple[int, int]:
        return divmod(index, self.columns)

    def _neighbours(self, index: int) -> List[int]:
        row, column = divmod(index, self.columns)
        result: List[int] = []
        for row_offset, column_offset in self._offsets:
            next_row = row + row_offset
            next_column = column + column_offset
            if 0 <= next_row < self.rows and 0 <= next_column < self.columns:
                result.append(next_row * self.columns + next_column)
        return result

    def _elevation(self, index: int) -> float:
        value = self._elevations[index]
        if value is None:
            raise AssertionError(f"Cell {self._position(index)} has no elevation")
        return value

    def outlets_for(self, row: int, column: int) -> FrozenSet[int]:
        """Ids of every numbered outlet the cell may drain to."""
        return self._labels[self._index(row, column)]

    def is_void(self, row: int, column: int) -> bool:
        """Whether the cell itself is a missing-measurement void."""
        return self._is_void[self._index(row, column)]

    def can_leak_to_void(self, row: int, column: int) -> bool:
        """
        Whether the cell may spill into a missing-measurement area.

        The elevation of a void cell is unknown, so it cannot be ruled out as
        a downstream sink. Every cell connected to a void through valid
        flat-or-downhill moves is considered a possible leak.
        """
        return self._void_reachable[self._index(row, column)]

    def is_closed_depression(self, row: int, column: int) -> bool:
        """
        Whether the cell is trapped in an enclosed depression.

        A closed depression reaches no numbered outlet and no void: all of its
        valid flow moves stay inside known terrain and end in a local low.
        """
        index = self._index(row, column)
        return not self._is_void[index] and not self._labels[index] and not self._void_reachable[index]

    def is_disputed(self, row: int, column: int) -> bool:
        """Whether more than one outlet is reachable from the cell."""
        return len(self._labels[self._index(row, column)]) > 1

    def kind(self, row: int, column: int) -> CellKind:
        """Coarse, mutually exclusive classification of the cell."""
        index = self._index(row, column)
        if self._is_void[index]:
            return CellKind.VOID
        if self._labels[index]:
            return CellKind.DRAINED
        if self._void_reachable[index]:
            return CellKind.VOID_LEAK
        return CellKind.CLOSED_DEPRESSION

    def disputed_cells(self) -> List[Tuple[int, int]]:
        """All cells from which more than one outlet is reachable."""
        return [self._position(i) for i, label in enumerate(self._labels) if len(label) > 1]

    def closed_depression_cells(self) -> List[Tuple[int, int]]:
        """All known-terrain cells trapped without an outlet or a void."""
        return [
            self._position(i)
            for i in range(len(self._labels))
            if not self._is_void[i] and not self._labels[i] and not self._void_reachable[i]
        ]

    def void_leak_cells(self) -> List[Tuple[int, int]]:
        """
        All known-terrain cells that may spill into a missing area.

        This list is not mutually exclusive with drained cells: a cell with a
        valid numbered-outlet route that also touches a void may leak out.
        :meth:`kind` stays mutually exclusive, while this summary follows the
        orthogonal :meth:`can_leak_to_void` flag.
        """
        return [
            self._position(i)
            for i in range(len(self._labels))
            if not self._is_void[i] and self._void_reachable[i]
        ]

    def path(self, row: int, column: int, outlet_id: int) -> Optional[List[Tuple[int, int]]]:
        """
        Find an actual flow path from the cell to the given outlet.

        The returned sequence starts at the given cell, ends on the outlet and
        only uses connectivity-valid moves with non-increasing elevation.
        Outlets are absorbing: a path never passes through another outlet.
        Returns ``None`` if the outlet is not reachable.
        """
        start = self._index(row, column)
        if outlet_id not in self._outlet_cells:
            raise KeyError(f"Unknown outlet id {outlet_id}")
        if self._is_void[start]:
            raise ValueError(f"Cell {self._position(start)} is a void and has no flow path")
        target = self._outlet_cells[outlet_id]
        if outlet_id not in self._labels[start]:
            return None
        if start == target:
            return [self._position(start)]

        seen = [False] * len(self._elevations)
        predecessor = [-1] * len(self._elevations)
        seen[start] = True
        queue = deque([start])

        while queue:
            current = queue.popleft()
            current_elevation = self._elevation(current)
            for next_cell in self._neighbours(current):
                if seen[next_cell] or self._is_void[next_cell]:
                    continue
                if self._elevation(next_cell) > current_elevation:
                    # Only contour-level spreading and strict descent are valid.
                    continue
                if next_cell in self._outlet_indices and next_cell != target:
                    # Water leaves the system at the first outlet it reaches.
                    continue
                seen[next_cell] = True
                predecessor[next_cell] = current
                if next_cell == target:
                    queue.clear()
                    break
                queue.append(next_cell)

        if predecessor[target] < 0:
            return None

        reversed_path: List[Tuple[int, int]] = []
        cursor = target
        while cursor >= 0:
            reversed_path.append(self._position(cursor))
            if cursor == start:
                break
            cursor = predecessor[cursor]
        reversed_path.reverse()
        return reversed_path

    def paths(self, row: int, column: int) -> Dict[int, List[Tuple[int, int]]]:
        """
        Concrete flow paths from the cell to every outlet it can reach.

        This is the practical view for a disputed cell: one non-increasing
        path per reachable outlet id.
        """
        result: Dict[int, List[Tuple[int, int]]] = {}
        for outlet_id in self._labels[self._index(row, column)]:
            outlet_path = self.path(row, column, outlet_id)
            if outlet_path is not None:
                result[outlet_id] = outlet_path
        return result


def analyze_drainage(
    elevations: Sequence[Sequence[Optional[float]]],
    outlets: Mapping[int, Tuple[int, int]],
    connectivity: Connectivity = Connectivity.EIGHT,
) -> DrainageResult:
    """
    Compute every outlet reachable from each cell of a local elevation grid.

    ``elevations`` is a row-major rectangular grid; ``None`` entries (or values
    at or below :data:`VOID_MARKER`) are treated as unknown void cells.
    ``outlets`` maps caller-chosen integer ids to ``(row, column)`` positions
    of numbered exits, which must lie on known terrain and be distinct.
    ``connectivity`` selects four- or eight-neighbour flow.

    The result does not depend on outlet insertion order or on the order in
    which neighbours are visited.
    """
    if not isinstance(connectivity, Connectivity):
        raise TypeError(f"connectivity must be a Connectivity value, got {connectivity!r}")

    rows = len(elevations)
    if rows == 0:
        raise ValueError("Elevation grid must contain at least one row")
    columns = len(elevations[0])
    if columns == 0:
        raise ValueError("Elevation grid must contain at least one column")
    for row_index, row in enumerate(elevations):
        if len(row) != columns:
            raise ValueError(
                f"Elevation grid is not rectangular: row {row_index} has {len(row)} cells, expected {columns}"
            )

    flat_elevations: List[Optional[float]] = []
    is_void: List[bool] = []
    for row in elevations:
        for value in row:
            if value is None or value <= VOID_MARKER:
                is_void.append(True)
                flat_elevations.append(None)
            else:
                is_void.append(False)
                flat_elevations.append(float(value))

    cell_count = rows * columns
    offsets = _OFFSETS[connectivity]

    outlet_cells: Dict[int, int] = {}
    occupied: Set[int] = set()
    for outlet_id, (outlet_row, outlet_column) in outlets.items():
        if not (0 <= outlet_row < rows and 0 <= outlet_column < columns):
            raise ValueError(f"Outlet {outlet_id} at ({outlet_row}, {outlet_column}) is outside the grid")
        outlet_index = outlet_row * columns + outlet_column
        if is_void[outlet_index]:
            raise ValueError(f"Outlet {outlet_id} at ({outlet_row}, {outlet_column}) is a void cell")
        if outlet_index in occupied:
            raise ValueError(
                f"Outlets {outlet_id} and another outlet share cell ({outlet_row}, {outlet_column})"
            )
        occupied.add(outlet_index)
        outlet_cells[outlet_id] = outlet_index

    def neighbours(index: int) -> List[int]:
        row, column = divmod(index, columns)
        result: List[int] = []
        for row_offset, column_offset in offsets:
            next_row = row + row_offset
            next_column = column + column_offset
            if 0 <= next_row < rows and 0 <= next_column < columns:
                result.append(next_row * columns + next_column)
        return result

    # Reverse flow graph: reverse_edges[v] contains u whenever water may move
    # u -> v, i.e. v is known and not higher than u. Equal elevations produce
    # edges in both directions, modelling the connected contour surface.
    reverse_edges: List[List[int]] = [[] for _ in range(cell_count)]
    for current in range(cell_count):
        if is_void[current]:
            continue
        current_elevation = flat_elevations[current]
        assert current_elevation is not None
        for next_cell in neighbours(current):
            if is_void[next_cell]:
                continue
            next_elevation = flat_elevations[next_cell]
            assert next_elevation is not None
            if next_elevation <= current_elevation:
                reverse_edges[next_cell].append(current)

    # Reverse flood from each numbered outlet. Crossing another outlet is
    # forbidden because water exits the system at the first outlet reached.
    raw_labels: List[Set[int]] = [set() for _ in range(cell_count)]
    for outlet_id, start in outlet_cells.items():
        raw_labels[start].add(outlet_id)
        queue = deque([start])
        while queue:
            current = queue.popleft()
            for next_cell in reverse_edges[current]:
                if next_cell in occupied and next_cell != start:
                    continue
                if outlet_id in raw_labels[next_cell]:
                    continue
                raw_labels[next_cell].add(outlet_id)
                queue.append(next_cell)

    labels: List[FrozenSet[int]] = [frozenset(label) for label in raw_labels]

    # Reverse flood from every void. A void has no known elevation, so any
    # adjacent known cell may spill into it; the flood then continues over the
    # usual reverse edges to mark every cell that could possibly leak out.
    void_reachable = [False] * cell_count
    queue = deque()
    for index in range(cell_count):
        if is_void[index]:
            void_reachable[index] = True
            queue.append(index)
    while queue:
        current = queue.popleft()
        candidates = neighbours(current) if is_void[current] else reverse_edges[current]
        for next_cell in candidates:
            if is_void[next_cell] or void_reachable[next_cell]:
                continue
            void_reachable[next_cell] = True
            queue.append(next_cell)

    return DrainageResult(
        rows=rows,
        columns=columns,
        connectivity=connectivity,
        outlets=outlet_cells,
        elevations=flat_elevations,
        is_void=is_void,
        labels=labels,
        void_reachable=void_reachable,
    )


def grid_from_tile(tile: "GeoElevationFile") -> List[List[Optional[float]]]:
    """
    Extract a plain elevation grid from a loaded :class:`GeoElevationFile`.

    Void measurements (the tile reader returns ``None`` for them) are kept as
    ``None`` so the grid can be fed straight into :func:`analyze_drainage`.
    """
    grid: List[List[Optional[float]]] = []
    for row in range(tile.square_side):
        grid_row: List[Optional[float]] = []
        for column in range(tile.square_side):
            grid_row.append(tile.get_elevation_from_row_and_column(row, column))
        grid.append(grid_row)
    return grid
