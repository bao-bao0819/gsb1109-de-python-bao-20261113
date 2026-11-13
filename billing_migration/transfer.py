# -*- coding: utf-8 -*-
"""旧账 -> 新账的费用转移矩阵.

口径约定:
- 每个原始服务段金额不变, 只把生效时刻 effective_at 之后的部分按
  段内均匀费率切出并迁移; 生效时刻之前的部分计入 RETAINED 列, 留在旧账.
- 新班表实例化后, 同一时刻被且仅被一个班次覆盖 -> 记入该班次;
  被两个及以上班次覆盖 (重叠待定区) 或没有任何班次覆盖 (空档) -> 记入
  PENDING 挂账列, 待裁定后再分派, 不重复也不遗漏.
- 所有金额都是 Fraction, 矩阵加总与两面对账全程不抹小数.

对账设计:
- build_transfer_matrix 用"边界切片 + 区间树点查"切分迁移窗口;
- resettle_under_new_schedule 用独立的"事件扫描线"按整段重新结算;
  两者算法路径不同, 列合计相等即证明新账与整段重结对齐.
"""

from collections import defaultdict
from fractions import Fraction
from itertools import groupby

from intervaltree import IntervalTree

from .model import exact_seconds, instantiate_range

PENDING = "挂账(待定区)"
RETAINED = "留存(生效前)"


def _build_tree(instances):
    tree = IntervalTree()
    for inst in instances:
        tree.addi(inst.begin, inst.end, inst.name)
    return tree


def _target_for(names):
    """覆盖同一时刻的班次集合 -> 入账科目."""
    return names[0] if len(names) == 1 else PENDING


def _coverage_durations_slice(tree, begin, end):
    """边界切片法: 用树内所有区间端点把 [begin, end) 切成原子段,
    每个原子段按其覆盖班次归集, 返回 {科目: 精确秒数}."""
    overlaps = tree.overlap(begin, end)
    bounds = {begin, end}
    for iv in overlaps:
        bounds.add(max(iv.begin, begin))
        bounds.add(min(iv.end, end))
    points = sorted(bounds)
    durations = defaultdict(Fraction)
    for piece_begin, piece_end in zip(points, points[1:]):
        if piece_begin >= piece_end:
            continue
        # 原子段内没有区间端点, 凡与它相交的班次实例必完整覆盖它
        names = sorted(
            iv.data
            for iv in overlaps
            if iv.begin < piece_end and iv.end > piece_begin
        )
        durations[_target_for(names)] += exact_seconds(piece_end - piece_begin)
    return durations


def _coverage_durations_sweep(instances, begin, end):
    """事件扫描线法 (独立实现, 用于对账): 班次起止作为 +/- 事件,
    扫描过程中按当前活跃班次集合归集, 返回 {科目: 精确秒数}."""
    events = []
    for inst in instances:
        clip_begin = max(inst.begin, begin)
        clip_end = min(inst.end, end)
        if clip_begin < clip_end:
            events.append((clip_begin, 1, inst.name))
            events.append((clip_end, -1, inst.name))
    events.sort(key=lambda ev: (ev[0], ev[1]))
    durations = defaultdict(Fraction)
    active = set()
    prev = None
    for moment, group in groupby(events, key=lambda ev: ev[0]):
        if prev is not None and prev < moment:
            durations[_target_for(sorted(active))] += exact_seconds(moment - prev)
        for _, delta, name in group:
            if delta == 1:
                active.add(name)
            else:
                active.discard(name)
        prev = moment
    return durations


