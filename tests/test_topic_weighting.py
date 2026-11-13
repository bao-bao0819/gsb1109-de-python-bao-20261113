import json
import random

import pytest

from topic_weighting import (
    classify,
    feasible_polygon,
    load_records,
    parse_ranges,
    rank_of,
    rank_table,
)
from topic_weighting import exact_engine as engine
from topic_weighting.analysis import weighted_score
from topic_weighting.cli import main


def make_records(pairs):
    return [
        {"number": number, "relevance": tuple(values)}
        for number, values in pairs
    ]


def _line_from_pair(rec_a, rec_b):
    """Tie line S_a = S_b as A*x + B*y + C = 0 over exact (w1, w2)."""
    ra, rb = rec_a["relevance"], rec_b["relevance"]
    d = tuple(engine.F(ra[k]) - engine.F(rb[k]) for k in range(3))
    return d[0] - d[2], d[1] - d[2], d[2]


def _line_intersection(line_a, line_b):
    a1, b1, c1 = line_a
    a2, b2, c2 = line_b
    det = a1 * b2 - a2 * b1
    if det == 0:
        return None
    return ((b1 * c2 - b2 * c1) / det, (c1 * a2 - c2 * a1) / det)


def _point_in_convex_polygon(point, poly):
    """Exact convex containment, treating boundary points as inside."""
    x, y = point
    if len(poly) == 1:
        return x == poly[0][0] and y == poly[0][1]
    sign = None
    for k in range(len(poly)):
        px, py = poly[k]
        qx, qy = poly[(k + 1) % len(poly)]
        cross = (qx - px) * (y - py) - (qy - py) * (x - px)
        if cross == 0:
            continue
        side = cross > 0
        if sign is None:
            sign = side
        elif sign != side:
            return False
    return True


def arrangement_weights(records, lows, highs):
    """Exact independent oracle enumerating every cell of the tie arrangement.

    Combines polygon vertices, all tie-line intersections (with edges and each
    other) and a fine rational grid, then evaluates exact rankings.  Using
    Fractions removes every boundary-snapping and sign-flip ambiguity.
    """
    poly = engine.feasible_polygon(lows, highs)
    lines = [
        _line_from_pair(records[i], records[j])
        for i in range(len(records)) for j in range(i + 1, len(records))
    ]
    points = list(poly)

    edges = [(poly[k], poly[(k + 1) % len(poly)]) for k in range(len(poly))]
    for line in lines:
        for p, q in edges:
            edge_line = (
                q[1] - p[1], p[0] - q[0],
                (q[0] - p[0]) * p[1] - (q[1] - p[1]) * p[0],
            )
            hit = _line_intersection(line, edge_line)
            if hit is not None and _point_in_convex_polygon(hit, poly):
                points.append(hit)
    for i in range(len(lines)):
        for j in range(i + 1, len(lines)):
            hit = _line_intersection(lines[i], lines[j])
            if hit is not None and _point_in_convex_polygon(hit, poly):
                points.append(hit)

    # Dense rational grid over the polygon bounding box; the simplex/range
    # constraints are re-checked exactly, so only feasible points survive.
    xs = sorted({x for x, _ in poly})
    ys = sorted({y for _, y in poly})
    grid = 60
    if len(xs) >= 2 and len(ys) >= 2:
        x0, x1 = xs[0], xs[-1]
        y0, y1 = ys[0], ys[-1]
        for i in range(grid + 1):
            for j in range(grid + 1):
                x = x0 + (x1 - x0) * i / grid
                y = y0 + (y1 - y0) * j / grid
                if _point_in_convex_polygon((x, y), poly):
                    points.append((x, y))

    weights, seen = [], set()
    lows_f = [engine.F(v) for v in lows]
    highs_f = [engine.F(v) for v in highs]
    for x, y in points:
        vector = (x, y, 1 - x - y)
        if any(vector[k] < lows_f[k] or vector[k] > highs_f[k] for k in range(3)):
            continue
        if vector not in seen:
            seen.add(vector)
            weights.append(vector)
    return weights


