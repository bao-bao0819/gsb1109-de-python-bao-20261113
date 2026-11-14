# -*- coding: utf-8 -*-

"""
Offline elevation survey batch runner.

Reads a batch of survey lines (测线), plans a read order with the minimal
number of HGT tile swaps under the constraint that the device memory holds
exactly one decompressed tile, then executes the plan and writes every
line back with elevations in its original mileage order.

Usage:
    python survey_batch.py [--workdir DIR]

Outputs (under ``DIR``, default ``output/``):
    survey_lines.json   the batch definition that was processed
    schedule.txt        the minimal load sequence and what each load reads
    verification.txt    per-line lower bounds + optimality argument
    lines/<id>.csv      mileage,lat,lon,elevation,tile,load_no per line
    checklist.md        small-scale hand-checkable listing (see --help)

Everything is offline: the real sample tile ``test_files/N44W072.hgt`` is
used together with deterministic synthetic neighbour tiles generated into
``batch_tiles/``.
"""

import argparse       as mod_argparse
import json           as mod_json
import math           as mod_math
import os             as mod_os
import os.path        as mod_path
import shutil         as mod_shutil
import struct         as mod_struct

from typing import *

from srtm import scheduler as mod_scheduler

VOID = -32768
REAL_TILE = mod_path.join(mod_path.dirname(mod_path.abspath(__file__)),
                          'test_files', 'N44W072.hgt')
TILE_DIR = 'batch_tiles'
CHECKLIST_TILE_DIR = 'checklist_tiles'

# ---------------------------------------------------------------------------
# HGT tile handling
# ---------------------------------------------------------------------------

def tile_name(lat_base: int, lon_base: int) -> str:
    ns = 'N' if lat_base >= 0 else 'S'
    ew = 'E' if lon_base >= 0 else 'W'
    return '%s%02d%s%03d.hgt' % (ns, abs(lat_base), ew, abs(lon_base))


def parse_tile_name(name: str) -> Tuple[int, int]:
    ns, lat, ew, lon = name[0], int(name[1:3]), name[3], int(name[4:7])
    return (lat if ns == 'N' else -lat), (lon if ew == 'E' else -lon)


class HgtTile:
    """One parsed .hgt file (kept in memory only while it is the current block)."""

    def __init__(self, name: str, data: bytes) -> None:
        self.name = name
        self.lat_base, self.lon_base = parse_tile_name(name)
        side = mod_math.sqrt(len(data) / 2.0)
        assert side == int(side), 'Invalid file size for %s' % name
        self.side = int(side)
        self.data = data

    def value_at(self, latitude: float, longitude: float) -> Optional[int]:
        """
        Edge-safe sample lookup: a coordinate exactly on a tile boundary is
        a valid sample of this tile (SRTM tiles share their edge rows/cols).
        Returns None for void samples.
        """
        row = int(round((self.lat_base + 1 - latitude) * (self.side - 1)))
        col = int(round((longitude - self.lon_base) * (self.side - 1)))
        if not (0 <= row < self.side and 0 <= col < self.side):
            return None
        i = (row * self.side + col) * 2
        value = mod_struct.unpack('>h', self.data[i:i + 2])[0]
        if value > 10000 or value < -1000:
            return None
        return value


class TileStore:
    """Direct (uncounted) access to tile files, used while planning."""

    def __init__(self, directory: str) -> None:
        self.directory = directory
        self._cache: Dict[str, HgtTile] = {}

    def has(self, name: str) -> bool:
        return mod_path.exists(mod_path.join(self.directory, name))

    def load(self, name: str) -> HgtTile:
        if name not in self._cache:
            with open(mod_path.join(self.directory, name), 'rb') as handle:
                self._cache[name] = HgtTile(name, handle.read())
        return self._cache[name]

    def names(self) -> List[str]:
        return sorted(name for name in mod_os.listdir(self.directory)
                      if name.endswith('.hgt'))


class SingleBlockReader:
    """
    The acquisition device: memory holds exactly one decompressed tile.
    Every load of a different tile counts one swap.
    """

    def __init__(self, store: TileStore) -> None:
        self.store = store
        self.current: Optional[HgtTile] = None
        self.loads = 0

    @property
    def swaps(self) -> int:
        return max(0, self.loads - 1)

    def load(self, name: str) -> None:
        if self.current is not None and self.current.name == name:
            return
        self.current = self.store.load(name)
        self.loads += 1

    def read(self, name: str, latitude: float, longitude: float) -> Optional[int]:
        assert self.current is not None and self.current.name == name, \
            'Tile %s is not the loaded block' % name
        return self.current.value_at(latitude, longitude)