class TransferMatrix:
    """旧班(行) -> 新班(列) 的精确金额矩阵.

    行: 旧班表班次; 列: 新班表班次 + PENDING + RETAINED.
    """

    def __init__(self, rows, new_shift_names):
        # rows: {旧班名: {列名: Fraction}}
        self._rows = {old: dict(cols) for old, cols in rows.items()}
        self._new_shift_names = list(new_shift_names)

    @property
    def old_shift_names(self):
        return list(self._rows)

    @property
    def columns(self):
        cols = list(self._new_shift_names)
        for extra in (PENDING, RETAINED):
            if any(extra in cols_row for cols_row in self._rows.values()):
                cols.append(extra)
        return cols

    def cell(self, old_shift, column):
        return self._rows.get(old_shift, {}).get(column, Fraction(0))

    def row_total(self, old_shift):
        """该旧班整行合计 (含留存), 应等于旧账该班总额."""
        return sum(self._rows.get(old_shift, {}).values(), Fraction(0))

    def migrated_row_total(self, old_shift):
        """该旧班实际迁出合计 (不含留存)."""
        return self.row_total(old_shift) - self.cell(old_shift, RETAINED)

    def column_total(self, column):
        return sum(
            cols.get(column, Fraction(0)) for cols in self._rows.values()
        )

    def grand_total(self):
        return sum(self.row_total(old) for old in self._rows)

    def format(self, places=4):
        """对齐打印; 显示值经四舍五入仅用于阅读, 内部仍为精确 Fraction."""
        quant = Fraction(1, 10**places)

        def fmt(value):
            rounded = round(value / quant) * quant
            text = _fraction_to_decimal_text(rounded, places)
            return text if value == rounded else text + "…"

        columns = self.columns
        header = ["旧班\\新班"] + columns + ["行合计"]
        lines = ["\t".join(header)]
        for old in self.old_shift_names:
            cells = [fmt(self.cell(old, col)) for col in columns]
            lines.append("\t".join([old] + cells + [fmt(self.row_total(old))]))
        totals = [fmt(self.column_total(col)) for col in columns]
        lines.append("\t".join(["列合计"] + totals + [fmt(self.grand_total())]))
        return "\n".join(lines)


def _fraction_to_decimal_text(value, places):
    from decimal import Decimal, localcontext

    with localcontext() as ctx:
        ctx.prec = max(28, places + 20)
        dec = Decimal(value.numerator) / Decimal(value.denominator)
    return "%.*f" % (places, dec)


def old_ledger_totals(segments, old_templates):
    """旧账口径: 按服务段开始时刻归入旧班次, 汇总每班全额."""
    if not segments:
        return {}
    begin = min(seg.begin for seg in segments)
    end = max(seg.end for seg in segments)
    old_tree = _build_tree(instantiate_range(old_templates, begin, end))
    totals = defaultdict(Fraction)
    for seg in segments:
        hits = old_tree.at(seg.begin)
        if len(hits) != 1:
            raise ValueError(
                "旧班表在 %s 处不唯一覆盖 (%d 个班次), 无法入账"
                % (seg.begin, len(hits))
            )
        totals[next(iter(hits)).data] += seg.amount
    return dict(totals)


def build_transfer_matrix(segments, old_templates, new_templates, effective_at):
    """产出旧班 -> 新班的费用转移矩阵 (只迁 effective_at 之后的部分)."""
    if not segments:
        return TransferMatrix({}, [t.name for t in new_templates])
    begin = min(min(seg.begin for seg in segments), effective_at)
    end = max(max(seg.end for seg in segments), effective_at)
    old_tree = _build_tree(instantiate_range(old_templates, begin, end))
    new_tree = _build_tree(instantiate_range(new_templates, begin, end))

    rows = defaultdict(lambda: defaultdict(Fraction))
    for seg in segments:
        hits = old_tree.at(seg.begin)
        if len(hits) != 1:
            raise ValueError(
                "旧班表在 %s 处不唯一覆盖 (%d 个班次), 服务段 %r 无法入账"
                % (seg.begin, len(hits), seg.seg_id)
            )
        old_shift = next(iter(hits)).data
        rate = seg.rate

        retained_end = min(seg.end, effective_at)
        if seg.begin < retained_end:
            rows[old_shift][RETAINED] += rate * exact_seconds(
                retained_end - seg.begin
            )

        window_begin = max(seg.begin, effective_at)
        if window_begin < seg.end:
            for target, seconds in _coverage_durations_slice(
                new_tree, window_begin, seg.end
            ).items():
                rows[old_shift][target] += rate * seconds

    return TransferMatrix(rows, [t.name for t in new_templates])


def resettle_under_new_schedule(segments, new_templates, effective_at):
    """新账口径的独立重算: 对每个原始服务段, 取生效时刻之后的整段,
    用事件扫描线按新班表重新结算, 返回 {科目: 精确金额}."""
    if not segments:
        return {}
    begin = min(min(seg.begin for seg in segments), effective_at)
    end = max(max(seg.end for seg in segments), effective_at)
    instances = instantiate_range(new_templates, begin, end)
    totals = defaultdict(Fraction)
    for seg in segments:
        window_begin = max(seg.begin, effective_at)
        if window_begin >= seg.end:
            continue
        for target, seconds in _coverage_durations_sweep(
            instances, window_begin, seg.end
        ).items():
            totals[target] += seg.rate * seconds
    return dict(totals)