def brute_force_ranks(records, lows, highs):
    """Exact oracle: ranks attained over every enumerated arrangement cell."""
    seen = {rec["number"]: set() for rec in records}
    for weights in arrangement_weights(records, lows, highs):
        order = rank_table(records, weights)
        for rank, number in enumerate(order, start=1):
            seen[number].add(rank)
    return seen


def assert_witness_admissible(entry, lows, highs, records):
    if entry["weights"] is None:
        return
    weights = entry["weights"]
    assert len(weights) == 3
    assert all(w >= -1e-12 for w in weights)
    assert sum(weights) == pytest.approx(1.0, abs=1e-9)
    for k in range(3):
        assert lows[k] - 1e-9 <= weights[k] <= highs[k] + 1e-9
    assert rank_of(records, weights, entry["number"]) <= 3


def test_feasible_polygon_full_simplex():
    poly = feasible_polygon((0.0, 0.0, 0.0), (1.0, 1.0, 1.0))
    assert len(poly) == 3


def test_feasible_polygon_single_point():
    lows = highs = (0.5, 0.3, 0.2)
    poly = feasible_polygon(lows, highs)
    assert len(poly) == 1
    assert poly[0][0] == pytest.approx(0.5)
    assert poly[0][1] == pytest.approx(0.3)


def test_infeasible_ranges_rejected():
    with pytest.raises(ValueError):
        feasible_polygon((0.6, 0.6, 0.0), (1.0, 1.0, 1.0))
    with pytest.raises(ValueError):
        feasible_polygon((0.0, 0.0, 0.0), (0.2, 0.2, 0.2))


def test_dominant_manuscript_is_guaranteed():
    records = make_records([
        (1, (9, 9, 9)),
        (2, (8, 1, 1)),
        (3, (1, 8, 1)),
        (4, (1, 1, 8)),
        (5, (0, 0, 0)),
    ])
    lows, highs = parse_ranges(["0,1", "0,1", "0,1"])
    verdicts = classify(records, lows, highs)
    top = {entry["number"]: entry for entry in verdicts}
    assert top[1]["verdict"] == "guaranteed"
    assert_witness_admissible(top[1], lows, highs, records)
    assert top[5]["verdict"] == "impossible"


def test_tie_break_by_manuscript_number():
    records = make_records([
        (2, (5, 5, 5)),
        (5, (5, 5, 5)),
        (8, (1, 1, 1)),
    ])
    lows, highs = parse_ranges(["0.2,0.6", "0.2,0.6", "0.2,0.6"])
    order = rank_table(records, (0.5, 0.3, 0.2))
    assert order == [2, 5, 8]
    verdicts = {entry["number"]: entry for entry in classify(records, lows, highs)}
    assert verdicts[2]["witness_rank"] == 1
    assert verdicts[5]["witness_rank"] == 2
    assert all(entry["verdict"] == "guaranteed" for entry in verdicts.values())


def test_anti_fake_ranking_shared_weights_only():
    # Manuscript 4 only looks viable if its own field were pushed up while each
    # rival's favoured field were pushed down at the same time.  A single shared
    # weight vector cannot do that: the three rivals stay strictly above it at
    # every admissible weight.
    records = make_records([
        (1, (3, 3, 0)),
        (2, (3, 0, 3)),
        (3, (0, 3, 3)),
        (4, (1, 1, 1)),
    ])
    lows, highs = parse_ranges(["0.2,0.5", "0.2,0.5", "0.2,0.5"])
    verdicts = {entry["number"]: entry for entry in classify(records, lows, highs)}
    assert verdicts[4]["verdict"] == "impossible"
    assert verdicts[4]["weights"] is None

    # Direct check: at every admissible weight all three rivals beat #4.
    for weights in arrangement_weights(records, lows, highs):
        target = weighted_score((1, 1, 1), weights)
        beats = sum(
            weighted_score(rec["relevance"], weights) > target
            for rec in records if rec["number"] != 4
        )
        assert beats == 3
        assert rank_of(records, weights, 4) == 4

    # The misleading per-rival "best case" would have promised a top-three place.
    # Rival 1 is weakest when w3 is maximal, etc.; those three conditions cannot
    # hold at once, which is exactly why the shared-weight rule rejects it.
    for number in (1, 2, 3):
        assert verdicts[number]["verdict"] == "guaranteed"
        assert_witness_admissible(verdicts[number], lows, highs, records)