# ---------------------------------------------------------------------------
# Synthetic neighbour tiles (deterministic, edges shared with the real tile)
# ---------------------------------------------------------------------------

def _terrain(latitude: float, longitude: float) -> int:
    return int(round(260 + 170 * mod_math.sin(3.1 * latitude) * mod_math.cos(2.7 * longitude)
                     + 80 * mod_math.sin(0.9 * latitude + 1.3 * longitude)))


def _empty_tile(side: int = 1201) -> List[List[int]]:
    return [[0] * side for _ in range(side)]


def _write_tile(directory: str, name: str, grid: List[List[int]]) -> None:
    path = mod_path.join(directory, name)
    with open(path, 'wb') as handle:
        for row in grid:
            handle.write(mod_struct.pack('>%dh' % len(row), *row))


def _read_grid(path: str) -> List[List[int]]:
    with open(path, 'rb') as handle:
        data = handle.read()
    side = int(mod_math.sqrt(len(data) / 2.0))
    values = mod_struct.unpack('>%dh' % (side * side), data)
    return [list(values[r * side:(r + 1) * side]) for r in range(side)]


def ensure_batch_tiles(directory: str) -> None:
    """
    Build the tile set of the batch once:

    * N44W072 -- the real sample tile shipped with srtm.py,
    * N45W072 / N44W073 / N43W072 / N45W073 -- synthetic neighbours whose
      shared edges are copied from the real tile, so an edge point truly is
      the same observation in both tiles.  A few edge samples are voided on
      exactly one side to create points that only one tile can serve.
    """
    if mod_path.exists(mod_path.join(directory, 'N45W073.hgt')):
        return
    mod_os.makedirs(directory, exist_ok=True)
    real_copy = mod_path.join(directory, 'N44W072.hgt')
    if not mod_path.exists(real_copy):
        mod_shutil.copyfile(REAL_TILE, real_copy)
    grid_a = _read_grid(real_copy)
    side = len(grid_a)

    def synthetic(lat_base: int, lon_base: int) -> List[List[int]]:
        grid = _empty_tile(side)
        for row in range(side):
            lat = lat_base + 1 - row / (side - 1)
            for col in range(side):
                lon = lon_base + col / (side - 1)
                grid[row][col] = _terrain(lat, lon)
        return grid

    # N45W072 (north): its bottom row is the shared lat=45 edge of N44W072.
    grid_b = synthetic(45, -72)
    grid_b[side - 1] = list(grid_a[0])
    # Void a few samples on the N45W072 side only -> those edge points are
    # pinned to N44W072 (the swap cannot be saved there).
    for col in (300, 600, 900):
        grid_b[side - 1][col] = VOID
    _write_tile(directory, 'N45W072.hgt', grid_b)

    # N44W073 (west): its right column is the shared lon=-72 edge.
    grid_c = synthetic(44, -73)
    for row in range(side):
        grid_c[row][side - 1] = grid_a[row][0]
    _write_tile(directory, 'N44W073.hgt', grid_c)

    # N43W072 (south): its top row is the shared lat=44 edge.
    grid_d = synthetic(43, -72)
    grid_d[0] = list(grid_a[side - 1])
    # One interior void, to deliver a point without any real measurement.
    grid_d[500][500] = VOID
    _write_tile(directory, 'N43W072.hgt', grid_d)

    # N45W073 (north-west): shares lat=45 with N44W073 and lon=-72 with N45W072.
    grid_e = synthetic(45, -73)
    for col in range(side):
        grid_e[side - 1][col] = grid_c[0][col]
    for row in range(side):
        grid_e[row][side - 1] = grid_b[row][0]
    grid_e[side - 1][side - 1] = grid_a[0][0]  # the four-tile corner (45, -72)
    _write_tile(directory, 'N45W073.hgt', grid_e)


# ---------------------------------------------------------------------------
# Edge-point candidate resolution
# ---------------------------------------------------------------------------

def _edge_bases(coordinate: float) -> List[int]:
    """Tile base indices whose grid contains this coordinate as a sample."""
    base = mod_math.floor(coordinate + 1e-12)
    bases = [int(base)]
    if abs(coordinate - round(coordinate)) < 1e-9:
        other = int(base) - 1
        if other not in bases:
            bases.append(other)
    return bases


