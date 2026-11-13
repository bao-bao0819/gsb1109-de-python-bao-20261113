import itertools
import os
import random
import sys

import mercantile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from download_plan import Point, plan_downloads


def brute_force(points, budget):
    """Exhaustive optimum over candidate tiles, used to cross-check the DP."""
    candidates = set()
    for p in points:
        for z in range(p.min_zoom, p.max_zoom + 1):
            candidates.add(mercantile.tile(p.lon, p.lat, z, truncate=True))
    candidates = sorted(candidates)
    best = None
    for size in range(0, min(budget, len(candidates)) + 1):
        for subset in itertools.combinations(candidates, size):
            total = 0.0
            for p in points:
                covering = -1
                for z in range(p.min_zoom, p.max_zoom + 1):
                    t = mercantile.tile(p.lon, p.lat, z, truncate=True)
                    if t in subset:
                        covering = z
                if covering < 0:
                    break
                total += p.weight * covering
            else:
                if best is None or total > best:
                    best = total
    return best


def test_single_point_uses_one_request_at_desired_zoom():
    points = [Point(lon=103.61, lat=30.99, min_zoom=10, max_zoom=13, weight=2.0)]
    plan = plan_downloads(points, budget=12)
    assert plan["feasible"]
    assert plan["requests"] == 1
    assert plan["tiles"] == [mercantile.tile(103.61, 30.99, 13, truncate=True)]
    assert plan["total_clarity"] == 26.0


def test_shared_tile_counts_one_request():
    points = [
        Point(lon=103.610, lat=30.990, min_zoom=10, max_zoom=10, weight=1.0),
        Point(lon=103.612, lat=30.992, min_zoom=10, max_zoom=10, weight=1.0),
    ]
    plan = plan_downloads(points, budget=12)
    assert plan["feasible"]
    assert plan["requests"] == 1
    assert plan["total_clarity"] == 20.0


def test_parent_and_child_do_not_double_count():
    parent_point = Point(lon=103.61, lat=30.99, min_zoom=8, max_zoom=8, weight=1.0)
    child_point = Point(lon=103.61, lat=30.99, min_zoom=12, max_zoom=12, weight=5.0)
    plan = plan_downloads([parent_point, child_point], budget=12)
    assert plan["feasible"]
    assert plan["requests"] == 2
    # child point scores only at the deepest covering tile, not at both
    assert plan["total_clarity"] == 1.0 * 8 + 5.0 * 12


def test_budget_shortfall_reports_minimum_gap():
    points = [
        Point(lon=103.61, lat=30.99, min_zoom=12, max_zoom=12),
        Point(lon=100.30, lat=29.04, min_zoom=12, max_zoom=12),
        Point(lon=116.40, lat=39.90, min_zoom=12, max_zoom=12),
    ]
    plan = plan_downloads(points, budget=1)
    assert not plan["feasible"]
    assert plan["min_required"] == 3
    assert plan["shortfall"] == 2
    assert plan["tiles"] == []


def test_zero_budget_shortfall():
    points = [Point(lon=103.61, lat=30.99, min_zoom=5, max_zoom=8)]
    plan = plan_downloads(points, budget=0)
    assert not plan["feasible"]
    assert plan["min_required"] == 1
    assert plan["shortfall"] == 1


def test_matches_brute_force_on_random_instances():
    rng = random.Random(20261113)
    for _ in range(30):
        n = rng.randint(1, 5)
        points = []
        for _ in range(n):
            min_zoom = rng.randint(6, 9)
            points.append(
                Point(
                    lon=rng.uniform(100.0, 105.0),
                    lat=rng.uniform(28.0, 32.0),
                    min_zoom=min_zoom,
                    max_zoom=min_zoom + rng.randint(0, 3),
                    weight=rng.randint(1, 4),
                )
            )
        budget = rng.randint(1, 8)
        plan = plan_downloads(points, budget)
        optimum = brute_force(points, budget)
        if optimum is None:
            assert not plan["feasible"]
            assert plan["shortfall"] == plan["min_required"] - budget
        else:
            assert plan["feasible"]
            assert plan["requests"] <= budget
            assert abs(plan["total_clarity"] - optimum) < 1e-9
