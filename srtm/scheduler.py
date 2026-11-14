# -*- coding: utf-8 -*-

"""
Minimum-tile-load scheduler for offline elevation survey batches.

Problem setting
---------------
There are several survey lines (测线).  Each line is an ordered list of
points that must be read in mileage (里程) order.  Points of different
lines may be interleaved freely.  The acquisition device can hold exactly
one decompressed HGT tile in memory, so every time a different tile is
needed the current block has to be swapped out.

Every point carries a *candidate set* of tiles that may serve it:

* an interior point has exactly one candidate tile,
* an edge point where two adjacent tiles store the same valid observation
  may be served by either tile (candidate set of two),
* an edge point where only one tile holds a real measurement is pinned to
  that tile (the swap to that tile cannot be saved).

This module finds a read order with the provably minimal number of tile
loads (loads = swaps + 1, minimising one minimises the other) and also
provides the certificates needed to check the minimal count on small
instances by hand.
"""

import heapq         as mod_heapq
import itertools     as mod_itertools

from typing import *

Candidates = List[FrozenSet[str]]
Lines = List[Candidates]


def _advance_one(line: Candidates, index: int, tile: str) -> int:
    """Chain-advance a single line while its next points accept ``tile``."""
    while index < len(line) and tile in line[index]:
        index += 1
    return index


def advance_state(lines: Lines, state: Tuple[int, ...], tile: str) -> Tuple[int, ...]:
    """
    Load ``tile`` and read every point that becomes readable.

    Reading every available point that accepts the loaded tile can never
    hurt: it only relaxes the ordering constraints of the successors, so
    for a fixed load sequence this maximal advance is always optimal.
    """
    return tuple(_advance_one(line, state[j], tile)
                 for j, line in enumerate(lines))


def _goal(lines: Lines) -> Tuple[int, ...]:
    return tuple(len(line) for line in lines)


def _frontier_tiles(lines: Lines, state: Tuple[int, ...]) -> List[str]:
    tiles: Set[str] = set()
    for j, line in enumerate(lines):
        if state[j] < len(line):
            tiles.update(line[state[j]])
    return sorted(tiles)


def single_line_remaining_loads(line: Candidates) -> List[int]:
    """
    ``rem[i]`` = minimal number of tile loads needed to read the points
    ``line[i:]`` of one isolated line.  Computed with an O(n^2) DP:

        rem[i] = 1 + min rem[e]  over all e > i such that the points
                 i .. e-1 share at least one common candidate tile.
    """
    n = len(line)
    rem = [0] * (n + 1)
    for i in range(n - 1, -1, -1):
        best = n - i  # one load per point always works
        common: Optional[Set[str]] = None
        for e in range(i + 1, n + 1):
            common = set(line[e - 1]) if common is None else common & line[e - 1]
            if not common:
                break
            cost = 1 + rem[e]
            if cost < best:
                best = cost
        rem[i] = best
    return rem


def single_line_min_loads(line: Candidates) -> int:
    """Minimal loads for one isolated line (a lower bound certificate)."""
    return single_line_remaining_loads(line)[0]


def lower_bound_loads(lines: Lines) -> int:
    """
    Admissible lower bound for the whole batch: every line must be
    finished, so the batch needs at least as many loads as the hardest
    single line.
    """
    if not lines:
        return 0
    return max(single_line_min_loads(line) for line in lines)


def greedy_schedule(lines: Lines) -> List[str]:
    """
    Fast constructive heuristic: repeatedly load the tile that reads the
    most currently reachable points (ties broken by tile name).  Returns
    the load sequence; used as an upper bound for the exact search.
    """
    state = tuple(0 for _ in lines)
    goal = _goal(lines)
    sequence: List[str] = []
    while state != goal:
        best_tile: Optional[str] = None
        best_key: Optional[Tuple[int, int]] = None
        for tile in _frontier_tiles(lines, state):
            new_state = advance_state(lines, state, tile)
            gain = sum(new_state[j] - state[j] for j in range(len(lines)))
            key = (gain, sum(new_state))
            if best_key is None or key > best_key or (key == best_key and tile < best_tile):  # type: ignore
                best_key = key
                best_tile = tile
        assert best_tile is not None
        sequence.append(best_tile)
        state = advance_state(lines, state, best_tile)
    return sequence


