"""Budget-constrained tile download planner built on mercantile.

A survey crew has circled observation points. Each point carries a hard
minimum zoom, a desired (maximum useful) zoom and a weight. A download
budget caps how many tiles may be requested. Points falling in the same
tile share the request, and a point covered by both a parent and a child
tile only yields its benefit once (at the deepest covering tile).

The planner first guarantees every hard minimum zoom, then maximizes the
total weighted clarity, defined as sum(weight * achieved zoom). If the
budget cannot even satisfy the hard minimums, it reports the minimum
number of additional requests that would be needed.

Input JSON:
    {
      "budget": 12,
      "points": [
        {"name": "A", "lon": 103.6, "lat": 30.9,
         "min_zoom": 10, "max_zoom": 13, "weight": 2.0},
        ...
      ]
    }

Usage:
    python download_plan.py points.json [--budget N] [--json]
"""

import argparse
import json
import sys
from dataclasses import dataclass

import mercantile

NEG_INF = float("-inf")
MAX_ZOOM = 26


@dataclass
class Point:
    lon: float
    lat: float
    min_zoom: int
    max_zoom: int
    weight: float = 1.0
    name: str = ""


class _Node:
    __slots__ = ("tile", "children", "point_indices")

    def __init__(self, tile):
        self.tile = tile
        self.children = []
        self.point_indices = []  # points whose desired-zoom tile is this node


def _validate(points):
    for p in points:
        if not (0 <= p.min_zoom <= p.max_zoom <= MAX_ZOOM):
            raise ValueError(
                "point %r: require 0 <= min_zoom <= max_zoom <= %d"
                % (p.name or (p.lon, p.lat), MAX_ZOOM)
            )
        if p.weight <= 0:
            raise ValueError("point %r: weight must be positive" % (p.name,))


def _build_tree(points):
    """Quadtree of every tile that may cover a point within its zoom window."""
    z0 = min(p.min_zoom for p in points)
    nodes = {}
    for idx, p in enumerate(points):
        for z in range(z0, p.max_zoom + 1):
            t = mercantile.tile(p.lon, p.lat, z, truncate=True)
            if t not in nodes:
                nodes[t] = _Node(t)
        top = mercantile.tile(p.lon, p.lat, p.max_zoom, truncate=True)
        nodes[top].point_indices.append(idx)

    root = _Node(mercantile.Tile(0, 0, z0 - 1))  # virtual root, never selected
    for node in nodes.values():
        if node.tile.z == z0:
            root.children.append(node)
        else:
            nodes[mercantile.parent(node.tile)].children.append(node)
    return root


def _min_requests(root, points):
    """Minimum tile count satisfying every hard minimum zoom."""
    memo = {}

    def solve(node, anc):
        key = (id(node), anc)
        if key in memo:
            return memo[key]
        z = node.tile.z
        best = float("inf")
        for sel in (0, 1):
            covering = z if sel else anc
            if any(covering < points[i].min_zoom for i in node.point_indices):
                continue
            cost = sel
            for child in node.children:
                cost += solve(child, z if sel else anc)
            best = min(best, cost)
        memo[key] = best
        return best

    return solve(root, -1)


