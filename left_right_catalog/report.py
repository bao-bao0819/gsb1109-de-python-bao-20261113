"""Render a Chinese verification report for the left/right catalog."""

from fractions import Fraction

from left_right_catalog.core import Catalog
from left_right_catalog.core import OrientationFix
from left_right_catalog.core import Parcel
from left_right_catalog.core import Point
from left_right_catalog.core import RawPoint
from left_right_catalog.core import build_catalog
from left_right_catalog.core import flip_rings
from left_right_catalog.core import normalize_parcels

SIDE_NAMES = {"left": "左", "right": "右"}
ORIENTATION_NAMES = {"CCW": "逆时针", "CW": "顺时针"}


def _to_point(raw: RawPoint) -> Point:
    x, y = raw
    return (Fraction(x), Fraction(y))


def _fmt_num(value: Fraction) -> str:
    if value.denominator == 1:
        return str(value.numerator)
    return str(float(value))


def _fmt_point(point: Point) -> str:
    return f"({_fmt_num(point[0])},{_fmt_num(point[1])})"


def _fmt_segment(start: Point, end: Point) -> str:
    return f"{_fmt_point(start)}->{_fmt_point(end)}"


def _fmt_parcels(parcels: tuple[str, ...] | frozenset[str]) -> str:
    return "、".join(sorted(parcels))


def _orientation_section(normalized_fixes: tuple[OrientationFix, ...]) -> list[str]:
    lines = [
        "[1] 朝向规范化（约定：外环逆时针，洞顺时针，二者方向相反）",
    ]
    if normalized_fixes:
        lines.extend(
            f"    {fix.parcel_id}/{fix.ring_name}："
            f"原为{ORIENTATION_NAMES[fix.was]}，已翻转"
            for fix in normalized_fixes
        )
    else:
        lines.append("    所有环已符合约定，无需翻转。")
    return lines


def _shared_section(catalog: Catalog) -> list[str]:
    lines = ["[2] 共享边走向核对（相邻宗地应沿共同边反向行走）"]
    shared = catalog.shared_boundaries()
    lines.extend(
        f"    {_fmt_segment(seg.start, seg.end)}："
        f"左={_fmt_parcels(seg.left)}，右={_fmt_parcels(seg.right)} ✓"
        for seg in shared
    )
    lines.append(f"    共 {len(shared)} 段共享边，两侧各一宗地，走向相反。")
    boundary = catalog.boundary_segments()
    lines.append(
        f"    另有 {len(boundary)} 段仅一侧有宗地的边（外边界或洞的边界）。",
    )
    return lines


def _insertion_section(catalog: Catalog) -> list[str]:
    lines = ["[3] 共线顶点吸收（一侧多插顶点不应报不匹配）"]
    if catalog.insertions:
        lines.extend(
            f"    宗地 {ins.vertex_of} 的顶点 {_fmt_point(ins.point)} "
            f"落在宗地 {ins.edge_of} 的边上：已切分吸收，未报不匹配。"
            for ins in catalog.insertions
        )
    else:
        lines.append("    未发现跨宗地的共线顶点。")
    return lines


def _conflict_section(catalog: Catalog) -> list[str]:
    lines = ["[4] 同侧冲突核对（同一侧被多宗地同时主张）"]
    conflicts = catalog.conflicts(min_claimants=2)
    if conflicts:
        lines.extend(
            f"    段 {_fmt_segment(c.start, c.end)} "
            f"{SIDE_NAMES[c.side]}侧被 {len(c.parcels)} 宗地同时主张："
            f"{_fmt_parcels(c.parcels)}"
            for c in conflicts
        )
    else:
        lines.append("    未发现同侧冲突。")
    shortest = catalog.shortest_conflict(min_claimants=3)
    if shortest is not None:
        lines.append(
            f"    ★ 三方及以上冲突的最短边段："
            f"{_fmt_segment(shortest.start, shortest.end)}，"
            f"{SIDE_NAMES[shortest.side]}侧，"
            f"宗地 {_fmt_parcels(shortest.parcels)}，"
            f"长度 {shortest.length:g}",
        )
    return lines


def _query_section(
    catalog: Catalog,
    queries: tuple[tuple[RawPoint, RawPoint], ...],
) -> list[str]:
    lines = ["[5] 左右归属查询示例（沿边走时左、右宗地号可直接查）"]
    for a, b in queries:
        claim = catalog.query(a, b)
        left = _fmt_parcels(claim.left) if claim.left else "（无）"
        right = _fmt_parcels(claim.right) if claim.right else "（无）"
        lines.append(
            f"    沿 {_fmt_segment(_to_point(a), _to_point(b))} 走："
            f"左={left}，右={right}",
        )
    return lines


def _flip_section(parcels: tuple[Parcel, ...], catalog: Catalog) -> list[str]:
    flipped = build_catalog(normalize_parcels(flip_rings(parcels)))
    consistent = flipped.signature() == catalog.signature()
    verdict = "左右归属完全一致（以约定朝向为准）" if consistent else "左右归属不一致！"
    return [
        "[6] 整体翻转不变性",
        f"    翻转全部输入环后重建目录：{verdict}",
    ]


def format_report(
    parcels: tuple[Parcel, ...],
    queries: tuple[tuple[RawPoint, RawPoint], ...] = (),
) -> str:
    """Build the catalog for ``parcels`` and render the full report."""
    normalized = normalize_parcels(parcels)
    catalog = build_catalog(normalized)
    sections = [
        "宗地线面目录核对报告",
        "=" * 30,
        *_orientation_section(normalized.fixes),
        *_shared_section(catalog),
        *_insertion_section(catalog),
        *_conflict_section(catalog),
    ]
    if queries:
        sections.extend(_query_section(catalog, queries))
    sections.extend(_flip_section(parcels, catalog))
    return "\n".join(sections)
