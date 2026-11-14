# -*- coding: utf-8 -*-

"""
Depression (closed-basin) merge analysis for local SRTM ``.hgt`` sample grids.

The planning team does not only ask "how much area is flooded at a given
level"; it also wants to know *at which elevation two depressions first become
connected*.  This module turns a single local HGT sample grid into a
persistent, repeatedly queryable *merge table*.

Boundary conventions (stated explicitly so every query uses the same rules):

* A cell is usable only when it holds a measured elevation.  SRTM void cells are
  encoded outside ``[-1000, 10000]`` and are treated as **no data**.  No-data
  cells are permanent barriers: they are never flooded and can never connect two
  depressions, so connectivity is never decided "ahead of time" through a void.
* Neighbourhood is the 4-connected grid (up/down/left/right); diagonal cells do
  not touch.
* Water is filled from the lowest cells upward.  A cell at elevation ``h`` is
  flooded once the water level reaches ``h``.  Two flooded regions become
  connected at the elevation of the highest cell on the lowest path between
  them, i.e. the elevation of the saddle plateau that first joins them.
* When a single saddle plateau joins *three or more* already distinct
  depressions at the same level, that is exactly **one** merge event (the
  resulting basin has all of them as members), not several pairwise merges.
* The grid border is an open spill boundary.  A depression that reaches the
  border spills out of the analysed area; its border spill level is recorded but
  it is not an inter-depression merge.

The result does not depend on the order in which grid cells are read.  Rotating
or reflecting the grid therefore leaves every merge elevation and member set
unchanged (only cell coordinates follow the grid).
"""

import array as mod_array
import csv as mod_csv
import heapq as mod_heapq
import json as mod_json
import math as mod_math
import os as mod_os
import struct as mod_struct
from typing import *

# Mirror the validity range used by GeoElevationFile.get_elevation_from_row_and_column
ELEVATION_MIN = -1000
ELEVATION_MAX = 10000
VOID = -32768

# Cell status flags stored in a byte array.
STATUS_UNVISITED = 0
STATUS_FLOODED = 1


class MergeEvent:
    """A single, distinct moment at which depressions join."""

    def __init__(self,
                 level: int,
                 members: Tuple[int, ...],
                 saddle: Tuple[int, int],
                 flooded_cells: int) -> None:
        self.level = level
        # Sorted tuple of root depression ids; order independent of read order.
        self.members = tuple(sorted(members))
        # Grid (row, column) of the saddle plateau cell that triggered the merge.
        self.saddle = saddle
        # Number of flooded cells in the resulting basin immediately after merge.
        self.flooded_cells = flooded_cells

    def as_dict(self) -> Dict[str, Any]:
        return {
            'level': self.level,
            'members': list(self.members),
            'saddle_row': self.saddle[0],
            'saddle_column': self.saddle[1],
            'flooded_cells': self.flooded_cells,
        }

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, MergeEvent):
            return NotImplemented
        return (self.level == other.level
                and self.members == other.members
                and self.saddle == other.saddle
                and self.flooded_cells == other.flooded_cells)

    def __repr__(self) -> str:
        return ('MergeEvent(level=%d, members=%s, saddle=%s, flooded_cells=%d)'
                % (self.level, self.members, self.saddle, self.flooded_cells))