def optimal_schedule(lines: Lines) -> Tuple[List[str], List[Tuple[int, ...]]]:
    """
    Exact minimum-load schedule.

    A* search over the progress vector ``(i_1, ..., i_k)`` (how many points
    of each line have been read).  The heuristic ``h(state)`` is the
    hardest single line's remaining-loads table, which is admissible, and
    the greedy solution prunes everything that cannot improve on it.

    Returns ``(tiles, states)``: the load sequence and the progress state
    reached after each load (the points read during load ``m`` are exactly
    those between ``states[m-1]`` and ``states[m]``).
    """
    lines = [list(line) for line in lines]
    for line in lines:
        for point in line:
            assert point, 'Every schedulable point needs a non-empty candidate set'

    goal = _goal(lines)
    start = tuple(0 for _ in lines)
    if start == goal:
        return [], []

    remaining = [single_line_remaining_loads(line) for line in lines]

    def heuristic(state: Tuple[int, ...]) -> int:
        return max(remaining[j][state[j]] for j in range(len(lines)))

    best_sequence = greedy_schedule(lines)
    best_states: List[Tuple[int, ...]] = []
    state = start
    for tile in best_sequence:
        state = advance_state(lines, state, tile)
        best_states.append(state)
    upper = len(best_sequence)

    if heuristic(start) >= upper:
        return best_sequence, best_states

    distances = {start: 0}
    parents: Dict[Tuple[int, ...], Tuple[Tuple[int, ...], str]] = {}
    queue: List[Tuple[int, int, Tuple[int, ...]]] = [(heuristic(start), 0, start)]

    while queue:
        _, cost, state = mod_heapq.heappop(queue)
        if cost > distances.get(state, cost):
            continue
        if cost + heuristic(state) >= upper:
            continue
        if state == goal:
            upper = cost
            sequence: List[str] = []
            states: List[Tuple[int, ...]] = []
            node = state
            while node != start:
                previous, tile = parents[node]
                sequence.append(tile)
                states.append(node)
                node = previous
            best_sequence = list(reversed(sequence))
            best_states = list(reversed(states))
            continue
        for tile in _frontier_tiles(lines, state):
            new_state = advance_state(lines, state, tile)
            new_cost = cost + 1
            if new_cost + heuristic(new_state) >= upper:
                continue
            if new_cost < distances.get(new_state, new_cost + 1):
                distances[new_state] = new_cost
                parents[new_state] = (state, tile)
                mod_heapq.heappush(queue, (new_cost + heuristic(new_state), new_cost, new_state))

    return best_sequence, best_states


def simulate(lines: Lines, sequence: Sequence[str]) -> Optional[List[Tuple[int, ...]]]:
    """
    Independent feasibility check: play a fixed load sequence with maximal
    advance.  Returns the progress states, or ``None`` if the sequence
    fails to read every point.
    """
    state = tuple(0 for _ in lines)
    states = []
    for tile in sequence:
        state = advance_state(lines, state, tile)
        states.append(state)
    if state != _goal(lines):
        return None
    return states


def brute_force_min_loads(lines: Lines, tiles: Optional[Sequence[str]] = None) -> Tuple[int, List[str], int]:
    """
    Independent exhaustive check for small instances: try every load
    sequence of length 1, 2, ... until one reads all points.  Returns
    ``(min_loads, sequence, sequences_checked)`` where the last element is
    the number of candidate sequences that were tested -- this is the
    hand-checkable evidence that no shorter schedule exists.
    """
    if tiles is None:
        tile_set: Set[str] = set()
        for line in lines:
            for point in line:
                tile_set.update(point)
        tiles = sorted(tile_set)
    total_points = sum(len(line) for line in lines)
    checked = 0
    for length in range(1, total_points + 1):
        for sequence in mod_itertools.product(tiles, repeat=length):
            checked += 1
            if simulate(lines, sequence) is not None:
                return length, list(sequence), checked
    raise AssertionError('A load per point always works, this is unreachable')
