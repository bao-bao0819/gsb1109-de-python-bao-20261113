"""Exact rational engine for the weighted top-three classification.

All arithmetic uses :class:`fractions.Fraction`, so editorial limits and
relevance decimals are represented exactly (parsed from their string form).
Ties and strict inequalities therefore need no epsilon: a score equality is
exact, and a larger-numbered rival that only reaches an exact tie is correctly
kept below its competitor.

Feasibility of a system mixing strict and non-strict linear inequalities over a
convex polygon uses this fact: a convex set covered by a finite union of
boundary hyperplanes must be contained in one of them.  Hence strict
constraints are feasible together exactly when none of them is identically zero
on the closure of the clipped region.
"""

from fractions import Fraction
from itertools import combinations

TOP_K = 3


def F(value):
    """Exact Fraction from int, float, Fraction or decimal string."""
    if isinstance(value, Fraction):
        return value
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, float):
        return Fraction(str(value))
    return Fraction(str(value))


def _clip(poly, a, b, c):
    """Keep points (x, y) with a*x + b*y <= c; all coefficients are Fractions."""
    if not poly:
        return []
    out = []
    size = len(poly)
    for k in range(size):
        px, py = poly[k]
        qx, qy = poly[(k + 1) % size]
        dp = a * px + b * py - c
        dq = a * qx + b * qy - c
        p_in = dp <= 0
        q_in = dq <= 0
        if p_in:
            out.append((px, py))
        if p_in != q_in:
            denom = a * (qx - px) + b * (qy - py)
            if denom == 0:
                continue
            t = -dp / denom
            out.append((px + t * (qx - px), py + t * (qy - py)))
    return _dedup(out)


def _dedup(poly):
    out = []
    for point in poly:
        if out and point == out[-1]:
            continue
        out.append(point)
    if len(out) > 1 and out[0] == out[-1]:
        out.pop()
    return out


def feasible_polygon(lows, highs):
    """Convex set of admissible (w1, w2) points as exact Fractions."""
    lows = [F(v) for v in lows]
    highs = [F(v) for v in highs]
    if any(not (0 <= lows[k] <= highs[k] <= 1) for k in range(3)):
        raise ValueError("an editorial range is invalid")
    if sum(lows) > 1 or sum(highs) < 1:
        raise ValueError("ranges admit no feasible weights")
    l1, l2, l3 = lows
    u1, u2, u3 = highs
    zero, one = Fraction(0), Fraction(1)
    poly = [(zero, zero), (one, zero), (zero, one)]
    poly = _clip(poly, -one, zero, -l1)
    poly = _clip(poly, one, zero, u1)
    poly = _clip(poly, zero, -one, -l2)
    poly = _clip(poly, zero, one, u2)
    poly = _clip(poly, one, one, one - l3)
    poly = _clip(poly, -one, -one, u3 - one)
    if not poly:
        raise ValueError("ranges admit no feasible weights")
    return poly


def to_weights(point):
    x, y = point
    return (x, y, 1 - x - y)


def _score(relevance, weights):
    return sum(F(r) * w for r, w in zip(relevance, weights))


def rank_table(records, weights):
    """Manuscript numbers ranked by exact score, ties by ascending number."""
    weights = tuple(F(w) for w in weights)
    ordered = sorted(
        records,
        key=lambda rec: (-_score(rec["relevance"], weights), rec["number"]),
    )
    return [rec["number"] for rec in ordered]


def rank_of(records, weights, number):
    return rank_table(records, weights).index(number) + 1


def weighted_score(relevance, weights):
    """Exact weighted score as a Fraction."""
    return _score(relevance, tuple(F(w) for w in weights))


def _difference(rec, rival):
    return tuple(F(rival["relevance"][k]) - F(rec["relevance"][k]) for k in range(3))


def _as_xy(diffs):
    """D(w) = (d1-d3) w1 + (d2-d3) w2 + d3 over the (w1, w2) plane."""
    d1, d2, d3 = diffs
    return d1 - d3, d2 - d3, d3


def _vertex_values(poly, dx, dy, const):
    return [dx * x + dy * y + const for x, y in poly]


def _max_value(poly, dx, dy, const):
    return max(_vertex_values(poly, dx, dy, const))


def _min_value(poly, dx, dy, const):
    return min(_vertex_values(poly, dx, dy, const))


def _can_beat(poly, rec, rival):
    """Whether rival can finish above rec at some admissible weight."""
    dx, dy, const = _as_xy(_difference(rec, rival))
    best = _max_value(poly, dx, dy, const)
    if best > 0:
        return True
    if rival["number"] < rec["number"] and best >= 0:
        return True
    return False


def _closure_region(poly, rec, group):
    """Polygon (possibly a segment/point) where every group member ties or beats."""
    region = poly
    for rival in group:
        dx, dy, const = _as_xy(_difference(rec, rival))
        # D >= 0  <=>  (-dx) x + (-dy) y <= const
        region = _clip(region, -dx, -dy, const)
        if not region:
            return []
    return region