class BasinMergeTable:
    """
    Queryable table of depression merge events built from one HGT grid.

    Build once with :func:`build_basin_merge_table`, persist with
    :meth:`save` / :func:`load_basin_merge_table`, then answer many queries
    without recomputing the grid.
    """

    def __init__(self,
                 file_name: str,
                 side: int,
                 cell_area: float,
                 merge_events: List[MergeEvent],
                 basin_birth_level: Dict[int, int],
                 basin_seed: Dict[int, Tuple[int, int]],
                 basin_birth_cells: Optional[Dict[int, Tuple[int, ...]]] = None,
                 border_spills: Optional[List[Dict[str, Any]]] = None) -> None:
        self.file_name = file_name
        self.side = side
        self.cell_area = cell_area
        # Merge events in non-decreasing flood order.
        self.merge_events = merge_events
        # First flooded elevation of each root depression (the basin floor).
        self.basin_birth_level = dict(basin_birth_level)
        # Lowest cell (row, column) of each root depression.
        self.basin_seed = dict(basin_seed)
        # Every flat cell index in the depression's minimum plateau.  This set is
        # geometric (rotation/reflection covariant) and therefore gives each
        # depression a stable identity independent of cell reading order.
        self.basin_birth_cells = {
            key: tuple(value) for key, value in (basin_birth_cells or {}).items()
        }
        # Levels at which inland water first reaches the open grid border.
        self.border_spills = list(border_spills or [])
        # Lazily built lookup: frozenset(members) -> event.
        self._member_index: Optional[Dict[Tuple[int, ...], MergeEvent]] = None

    # ------------------------------------------------------------------ #
    # Queries
    # ------------------------------------------------------------------ #
    def _index(self) -> Dict[Tuple[int, ...], MergeEvent]:
        if self._member_index is None:
            self._member_index = {event.members: event
                                  for event in self.merge_events}
        return self._member_index

    def connection_level(self, basin_a: int, basin_b: int) -> Optional[int]:
        """
        Elevation at which two root depressions first belong to the same basin.

        Returns ``None`` when the two never connect inside the analysed grid
        (for example a no-data barrier or an open-border spill separates them).
        """
        if basin_a == basin_b:
            return self.basin_birth_level.get(basin_a)
        for event in self.merge_events:
            if basin_a in event.members and basin_b in event.members:
                return event.level
        return None

    def connection_event(self, basin_ids: Iterable[int]) -> Optional[MergeEvent]:
        """
        The single merge event whose resulting basin contains every requested
        root depression.  Returns ``None`` if they never all join.
        """
        wanted = tuple(sorted(set(basin_ids)))
        return self._index().get(wanted)

    def basin_for_query(self, basin_ids: Iterable[int]) -> Optional[Dict[str, Any]]:
        """
        Full connectivity answer for a set of depressions: the level, saddle
        position, member set and flooded area at the moment they are all joined.
        """
        event = self.connection_event(basin_ids)
        if event is None:
            return None
        return {
            'level': event.level,
            'members': list(event.members),
            'saddle': {'row': event.saddle[0], 'column': event.saddle[1]},
            'flooded_cells': event.flooded_cells,
            'flooded_area': event.flooded_cells * self.cell_area,
        }

    def events_at_or_below(self, level: float) -> List[MergeEvent]:
        """Every merge event whose connection level is <= ``level``."""
        return [event for event in self.merge_events if event.level <= level]

    def all_events(self) -> List[MergeEvent]:
        return list(self.merge_events)

    def basin_ids(self) -> List[int]:
        return sorted(self.basin_birth_level)

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #
    def to_dict(self) -> Dict[str, Any]:
        return {
            'file_name': self.file_name,
            'side': self.side,
            'cell_area': self.cell_area,
            'basin_birth_level': {str(k): v for k, v in self.basin_birth_level.items()},
            'basin_seed': {str(k): [v[0], v[1]] for k, v in self.basin_seed.items()},
            'basin_birth_cells': {
                str(k): [[cell // self.side, cell % self.side] for cell in value]
                for k, value in self.basin_birth_cells.items()
            },
            'border_spills': self.border_spills,
            'merge_events': [event.as_dict() for event in self.merge_events],
        }

    def save(self, path: str) -> None:
        with open(path, 'w', encoding='utf-8') as stream:
            mod_json.dump(self.to_dict(), stream, indent=2, sort_keys=True)

    def save_csv(self, path: str) -> None:
        """Flat, one-row-per-merge table convenient for spreadsheet queries."""
        with open(path, 'w', newline='', encoding='utf-8') as stream:
            writer = mod_csv.writer(stream)
            writer.writerow(['level', 'members', 'saddle_row',
                             'saddle_column', 'flooded_cells', 'flooded_area'])
            for event in self.merge_events:
                writer.writerow([
                    event.level,
                    '|'.join(str(member) for member in event.members),
                    event.saddle[0],
                    event.saddle[1],
                    event.flooded_cells,
                    event.flooded_cells * self.cell_area,
                ])


def load_basin_merge_table(path: str) -> BasinMergeTable:
    with open(path, 'r', encoding='utf-8') as stream:
        payload = mod_json.load(stream)
    merge_events = [
        MergeEvent(
            level=item['level'],
            members=tuple(item['members']),
            saddle=(item['saddle_row'], item['saddle_column']),
            flooded_cells=item['flooded_cells'],
        )
        for item in payload['merge_events']
    ]
    basin_birth_level = {int(k): v for k, v in payload['basin_birth_level'].items()}
    basin_seed = {int(k): tuple(v) for k, v in payload['basin_seed'].items()}
    side = payload['side']
    basin_birth_cells = {
        int(key): tuple(row * side + column for row, column in value)
        for key, value in payload.get('basin_birth_cells', {}).items()
    }
    return BasinMergeTable(
        file_name=payload['file_name'],
        side=side,
        cell_area=payload['cell_area'],
        merge_events=merge_events,
        basin_birth_level=basin_birth_level,
        basin_seed=basin_seed,
        basin_birth_cells=basin_birth_cells,
        border_spills=payload.get('border_spills', []),
    )


# ---------------------------------------------------------------------- #
# Grid loading
# ---------------------------------------------------------------------- #
def read_hgt_grid(data: bytes) -> Tuple[List[Optional[int]], int]:
    """
    Decode raw HGT bytes into a flat grid of elevations.

    No-data SRTM cells (outside the valid range) become ``None`` so voids are
    indistinguishable from missing measurements and can never connect basins.
    """
    cell_count = len(data) // 2
    side = int(mod_math.sqrt(cell_count))
    if side * side != cell_count:
        raise ValueError('HGT data is not a square grid (%d cells)' % cell_count)
    raw = mod_struct.unpack('>%dh' % cell_count, data)
    grid: List[Optional[int]] = [None] * cell_count
    for index, value in enumerate(raw):
        if ELEVATION_MIN <= value <= ELEVATION_MAX:
            grid[index] = value
    return grid, side


def load_hgt_file(path: str) -> Tuple[List[Optional[int]], int]:
    with open(path, 'rb') as stream:
        return read_hgt_grid(stream.read())


# ---------------------------------------------------------------------- #
# Merge analysis
# ---------------------------------------------------------------------- #
class _DisjointSet:
    """Union-find whose roots are kept stable regardless of union order."""

    def __init__(self, count: int) -> None:
        self.parent = mod_array.array('i', range(count))
        self.size = mod_array.array('i', [1] * count)

    def find(self, node: int) -> int:
        parent = self.parent
        root = node
        while parent[root] != root:
            root = parent[root]
        while parent[node] != node:
            grandparent = parent[node]
            parent[node] = root
            node = grandparent
        return root

    def union(self, node_a: int, node_b: int) -> int:
        root_a = self.find(node_a)
        root_b = self.find(node_b)
        if root_a == root_b:
            return root_a
        # Always fold the larger-id root under the smaller-id root so the
        # surviving root label is independent of cell traversal order.
        if root_a < root_b:
            keep, merge = root_a, root_b
        else:
            keep, merge = root_b, root_a
        self.parent[merge] = keep
        self.size[keep] += self.size[merge]
        return keep


def build_basin_merge_table(grid: List[Optional[int]],
                            side: int,
                            file_name: str = '',
                            cell_area: float = 1.0) -> BasinMergeTable:
    """
    Flood the grid from the bottom up and record one :class:`MergeEvent` per
    distinct depression merge.

    The priority queue orders cells by elevation and breaks ties by flat cell
    index, a total order, so the result is fully deterministic and is unaffected
    by the order in which the caller read the grid.  All equal-elevation
    plateaus at a level are resolved before the level advances, which guarantees
    that a saddle joining several depressions at the same height produces a
    single merge event and that a basin which both merges and spills at that
    height is classified consistently.
    """
    total = side * side
    if len(grid) != total:
        raise ValueError('grid length %d does not match side %d' % (len(grid), side))

    cells = [(index, elevation)
             for index, elevation in enumerate(grid)
             if elevation is not None]
    return _analyze_cells(cells, side, file_name=file_name, cell_area=cell_area)


def build_basin_merge_table_from_cells(cells: Iterable[Tuple[int, int]],
                                       side: int,
                                       file_name: str = '',
                                       cell_area: float = 1.0) -> BasinMergeTable:
    """
    Build a merge table from ``(flat_index, elevation)`` pairs in *any* order.

    This explicit entry point makes the reading-order guarantee testable: feed
    the same cells shuffled and the merge levels and member sets are identical.
    Voids are simply omitted from ``cells``.
    """
    return _analyze_cells(list(cells), side, file_name=file_name, cell_area=cell_area)


def _analyze_cells(cells: List[Tuple[int, int]],
                   side: int,
                   file_name: str,
                   cell_area: float) -> BasinMergeTable:
    total = side * side

    heap: List[Tuple[int, int]] = []
    elevations: List[int] = [VOID] * total
    for index, elevation in cells:
        if not 0 <= index < total:
            raise ValueError('cell index %d outside a %dx%d grid' % (index, side, side))
        mod_heapq.heappush(heap, (elevation, index))
        elevations[index] = elevation

    status = mod_array.array('b', [STATUS_UNVISITED]) * total
    disjoint = _DisjointSet(total)

    # Per union-find-root component state.  Depression labels are merged with
    # ordinary set union, which is commutative and associative, so component
    # membership cannot depend on union (cell reading) order.
    comp_labels: Dict[int, Set[int]] = {}
    comp_spill: Dict[int, bool] = {}
    next_depression_id = 0
    basin_birth_level: Dict[int, int] = {}
    basin_seed: Dict[int, Tuple[int, int]] = {}
    basin_birth_cells: Dict[int, Tuple[int, ...]] = {}
    merge_events: List[MergeEvent] = []
    border_spills: List[Dict[str, Any]] = []
    emitted_label_sets: Set[FrozenSet[int]] = set()
    spilled_label_sets: Set[FrozenSet[int]] = set()

    def neighbours(index: int) -> Iterator[int]:
        row, column = divmod(index, side)
        if row > 0:
            yield index - side
        if row + 1 < side:
            yield index + side
        if column > 0:
            yield index - 1
        if column + 1 < side:
            yield index + 1

    def is_border(index: int) -> bool:
        row, column = divmod(index, side)
        return row == 0 or column == 0 or row == side - 1 or column == side - 1

    def unite(index_a: int, index_b: int) -> int:
        """Union two components, combining their label sets and spill flag."""
        root_a = disjoint.find(index_a)
        root_b = disjoint.find(index_b)
        if root_a == root_b:
            return root_a
        labels = comp_labels.get(root_a, set()) | comp_labels.get(root_b, set())
        spill = comp_spill.get(root_a, False) or comp_spill.get(root_b, False)
        new_root = disjoint.union(root_a, root_b)
        dead = root_b if new_root == root_a else root_a
        comp_labels[new_root] = labels
        comp_spill[new_root] = spill
        comp_labels.pop(dead, None)
        comp_spill.pop(dead, None)
        return new_root

    while heap:
        level = heap[0][0]

        # ---- Phase 1: discover every plateau at this exact level ----------
        plateaus: List[Dict[str, Any]] = []
        while heap and heap[0][0] == level:
            _, first_index = mod_heapq.heappop(heap)
            if status[first_index] != STATUS_UNVISITED:
                continue
            cells_in_plateau: List[int] = []
            stack = [first_index]
            status[first_index] = STATUS_FLOODED
            touches_border = False
            while stack:
                current = stack.pop()
                cells_in_plateau.append(current)
                if is_border(current):
                    touches_border = True
                for neighbour in neighbours(current):
                    if (status[neighbour] == STATUS_UNVISITED
                            and elevations[neighbour] == level):
                        status[neighbour] = STATUS_FLOODED
                        stack.append(neighbour)
            anchor = min(cells_in_plateau)
            for current in cells_in_plateau:
                disjoint.union(anchor, current)
            plateau_root = disjoint.find(anchor)
            comp_labels[plateau_root] = set()
            comp_spill[plateau_root] = touches_border
            plateaus.append({
                'cells': cells_in_plateau,
                'anchor': anchor,
                'root': plateau_root,
                'border': touches_border,
                'lower_roots': set(),
            })

        # Snapshot the distinct, strictly-lower components each plateau meets.
        for plateau in plateaus:
            lower_roots = plateau['lower_roots']
            for current in plateau['cells']:
                for neighbour in neighbours(current):
                    if (status[neighbour] == STATUS_FLOODED
                            and elevations[neighbour] < level):
                        lower_roots.add(disjoint.find(neighbour))

        # ---- Phase 2: birth depressions on local-minimum plateaus ----------
        # Deterministic ids follow the plateaus' smallest cell index.
        for plateau in sorted(plateaus, key=lambda item: item['anchor']):
            if plateau['lower_roots']:
                continue
            root = plateau['root']
            if plateau['border']:
                comp_spill[root] = True
                continue
            label = next_depression_id
            next_depression_id += 1
            comp_labels[root] = {label}
            seed_row, seed_column = divmod(plateau['anchor'], side)
            basin_birth_level[label] = level
            basin_seed[label] = (seed_row, seed_column)
            basin_birth_cells[label] = tuple(sorted(plateau['cells']))

        # ---- Phase 3: connect plateaus to every lower component ------------
        for plateau in plateaus:
            anchor = plateau['anchor']
            for lower_root in plateau['lower_roots']:
                unite(anchor, lower_root)

        # ---- Phase 4: classify each resulting component exactly once -------
        component_cells: Dict[int, List[int]] = {}
        for plateau in plateaus:
            root = disjoint.find(plateau['anchor'])
            component_cells.setdefault(root, []).extend(plateau['cells'])

        for root, new_cells in component_cells.items():
            labels = frozenset(comp_labels.get(root, set()))
            spilled = comp_spill.get(root, False)

            if spilled:
                if labels and labels not in spilled_label_sets:
                    spilled_label_sets.add(labels)
                    spill_row, spill_column = divmod(min(new_cells), side)
                    border_spills.append({
                        'level': level,
                        'members': sorted(labels),
                        'cell_row': spill_row,
                        'cell_column': spill_column,
                    })
                continue

            # Two or more distinct depression labels first share one component:
            # a genuine merge.  Re-encountering an already-formed label set while
            # a basin merely extends uphill emits nothing.
            if len(labels) >= 2 and labels not in emitted_label_sets:
                emitted_label_sets.add(labels)
                saddle_row, saddle_column = divmod(min(new_cells), side)
                merge_events.append(MergeEvent(
                    level=level,
                    members=tuple(sorted(labels)),
                    saddle=(saddle_row, saddle_column),
                    flooded_cells=disjoint.size[root],
                ))

    return BasinMergeTable(
        file_name=file_name,
        side=side,
        cell_area=cell_area,
        merge_events=merge_events,
        basin_birth_level=basin_birth_level,
        basin_seed=basin_seed,
        basin_birth_cells=basin_birth_cells,
        border_spills=border_spills,
    )


def build_from_hgt_file(path: str, cell_area: float = 1.0) -> BasinMergeTable:
    """Convenience wrapper: load a local ``.hgt`` and build its merge table."""
    grid, side = load_hgt_file(path)
    return build_basin_merge_table(
        grid, side, file_name=mod_os.path.basename(path), cell_area=cell_area)


def main(argv: Optional[List[str]] = None) -> int:
    """Command line entry: build and persist a merge table for a local HGT."""
    import argparse as mod_argparse

    parser = mod_argparse.ArgumentParser(
        description='Build a queryable depression-merge table from a local .hgt file')
    parser.add_argument('hgt_path', help='path to the local .hgt sample grid')
    parser.add_argument('--json', dest='json_path',
                        help='write the full merge table as JSON')
    parser.add_argument('--csv', dest='csv_path',
                        help='write a flat one-row-per-merge CSV table')
    parser.add_argument('--cell-area', dest='cell_area', type=float, default=1.0,
                        help='area represented by one grid cell (default 1.0)')
    args = parser.parse_args(argv)

    table = build_from_hgt_file(args.hgt_path, cell_area=args.cell_area)
    if args.json_path:
        table.save(args.json_path)
    if args.csv_path:
        table.save_csv(args.csv_path)

    print('file:            %s' % table.file_name)
    print('grid side:       %d (%d cells)' % (table.side, table.side * table.side))
    print('depressions:     %d' % len(table.basin_ids()))
    print('merge events:    %d' % len(table.merge_events))
    print('border spills:   %d' % len(table.border_spills))
    for event in table.merge_events[:10]:
        print('  level %d merges %s at %s'
              % (event.level, event.members, event.saddle))
    if len(table.merge_events) > 10:
        print('  ... %d more' % (len(table.merge_events) - 10))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