def plan_downloads(points, budget):
    """Choose up to ``budget`` tiles for ``points``.

    Returns a dict with keys:
      feasible       -- bool, True if every hard minimum zoom is met
      tiles          -- list of mercantile.Tile to download (empty if infeasible)
      requests       -- number of tiles in the list
      total_clarity  -- sum(weight * achieved zoom) over points
      min_required   -- minimum requests needed for the hard minimums
      shortfall      -- min_required - budget (0 when feasible)
      assignments    -- per-point list of (point, achieved zoom)
    """
    if not points:
        raise ValueError("at least one point is required")
    _validate(points)
    root = _build_tree(points)
    min_required = _min_requests(root, points)

    result = {
        "feasible": False,
        "tiles": [],
        "requests": 0,
        "total_clarity": 0.0,
        "min_required": min_required,
        "shortfall": max(0, min_required - budget),
        "assignments": [],
    }
    if min_required > budget:
        return result

    memo = {}

    def solve(node, anc):
        """Best subtree benefit per spend, given deepest selected ancestor zoom."""
        key = (id(node), anc)
        if key in memo:
            return memo[key][0]
        z = node.tile.z
        best = [NEG_INF] * (budget + 1)
        choices = {}
        for sel in (0, 1):
            child_anc = z if sel else anc
            covering = z if sel else anc
            base = 0.0
            feasible = True
            for idx in node.point_indices:
                p = points[idx]
                if covering < p.min_zoom:
                    feasible = False
                    break
                base += p.weight * covering
            if not feasible:
                continue
            acc = [NEG_INF] * (budget + 1)
            acc[0] = 0.0
            splits = []
            for child in node.children:
                gain = solve(child, child_anc)
                merged = [NEG_INF] * (budget + 1)
                split = [None] * (budget + 1)
                for spent_before in range(budget + 1):
                    if acc[spent_before] == NEG_INF:
                        continue
                    for spent_child in range(budget + 1 - spent_before):
                        if gain[spent_child] == NEG_INF:
                            continue
                        total = spent_before + spent_child
                        value = acc[spent_before] + gain[spent_child]
                        if value > merged[total]:
                            merged[total] = value
                            split[total] = (spent_child, spent_before)
                splits.append(split)
                acc = merged
            for k in range(sel, budget + 1):
                if acc[k - sel] == NEG_INF:
                    continue
                value = acc[k - sel] + base
                if value > best[k]:
                    best[k] = value
                    choices[k] = (sel, splits)
        memo[key] = (best, choices)
        return best

    def reconstruct(node, anc, k, out):
        best, choices = memo[(id(node), anc)]
        sel, splits = choices[k]
        if sel:
            out.append(node.tile)
        child_anc = node.tile.z if sel else anc
        remaining = k - sel
        for child, split in zip(reversed(node.children), reversed(splits)):
            spent_child, spent_before = split[remaining]
            reconstruct(child, child_anc, spent_child, out)
            remaining = spent_before

    root_gains = solve(root, -1)
    best_k = max(
        (k for k in range(budget + 1) if root_gains[k] != NEG_INF),
        key=lambda k: root_gains[k],
    )
    tiles = []
    reconstruct(root, -1, best_k, tiles)
    tile_set = set(tiles)

    assignments = []
    for p in points:
        for z in range(p.max_zoom, p.min_zoom - 1, -1):
            t = mercantile.tile(p.lon, p.lat, z, truncate=True)
            if t in tile_set:
                assignments.append((p, z))
                break

    result.update(
        feasible=True,
        tiles=sorted(tiles, key=lambda t: (t.z, t.x, t.y)),
        requests=len(tiles),
        total_clarity=root_gains[best_k],
        assignments=assignments,
    )
    return result


def _load_spec(path):
    with open(path, encoding="utf-8") as f:
        spec = json.load(f)
    points = [
        Point(
            lon=float(item["lon"]),
            lat=float(item["lat"]),
            min_zoom=int(item["min_zoom"]),
            max_zoom=int(item["max_zoom"]),
            weight=float(item.get("weight", 1.0)),
            name=str(item.get("name", "")),
        )
        for item in spec["points"]
    ]
    return points, spec.get("budget")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Plan an optimal tile download list under a request budget."
    )
    parser.add_argument("spec", help="JSON file with budget and points")
    parser.add_argument(
        "--budget", type=int, default=None, help="override the budget in the spec"
    )
    parser.add_argument(
        "--json", action="store_true", help="emit the plan as JSON only"
    )
    args = parser.parse_args(argv)

    points, spec_budget = _load_spec(args.spec)
    budget = args.budget if args.budget is not None else spec_budget
    if budget is None:
        parser.error("budget must be given in the spec or via --budget")

    plan = plan_downloads(points, budget)

    if args.json:
        payload = dict(plan)
        payload["tiles"] = [
            {"z": t.z, "x": t.x, "y": t.y} for t in plan["tiles"]
        ]
        payload["assignments"] = [
            {"name": p.name, "lon": p.lon, "lat": p.lat, "achieved_zoom": z}
            for p, z in plan["assignments"]
        ]
        json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    elif plan["feasible"]:
        print(
            "预算可行：%d/%d 次请求，加权清晰度 %.1f"
            % (plan["requests"], budget, plan["total_clarity"])
        )
        print("下载清单 (z/x/y)：")
        for t in plan["tiles"]:
            print("  %d/%d/%d" % (t.z, t.x, t.y))
        print("各点实际层级：")
        for p, z in plan["assignments"]:
            label = p.name or "(%.5f, %.5f)" % (p.lon, p.lat)
            print(
                "  %s: z%d (要求 z%d..z%d, 权重 %g)"
                % (label, z, p.min_zoom, p.max_zoom, p.weight)
            )
    else:
        print(
            "预算不足：满足全部硬性最低层级至少需要 %d 次请求，"
            "当前预算 %d，最少还差 %d 次。"
            % (plan["min_required"], budget, plan["shortfall"])
        )

    return 0 if plan["feasible"] else 1


if __name__ == "__main__":
    sys.exit(main())