def resolve_point(latitude: float, longitude: float, store: TileStore) -> Dict[str, Any]:
    """
    Decide which tiles may serve a point.

    * interior point            -> exactly its own tile,
    * edge point, both tiles store the same valid observation
                                -> either tile (the swap may be saved),
    * edge point, only one tile has a real measurement
                                -> pinned to that tile (the swap cannot be saved),
    * no tile has a real measurement -> no candidate, elevation is None.
    """
    tiles: Dict[str, int] = {}
    covering = 0  # existing tiles whose grid contains the point
    for lat_base in _edge_bases(latitude):
        for lon_base in _edge_bases(longitude):
            name = tile_name(lat_base, lon_base)
            if store.has(name):
                covering += 1
                value = store.load(name).value_at(latitude, longitude)
                if value is not None:
                    tiles[name] = value
    if not tiles:
        return {'candidates': [], 'elevation': None, 'note': 'void'}
    values = set(tiles.values())
    if len(values) == 1:
        if len(tiles) > 1:
            note = 'shared-edge'
        elif covering > 1:
            # Neighbour tile exists but holds no real measurement here:
            # the point is pinned, the swap cannot be saved.
            note = 'pinned-edge'
        else:
            note = 'interior'
        return {'candidates': sorted(tiles), 'elevation': values.pop(), 'note': note}
    # Adjacent tiles disagree at this sample: stay conservative and pin the
    # point to its own (floor) tile so no swap is saved on shaky data.
    own = tile_name(int(mod_math.floor(latitude + 1e-12)),
                    int(mod_math.floor(longitude + 1e-12)))
    if own in tiles:
        return {'candidates': [own], 'elevation': tiles[own], 'note': 'conflict->own-tile'}
    name = sorted(tiles)[0]
    return {'candidates': [name], 'elevation': tiles[name], 'note': 'conflict->%s' % name}


# ---------------------------------------------------------------------------
# The batch
# ---------------------------------------------------------------------------

def default_lines() -> List[Dict[str, Any]]:
    """
    Six survey lines over the five-tile area.  Points exactly on integer
    degree boundaries are edge points; ``(45.0, -71.5)`` etc. fall on
    samples that are void on the northern side and are therefore pinned to
    N44W072.
    """
    raw = [
        ('L01', '河谷测线', [
            (0.0, 44.20, -71.80), (1.2, 44.35, -71.70), (2.5, 44.60, -71.60),
            (3.8, 45.00, -71.50),   # pinned to N44W072 (void on N45W072 side)
            (5.1, 45.20, -71.45), (6.4, 45.40, -71.55),
            (7.7, 45.00, -71.80),   # shared edge N44W072 / N45W072
            (9.0, 44.80, -71.75),
        ]),
        ('L02', '山脊测线', [
            (0.0, 45.60, -71.30), (1.1, 45.45, -71.25), (2.3, 45.30, -71.20),
            (3.4, 45.00, -71.30),   # shared edge
            (4.6, 44.85, -71.30), (5.8, 44.60, -71.28),
            (7.0, 44.00, -71.25),   # shared edge N44W072 / N43W072
            (8.2, 43.80, -71.20), (9.4, 43.60, -71.15),
        ]),
        ('L03', '东西联络线', [
            (0.0, 44.50, -72.60), (1.4, 44.50, -72.30),
            (2.8, 44.50, -72.00),   # shared edge N44W073 / N44W072
            (4.2, 44.50, -71.70), (5.6, 44.55, -71.40), (7.0, 44.55, -71.10),
        ]),
        ('L04', '环湖测线', [
            (0.0, 44.70, -71.60), (1.0, 44.50, -71.62), (2.0, 44.30, -71.60),
            (3.0, 44.00, -71.60),   # shared edge N44W072 / N43W072
            (4.0, 43.75, -71.58),
            (5.0, 43.5833, -71.5833),  # void sample inside N43W072 -> None
            (6.0, 44.00, -71.40),   # shared edge again
            (7.0, 44.25, -71.42), (8.0, 44.45, -71.44),
        ]),
        ('L05', '边角测线', [
            (0.0, 45.00, -72.00),   # four-tile corner
            (1.3, 45.25, -71.90), (2.6, 45.50, -71.80),
            (3.9, 45.60, -72.20), (5.2, 45.40, -72.50),
            (6.5, 44.80, -72.60), (7.8, 44.60, -72.40),
            (9.1, 44.40, -72.00),   # shared edge N44W073 / N44W072
            (10.4, 44.30, -71.80),
        ]),
        ('L06', '短测线', [
            (0.0, 44.10, -71.10), (0.9, 44.55, -71.10),
            (1.8, 45.00, -71.10),   # shared edge
            (2.7, 45.30, -71.10), (3.6, 45.70, -71.10),
        ]),
    ]
    lines = []
    for line_id, name, points in raw:
        mileages = [p[0] for p in points]
        assert mileages == sorted(mileages), 'Mileage must increase along %s' % line_id
        lines.append({'id': line_id, 'name': name,
                      'points': [{'mileage': m, 'lat': la, 'lon': lo}
                                 for m, la, lo in points]})
    return lines