def test_possible_but_not_guaranteed_single_field_manuscript():
    records = make_records([
        (1, (10, 0, 0)),
        (2, (0, 10, 0)),
        (3, (0, 0, 10)),
        (4, (4, 4, 4)),
    ])
    lows, highs = parse_ranges(["0,1", "0,1", "0,1"])
    verdicts = {entry["number"]: entry for entry in classify(records, lows, highs)}
    assert verdicts[1]["verdict"] == "possible"
    assert_witness_admissible(verdicts[1], lows, highs, records)
    assert verdicts[4]["verdict"] == "guaranteed"


@pytest.mark.parametrize("seed", range(12))
@pytest.mark.parametrize(
    "range_spec",
    [
        ["0,1", "0,1", "0,1"],
        ["0.2,0.6", "0.2,0.6", "0.2,0.6"],
        ["0.1,0.8", "0.0,0.5", "0.1,0.4"],
        ["0.34,0.34", "0.33,0.33", "0.33,0.33"],
    ],
)
def test_matches_brute_force_grid(seed, range_spec):
    rng = random.Random(seed)
    lows, highs = parse_ranges(range_spec)
    n = rng.randint(4, 8)
    records = [
        {
            "number": index + 1,
            "relevance": tuple(rng.randint(0, 10) for _ in range(3)),
        }
        for index in range(n)
    ]
    verdicts = classify(records, lows, highs)
    seen_ranks = brute_force_ranks(records, lows, highs)

    for entry in verdicts:
        number = entry["number"]
        best_possible = min(seen_ranks[number])
        worst = max(seen_ranks[number])
        if entry["verdict"] == "guaranteed":
            assert worst <= 3
        elif entry["verdict"] == "impossible":
            assert best_possible > 3
        else:
            assert best_possible <= 3 < worst
        assert_witness_admissible(entry, lows, highs, records)


def test_csv_and_json_loaders(tmp_path):
    csv_path = tmp_path / "export.csv"
    csv_path.write_text(
        "number,title_score,keyword_score,body_score\n"
        "1,9,1,2\n2,3,8,0\n3,4,4,4\n",
        encoding="utf-8",
    )
    records, header = load_records(str(csv_path))
    assert records[0] == {"number": 1, "relevance": (9.0, 1.0, 2.0)}
    assert header[1] == "title_score"

    json_path = tmp_path / "export.json"
    json_path.write_text(json.dumps([
        {"number": 1, "relevance": [9, 1, 2]},
        {"number": 2, "relevance": [3, 8, 0]},
    ]), encoding="utf-8")
    records, _ = load_records(str(json_path))
    assert records[1]["number"] == 2


def test_cli_text_and_json_output(tmp_path, capsys):
    csv_path = tmp_path / "export.csv"
    csv_path.write_text(
        "number,f1,f2,f3\n"
        "1,3,3,0\n2,3,0,3\n3,0,3,3\n4,1,1,1\n",
        encoding="utf-8",
    )
    range_path = tmp_path / "ranges.csv"
    range_path.write_text(
        "field,low,high\n1,0.2,0.5\n2,0.2,0.5\n3,0.2,0.5\n",
        encoding="utf-8",
    )
    main([str(csv_path), "--ranges", str(range_path)])
    text = capsys.readouterr().out
    assert "稳进前三" in text
    assert "无缘前三" in text

    main([str(csv_path), "--ranges", str(range_path), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["top_k"] == 3
    numbers = {entry["number"]: entry for entry in payload["verdicts"]}
    assert numbers[4]["verdict"] == "impossible"

    main([
        str(csv_path), "--ranges", str(range_path),
        "--rank-with", "0.4,0.3,0.3", "--json",
    ])
    payload = json.loads(capsys.readouterr().out)
    assert [row["rank"] for row in payload["ranking"]] == [1, 2, 3, 4]
    assert len(payload["ranking"]) == 4