def _group_can_beat(poly, rec, group):
    """Exact feasibility of every group member finishing above rec at once."""
    region = _closure_region(poly, rec, group)
    if not region:
        return False, []
    strict_rivals = [rival for rival in group if rival["number"] > rec["number"]]
    for rival in strict_rivals:
        dx, dy, const = _as_xy(_difference(rec, rival))
        # A larger number must win strictly; closure lying on D == 0 cannot.
        if _max_value(region, dx, dy, const) == 0:
            return False, []
    return True, region


def _centroid(poly):
    return (
        sum(point[0] for point in poly) / len(poly),
        sum(point[1] for point in poly) / len(poly),
    )


def _strict_point(region, forms):
    """Point in region satisfying every D_i < 0 (strict forms only).

    Every vertex of region already obeys D_i <= 0.  For each strict form pick a
    vertex attaining its (negative) minimum and average them: convexity then
    gives D_i(average) <= D_i(chosen vertex) < 0 for every form simultaneously.
    """
    witness_vertices = []
    for dx, dy, const in forms:
        vertex = min(region, key=lambda point: dx * point[0] + dy * point[1] + const)
        witness_vertices.append(vertex)
    return (
        sum((point[0] for point in witness_vertices), Fraction(0)) / len(witness_vertices),
        sum((point[1] for point in witness_vertices), Fraction(0)) / len(witness_vertices),
    )


def _witness(poly, rec, records, force_below):
    """An admissible weight where the kept-down rivals do not finish above rec."""
    if not force_below:
        point = _centroid(poly)
        weights = to_weights(point)
        return weights if rank_of(records, weights, rec["number"]) <= TOP_K else None

    # Closure: every kept-down rival satisfies D <= 0 (does not strictly beat).
    region = poly
    forms = []
    for rival in force_below:
        dx, dy, const = _as_xy(_difference(rec, rival))
        region = _clip(region, dx, dy, -const)
        if not region:
            return None
        forms.append((rival, dx, dy, const))

    # Build a strict interior point so the witness keeps a positive margin and
    # survives conversion to float for display.  A rival whose minimum is exactly
    # zero ties rec everywhere on the region; a smaller-numbered one then wins
    # the tie-break (infeasible), while a larger-numbered one never gets above.
    strict_forms = []
    for rival, dx, dy, const in forms:
        minimum = _min_value(region, dx, dy, const)
        if minimum == 0:
            if rival["number"] < rec["number"]:
                return None
            continue
        strict_forms.append((dx, dy, const))
    point = _strict_point(region, strict_forms) if strict_forms else _centroid(region)
    weights = to_weights(point)
    return weights if rank_of(records, weights, rec["number"]) <= TOP_K else None


def _find_witness(poly, rec, records, candidates):
    """Admissible weight leaving at most two beatable rivals above rec."""
    for exempt_count in range(TOP_K):
        for exempt in combinations(candidates, exempt_count):
            exempt_numbers = {rival["number"] for rival in exempt}
            kept_down = [
                rival for rival in candidates
                if rival["number"] not in exempt_numbers
            ]
            weights = _witness(poly, rec, records, kept_down)
            if weights is not None:
                return weights
    return None


def classify(records, lows, highs):
    """Exact guaranteed / possible / impossible classification with witnesses."""
    _validate(records)
    poly = feasible_polygon(lows, highs)
    results = []
    for rec in sorted(records, key=lambda item: item["number"]):
        candidates = [
            rival for rival in records
            if rival["number"] != rec["number"] and _can_beat(poly, rec, rival)
        ]
        guaranteed = len(candidates) < TOP_K or not any(
            _group_can_beat(poly, rec, group)[0]
            for group in combinations(candidates, TOP_K)
        )
        weights = _find_witness(poly, rec, records, candidates)
        if guaranteed:
            results.append(_entry(rec, "guaranteed", weights, records, candidates))
        elif weights is None:
            results.append(_entry(rec, "impossible", None, records, candidates))
        else:
            results.append(_entry(rec, "possible", weights, records, candidates))
    return results


def _entry(rec, verdict, weights, records, candidates):
    entry = {
        "number": rec["number"],
        "verdict": verdict,
        "rivals_that_can_beat": [rival["number"] for rival in candidates],
        "weights": weights,
        "witness_rank": rank_of(records, weights, rec["number"]) if weights else None,
    }
    return entry


def _validate(records):
    numbers = [rec["number"] for rec in records]
    if len(set(numbers)) != len(numbers):
        raise ValueError("manuscript numbers must be unique")
    if not records:
        raise ValueError("at least one manuscript is required")
    for rec in records:
        if len(rec["relevance"]) != 3:
            raise ValueError(
                "manuscript {} needs exactly three relevance fields".format(rec["number"])
            )