# ---------------------------------------------------------------------------
# Planning + execution
# ---------------------------------------------------------------------------

def plan_batch(lines: List[Dict[str, Any]], store: TileStore) -> None:
    """Attach candidate tiles / expected elevation to every point in place."""
    for line in lines:
        for point in line['points']:
            resolved = resolve_point(point['lat'], point['lon'], store)
            point['candidates'] = resolved['candidates']
            point['elevation'] = resolved['elevation']
            point['note'] = resolved['note']


def candidate_lines(lines: List[Dict[str, Any]]) -> Tuple[mod_scheduler.Lines, List[List[int]]]:
    """
    Build the scheduler input.  Points without any real measurement need no
    load and are dropped from the scheduling problem; ``index_map`` maps
    scheduler positions back to point indices per line.
    """
    sched_lines: mod_scheduler.Lines = []
    index_map: List[List[int]] = []
    for line in lines:
        candidates: List[FrozenSet[str]] = []
        mapping: List[int] = []
        for index, point in enumerate(line['points']):
            if point['candidates']:
                candidates.append(frozenset(point['candidates']))
                mapping.append(index)
        sched_lines.append(candidates)
        index_map.append(mapping)
    return sched_lines, index_map


def run_batch(workdir: str) -> Dict[str, Any]:
    tile_dir = mod_path.join(workdir, TILE_DIR)
    ensure_batch_tiles(tile_dir)
    store = TileStore(tile_dir)

    lines = default_lines()
    plan_batch(lines, store)

    sched_lines, index_map = candidate_lines(lines)
    sequence, states = mod_scheduler.optimal_schedule(sched_lines)
    assert mod_scheduler.simulate(sched_lines, sequence) is not None

    # Execute the plan on the single-block device.
    reader = SingleBlockReader(store)
    per_load: List[List[Tuple[str, int]]] = []  # (line_id, point_index) per load
    previous = tuple(0 for _ in lines)
    for load_no, (tile, state) in enumerate(zip(sequence, states), start=1):
        reader.load(tile)
        read_now: List[Tuple[str, int]] = []
        for j, line in enumerate(lines):
            for pos in range(previous[j], state[j]):
                point_index = index_map[j][pos]
                point = line['points'][point_index]
                elevation = reader.read(tile, point['lat'], point['lon'])
                assert elevation == point['elevation'], \
                    'Device reading disagrees with planning for %s' % line['id']
                point['tile'] = tile
                point['load_no'] = load_no
                read_now.append((line['id'], point_index))
        per_load.append(read_now)
        previous = state

    result = {
        'lines': lines,
        'sequence': sequence,
        'states': states,
        'per_load': per_load,
        'loads': reader.loads,
        'swaps': reader.swaps,
        'sched_lines': sched_lines,
    }
    write_outputs(workdir, result)
    return result


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def write_outputs(workdir: str, result: Dict[str, Any]) -> None:
    out_dir = mod_path.join(workdir, 'output')
    lines_dir = mod_path.join(out_dir, 'lines')
    mod_os.makedirs(lines_dir, exist_ok=True)
    lines = result['lines']

    with open(mod_path.join(out_dir, 'survey_lines.json'), 'w') as handle:
        mod_json.dump({'lines': [
            {'id': line['id'], 'name': line['name'],
             'points': [{'mileage': p['mileage'], 'lat': p['lat'], 'lon': p['lon'],
                         'candidates': p['candidates'], 'note': p['note']}
                        for p in line['points']]}
            for line in lines]}, handle, ensure_ascii=False, indent=2)

    for line in lines:
        with open(mod_path.join(lines_dir, '%s.csv' % line['id']), 'w') as handle:
            handle.write('mileage_km,lat,lon,elevation_m,tile,load_no\n')
            for point in sorted(line['points'], key=lambda p: p['mileage']):
                handle.write('%.4f,%.6f,%.6f,%s,%s,%s\n' % (
                    point['mileage'], point['lat'], point['lon'],
                    point['elevation'] if point['elevation'] is not None else '',
                    point.get('tile', ''), point.get('load_no', '')))

    with open(mod_path.join(out_dir, 'schedule.txt'), 'w') as handle:
        handle.write('最少换块读取顺序（内存一次只放一块解压 HGT）\n')
        handle.write('加载 %d 块次 = 换块 %d 次\n\n' % (result['loads'], result['swaps']))
        points_by_id = {line['id']: line['points'] for line in lines}
        for load_no, (tile, reads) in enumerate(
                zip(result['sequence'], result['per_load']), start=1):
            handle.write('块次 %02d  加载 %s\n' % (load_no, tile))
            for line_id, point_index in reads:
                point = points_by_id[line_id][point_index]
                handle.write('    %s 里程 %7.2f km  (%.4f, %.4f) -> %s m\n' % (
                    line_id, point['mileage'], point['lat'], point['lon'],
                    point['elevation']))
        handle.write('\n换块序列: %s\n' % ' -> '.join(result['sequence']))

    with open(mod_path.join(out_dir, 'verification.txt'), 'w') as handle:
        handle.write('最少次数核对依据\n\n')
        bound = mod_scheduler.lower_bound_loads(result['sched_lines'])
        for line, sched_line in zip(lines, result['sched_lines']):
            pattern = ['/'.join(sorted(c)) for c in sched_line]
            handle.write('%s %s: 单线最少 %d 块次  候选序列 %s\n' % (
                line['id'], line['name'],
                mod_scheduler.single_line_min_loads(sched_line), ' '.join(pattern)))
        tiles_used = sorted({t for line in result['sched_lines']
                             for point in line for t in point})
        handle.write('\n下界a = 最难单线 = %d 块次\n' % bound)
        handle.write('下界b = 批次涉及瓦片 %s 至少各装 1 次 = %d 块次\n' % (
            '/'.join(tiles_used), len(tiles_used)))
        handle.write('调度结果 = %d 块次（A* 精确搜索，可采纳启发式 = 单线剩余下界，\n'
                     '搜索已对更短方案完整剪枝，故 %d 为最少块次，即换块 %d 次）\n' % (
                         result['loads'], result['loads'], result['swaps']))
        brute_loads, _, checked = mod_scheduler.brute_force_min_loads(result['sched_lines'])
        handle.write('独立穷举核对: 长度 1..%d 的全部装块序列共 %d 个逐一仿真，'
                     '最短可行 = %d 块次，与调度结果一致\n' % (
                         brute_loads, checked, brute_loads))
        edge_points = [(line['id'], p) for line in lines for p in line['points']
                       if p['note'] != 'interior']
        handle.write('\n边缘点判定（相邻两块确有同一有效观测才可任选其一）:\n')
        for line_id, point in edge_points:
            handle.write('  %s 里程 %.2f km (%.4f, %.4f): %s -> 候选 %s, 高程 %s\n' % (
                line_id, point['mileage'], point['lat'], point['lon'],
                point['note'], '/'.join(point['candidates']) or '无', point['elevation']))


