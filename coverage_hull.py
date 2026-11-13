"""多基站覆盖采样点的凸包分析.

每个基站(天线)上报一组覆盖采样点, 怀疑至多一支天线整组坐标有误.
按 "不剔除" 与 "只剔除一个来源" 两类假设分别求凸包:

- 可能覆盖 = 各假设凸包的并. 剔除来源只会缩小凸包, 故并集即全部点的凸包.
- 一定覆盖 = 所有假设凸包的交(凸多边形半平面裁剪).

不同天线可能上报完全相同的角点: 剔除一支天线时按来源取点集并集,
只要其它天线也上报了该点, 这个角点仍然保留, 不会随剔除被删掉.

对争议位置的判定附一组实际的来源取舍(单一假设)作为证据,
不拼接来自不同假设的证据.
"""

from __future__ import annotations

from pygeoif.functions import convex_hull
from pygeoif.functions import signed_area

Point = tuple[float, float]

SAMPLES: dict[str, list[Point]] = {
    "A": [(0, 0), (4, 0), (4, 4), (0, 4)],
    "B": [(2, 1), (6, 1), (6, 5), (2, 5)],
    "C": [(1, 2), (5, 2), (6, 5), (3, 6)],
}

QUERIES: list[Point] = [(3.5, 3.0), (0.5, 0.5), (10.0, 10.0)]

EPS = 1e-9


def cross(origin: Point, tip: Point, p: Point) -> float:
    return (tip[0] - origin[0]) * (p[1] - origin[1]) - (tip[1] - origin[1]) * (
        p[0] - origin[0]
    )


def close_enough(p: Point, q: Point) -> bool:
    return abs(p[0] - q[0]) <= EPS and abs(p[1] - q[1]) <= EPS


def dedupe_ring(ring: list[Point]) -> list[Point]:
    pts = [p for i, p in enumerate(ring) if i == 0 or not close_enough(p, ring[i - 1])]
    while len(pts) > 1 and close_enough(pts[0], pts[-1]):
        pts.pop()
    return pts


def as_ccw(ring: list[Point]) -> list[Point]:
    pts = dedupe_ring(list(ring))
    if signed_area(pts) < 0:
        pts.reverse()
    return pts


def clip_convex(subject: list[Point], clip_ring: list[Point]) -> list[Point]:
    """Sutherland-Hodgman: 用逆时针凸多边形 clip_ring 裁剪凸多边形 subject."""
    output = list(subject)
    for i, a in enumerate(clip_ring):
        b = clip_ring[(i + 1) % len(clip_ring)]
        source, output = output, []
        if not source:
            break
        prev = source[-1]
        prev_in = cross(a, b, prev) >= -EPS
        for cur in source:
            cur_in = cross(a, b, cur) >= -EPS
            if cur_in != prev_in:
                fa = cross(a, b, prev)
                fb = cross(a, b, cur)
                t = fa / (fa - fb)
                output.append(
                    (prev[0] + t * (cur[0] - prev[0]), prev[1] + t * (cur[1] - prev[1]))
                )
            if cur_in:
                output.append(cur)
            prev, prev_in = cur, cur_in
    return output


def contains(ring: list[Point], p: Point) -> bool:
    if len(ring) < 3:
        return False
    n = len(ring)
    return all(cross(ring[i], ring[(i + 1) % n], p) >= -EPS for i in range(n))


def scenario_hulls(
    samples: dict[str, list[Point]],
) -> list[tuple[str, tuple[str, ...], list[Point]]]:
    """返回 (假设名称, 该假设采用的来源集合, 凸包顶点) 列表."""
    names = sorted(samples)
    scenarios = [("不剔除", tuple(names))]
    scenarios += [
        (f"剔除 {name}", tuple(n for n in names if n != name)) for name in names
    ]
    results = []
    for label, kept in scenarios:
        pts = {p for name in kept for p in samples[name]}
        hull = convex_hull(pts)
        results.append((label, kept, as_ccw(hull) if len(hull) >= 3 else list(hull)))
    return results


def fmt(ring: list[Point]) -> str:
    if not ring:
        return "(空)"
    return " -> ".join(f"({x:g}, {y:g})" for x, y in ring)


def classify(
    q: Point,
    scenarios: list[tuple[str, tuple[str, ...], list[Point]]],
    certain: list[Point],
) -> str:
    if contains(certain, q):
        return "一定覆盖(不剔除或剔除任意一个来源, 所有假设的凸包都包含该点)"
    for label, kept, ring in scenarios:
        if contains(ring, q):
            return (
                f"可能覆盖; 证据取自单一假设[{label}], "
                f"实际采用来源: {', '.join(kept)}"
            )
    return "无法覆盖(没有任何单一假设的凸包包含该点)"


def main() -> None:
    scenarios = scenario_hulls(SAMPLES)

    shared: dict[Point, list[str]] = {}
    for name, pts in SAMPLES.items():
        for p in pts:
            shared.setdefault(p, []).append(name)
    for p, names in sorted(shared.items()):
        if len(names) > 1:
            kept_by = {
                label: p in ring for label, _kept, ring in scenarios if len(ring) >= 3
            }
            print(f"共享角点 {p} 由 {', '.join(names)} 上报; 是否仍为各假设凸包顶点: {kept_by}")

    print("\n各假设下的覆盖凸包:")
    for label, kept, ring in scenarios:
        print(f"  [{label}] 来源={kept}: {fmt(ring)}")

    possible = next(ring for label, _k, ring in scenarios if label == "不剔除")
    certain: list[Point] = list(possible)
    for _label, _kept, ring in scenarios:
        certain = clip_convex(certain, ring) if len(ring) >= 3 else []

    print(f"\n可能覆盖边界(任一假设成立即可): {fmt(possible)}")
    print(f"一定覆盖边界(所有假设共同覆盖): {fmt(dedupe_ring(certain))}")

    print("\n争议位置判定:")
    for q in QUERIES:
        print(f"  {q}: {classify(q, scenarios, certain)}")

    assert contains(certain, (3.5, 3.0))
    assert not contains(certain, (0.5, 0.5)) and contains(possible, (0.5, 0.5))
    assert not contains(possible, (10.0, 10.0))


if __name__ == "__main__":
    main()
