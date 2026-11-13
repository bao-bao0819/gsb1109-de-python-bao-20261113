# -*- coding: utf-8 -*-
"""演示: 2026-11 月中途从旧巡检班表切换到新班表.

场景设定
- 账期: 2026-11-01 00:00 ~ 2026-12-01 00:00
- 生效时刻: 2026-11-15 12:00 (只迁这之后的费用, 原始服务段不变)
- 旧班表: 早班 06:00-14:00 / 中班 14:00-22:00 / 夜班 22:00-06:00(跨午夜)
- 新班表: 甲班 08:00-20:00 / 乙班 20:00-08:00(跨午夜)
          / 机动班 19:00-21:00 —— 与甲、乙两班各重叠一小时,
          重叠区(19:00-20:00, 20:00-21:00)裁定前费用一律挂账

运行: python -m billing_migration.demo
"""

from datetime import datetime

from .model import ServiceSegment, ShiftTemplate
from .transfer import (
    PENDING,
    RETAINED,
    build_transfer_matrix,
    old_ledger_totals,
    resettle_under_new_schedule,
)

OLD_SHIFTS = [
    ShiftTemplate("早班", datetime.strptime("06:00", "%H:%M").time(),
                  datetime.strptime("14:00", "%H:%M").time()),
    ShiftTemplate("中班", datetime.strptime("14:00", "%H:%M").time(),
                  datetime.strptime("22:00", "%H:%M").time()),
    ShiftTemplate("夜班", datetime.strptime("22:00", "%H:%M").time(),
                  datetime.strptime("06:00", "%H:%M").time()),
]

NEW_SHIFTS = [
    ShiftTemplate("甲班", datetime.strptime("08:00", "%H:%M").time(),
                  datetime.strptime("20:00", "%H:%M").time()),
    ShiftTemplate("乙班", datetime.strptime("20:00", "%H:%M").time(),
                  datetime.strptime("08:00", "%H:%M").time()),
    ShiftTemplate("机动班", datetime.strptime("19:00", "%H:%M").time(),
                  datetime.strptime("21:00", "%H:%M").time()),
]

EFFECTIVE_AT = datetime(2026, 11, 15, 12, 0, 0)


def _dt(text):
    return datetime.strptime(text, "%Y-%m-%d %H:%M")


def demo_segments():
    """构造覆盖各边界情形的原始服务段 (金额故意除不尽, 考验精确性)."""
    raw = [
        # 完全在生效时刻之前 -> 全部留存旧账
        ("S01", "2026-11-03 07:00", "2026-11-03 13:00", "240.00"),
        # 跨过生效时刻的早班段 -> 前段留存, 后段迁甲班
        ("S02", "2026-11-15 08:00", "2026-11-15 14:00", "173.29"),
        # 生效后普通早班段 -> 全迁甲班
        ("S03", "2026-11-16 09:30", "2026-11-16 13:45", "88.10"),
        # 中班段, 尾部落入 19:00-20:00 重叠待定区 -> 部分挂账
        ("S04", "2026-11-17 15:00", "2026-11-17 21:30", "390.00"),
        # 跨午夜夜班段, 同时跨过 20:00-21:00 待定区 -> 乙班+挂账
        ("S05", "2026-11-18 19:30", "2026-11-19 02:30", "511.11"),
        # 长段横跨甲/乙/机动多班与两个待定区
        ("S06", "2026-11-20 06:30", "2026-11-21 01:30", "1000.00"),
        # 恰好从生效时刻开始 -> 全部迁移, 不留存
        ("S07", "2026-11-15 12:00", "2026-11-15 16:00", "66.67"),
        # 生效前最后一班夜班(跨午夜到 15 日早上) -> 全部留存
        ("S08", "2026-11-14 22:00", "2026-11-15 06:00", "320.00"),
        # 生效后跨午夜乙班段, 不碰待定区
        ("S09", "2026-11-22 23:00", "2026-11-23 05:00", "199.99"),
        # 完全落在 19:00-21:00 重叠待定区内 -> 全额挂账
        ("S10", "2026-11-24 19:00", "2026-11-24 21:00", "77.70"),
        # 月末中班段 -> 甲班 + 待定区挂账
        ("S11", "2026-11-28 14:00", "2026-11-28 20:30", "456.78"),
        # 生效前中班段 -> 全部留存
        ("S12", "2026-11-10 14:00", "2026-11-10 20:00", "150.00"),
    ]
    return [
        ServiceSegment(seg_id, _dt(begin), _dt(end), amount)
        for seg_id, begin, end, amount in raw
    ]


def reconcile_report(matrix, segments):
    """两面对账: 旧账还原 + 新账整段重结对齐, 全部精确比较."""
    lines = []
    ledger = old_ledger_totals(segments, OLD_SHIFTS)
    lines.append("[旧账还原] 行合计(含留存) == 旧账各班总额:")
    for old in sorted(ledger):
        row_total = matrix.row_total(old)
        ok = row_total == ledger[old]
        lines.append(
            "  %s: 矩阵行合计 %s == 旧账 %s -> %s"
            % (old, _yuan(row_total), _yuan(ledger[old]), "OK" if ok else "MISMATCH")
        )

    resettled = resettle_under_new_schedule(segments, NEW_SHIFTS, EFFECTIVE_AT)
    lines.append("[新账对齐] 列合计 == 整段重新结算:")
    for col in matrix.columns:
        if col == RETAINED:
            continue
        col_total = matrix.column_total(col)
        other = resettled.get(col, 0)
        ok = col_total == other
        lines.append(
            "  %s: 矩阵列合计 %s == 重结算 %s -> %s"
            % (col, _yuan(col_total), _yuan(other), "OK" if ok else "MISMATCH")
        )

    grand = matrix.grand_total()
    original = sum(seg.amount for seg in segments)
    lines.append(
        "[总量守恒] 矩阵总额 %s == 原始服务段总额 %s -> %s"
        % (grand, original, "OK" if grand == original else "MISMATCH")
    )
    migrated = grand - matrix.column_total(RETAINED)
    lines.append(
        "[迁移规模] 迁出总额(含挂账) %s; 其中挂账 %s 待裁定"
        % (_yuan(migrated), _yuan(matrix.column_total(PENDING)))
    )
    return "\n".join(lines)


def _yuan(value):
    from decimal import Decimal, localcontext

    with localcontext() as ctx:
        ctx.prec = 40
        dec = Decimal(value.numerator) / Decimal(value.denominator)
    return "%.6f" % dec


def main():
    segments = demo_segments()
    matrix = build_transfer_matrix(segments, OLD_SHIFTS, NEW_SHIFTS, EFFECTIVE_AT)
    print("== 费用转移矩阵 (旧班 -> 新班, 生效时刻 %s) ==" % EFFECTIVE_AT)
    print("(显示值保留 4 位小数仅供阅读, 内部为精确分数, 尾数未舍去的标 …)")
    print(matrix.format(places=4))
    print()
    print(reconcile_report(matrix, segments))


if __name__ == "__main__":
    main()