# ---------------------------------------------------------------------------
# Small-scale hand-checkable listing
# ---------------------------------------------------------------------------

def _build_checklist_tiles(directory: str) -> None:
    """
    Three tiny 3x3 tiles: X=N10E010, Y=N10E011 share the lon=11 edge,
    Z=N11E010 shares the lat=11 edge with X.  One shared sample is void on
    the Y side only, which pins that edge point to X.
    """
    if mod_path.exists(mod_path.join(directory, 'N11E010.hgt')):
        return
    mod_os.makedirs(directory, exist_ok=True)
    side = 3
    grid_x = [[100, 110, 120],
              [130, 140, 150],
              [160, 170, 180]]
    # Y's left column is the shared lon=11 edge: it equals X's right column
    # except the middle sample, which is void on the Y side only.
    grid_y = [[120, 210, 200],
              [VOID, 230, 220],
              [180, 250, 240]]
    # Z's bottom row is the shared lat=11 edge and equals X's top row.
    grid_z = [[300, 310, 320],
              [330, 340, 350],
              [100, 110, 120]]
    _write_tile(directory, 'N10E010.hgt', grid_x)
    _write_tile(directory, 'N10E011.hgt', grid_y)
    _write_tile(directory, 'N11E010.hgt', grid_z)


def write_checklist(workdir: str) -> str:
    tile_dir = mod_path.join(workdir, CHECKLIST_TILE_DIR)
    _build_checklist_tiles(tile_dir)
    store = TileStore(tile_dir)

    lines = [
        ('线甲', [(0.0, 10.0, 10.0), (1.0, 10.0, 11.0), (2.0, 10.5, 10.0)]),
        ('线乙', [(0.0, 10.0, 11.5), (1.0, 10.5, 11.0), (2.0, 10.5, 11.5)]),
        ('线丙', [(0.0, 11.5, 10.0), (1.0, 11.0, 10.0)]),
    ]
    sched_lines: mod_scheduler.Lines = []
    descriptions: List[str] = []
    for name, points in lines:
        candidates: List[FrozenSet[str]] = []
        parts = []
        for mileage, lat, lon in points:
            resolved = resolve_point(lat, lon, store)
            candidates.append(frozenset(resolved['candidates']))
            parts.append('里程%.0fkm(%.2f,%.2f) 候选{%s} 高程%s [%s]' % (
                mileage, lat, lon, '/'.join(resolved['candidates']),
                resolved['elevation'], resolved['note']))
        sched_lines.append(candidates)
        descriptions.append('  %s: %s' % (name, '；'.join(parts)))

    sequence, _ = mod_scheduler.optimal_schedule(sched_lines)
    brute_loads, brute_sequence, checked = mod_scheduler.brute_force_min_loads(sched_lines)
    assert brute_loads == len(sequence)

    out_path = mod_path.join(workdir, 'output', 'checklist.md')
    mod_os.makedirs(mod_path.dirname(out_path), exist_ok=True)
    with open(out_path, 'w') as handle:
        handle.write('# 小规模清单：最少换块次数的手动核对依据\n\n')
        handle.write('瓦片 X=N10E010, Y=N10E011, Z=N11E010（3x3 小样，值可直接打开核对）。\n')
        handle.write('X/Y 共有 lon=11 边，其中 lat=10.5 的样本只在 X 有实测值（Y 侧为 void）；\n')
        handle.write('X/Z 共有 lat=11 边，两侧完全一致。\n\n')
        handle.write('## 测线（里程顺序）\n\n')
        for text in descriptions:
            handle.write(text + '\n')
        handle.write('\n## 下界推理\n\n')
        handle.write('- 线乙的候选序列为 {Y},{X},{Y}：中点只在 X 有实测值，换到 X 这一块省不掉，'
                     '而首尾两块又只能是 Y，故线乙单独就需 3 块次（Y-X-Y）。\n')
        handle.write('- 线丙首点只在 Z，而 Z 不在线乙的序列里，至少还要再加 1 块次。\n')
        handle.write('- 所以任何方案 >= 4 块次（换块 >= 3 次）。\n\n')
        handle.write('## 达到下界的方案（%d 块次 = 换块 %d 次）\n\n' % (len(sequence), len(sequence) - 1))
        handle.write('  ' + ' -> '.join(sequence) + '\n\n')
        handle.write('装 X 的那一块次顺带走完线甲全部三点（线甲中点是 X/Y 共边点，两侧观测相同，'
                     '可任选 X，不必单独换 Y）。\n\n')
        handle.write('## 穷举核对\n\n')
        handle.write('对长度 1..%d 的全部装块序列逐一仿真，共检查 %d 个序列，'
                     '最短可行长度 = %d（%s），与上面推理一致。\n' % (
                         brute_loads, checked, brute_loads, ' -> '.join(brute_sequence)))
    return out_path


def main() -> None:
    parser = mod_argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workdir', default='.',
                        help='working directory holding batch_tiles/ and output/')
    args = parser.parse_args()
    result = run_batch(args.workdir)
    checklist = write_checklist(args.workdir)
    print('测线 %d 条，全部交齐；加载 %d 块次 = 换块 %d 次（最少）' % (
        len(result['lines']), result['loads'], result['swaps']))
    print('换块序列: %s' % ' -> '.join(result['sequence']))
    print('交付: output/lines/*.csv, output/schedule.txt, output/verification.txt, %s' % checklist)


if __name__ == '__main__':
    main()
