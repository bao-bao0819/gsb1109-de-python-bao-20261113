# -*- coding: utf-8 -*-
"""费用转移矩阵的不变量测试: 两面对账、精确性、边界情形."""

from datetime import datetime, timedelta
from fractions import Fraction

import pytest

from billing_migration.demo import (
    EFFECTIVE_AT,
    NEW_SHIFTS,
    OLD_SHIFTS,
    demo_segments,
)
from billing_migration.model import ServiceSegment, exact_seconds
from billing_migration.transfer import (
    PENDING,
    RETAINED,
    build_transfer_matrix,
    old_ledger_totals,
    resettle_under_new_schedule,
)


@pytest.fixture(scope="module")
def segments():
    return demo_segments()


@pytest.fixture(scope="module")
def matrix(segments):
    return build_transfer_matrix(segments, OLD_SHIFTS, NEW_SHIFTS, EFFECTIVE_AT)


def test_old_view_restores_ledger(matrix, segments):
    """按原班次加总(含留存)必须精确还原旧账, 不允许各自抹小数."""
    ledger = old_ledger_totals(segments, OLD_SHIFTS)
    assert set(matrix.old_shift_names) == set(ledger)
    for old_shift, total in ledger.items():
        assert matrix.row_total(old_shift) == total
        assert isinstance(matrix.row_total(old_shift), Fraction)


def test_new_view_matches_resettlement(matrix, segments):
    """按新班次看的列合计必须与整段重新结算(独立扫描线算法)精确对齐."""
    resettled = resettle_under_new_schedule(segments, NEW_SHIFTS, EFFECTIVE_AT)
    for col in matrix.columns:
        if col == RETAINED:
            continue
        assert matrix.column_total(col) == resettled.get(col, Fraction(0))
    # 反向: 重结算里也不得多出矩阵没有的科目
    for col, total in resettled.items():
        assert matrix.column_total(col) == total


def test_grand_total_conserved(matrix, segments):
    """矩阵总额 == 原始服务段总额 (含留存), 一分钱不多不少."""
    assert matrix.grand_total() == sum(
        (seg.amount for seg in segments), Fraction(0)
    )


def test_nothing_migrates_before_effective(matrix, segments):
    """生效时刻之前的费用全部留在 RETAINED 列, 独立重算验证."""
    expected = Fraction(0)
    for seg in segments:
        retained_end = min(seg.end, EFFECTIVE_AT)
        if seg.begin < retained_end:
            expected += seg.rate * exact_seconds(retained_end - seg.begin)
    assert matrix.column_total(RETAINED) == expected
    assert expected > 0  # 场景里确实有生效前的费用


def test_pending_only_from_undecided_zones(matrix, segments):
    """挂账金额 == 落在重叠/空档区的费用, 用逐段手工切分独立验证."""
    expected = Fraction(0)
    for seg in segments:
        window_begin = max(seg.begin, EFFECTIVE_AT)
        if window_begin >= seg.end:
            continue
        # 待定区 = 每天 19:00-20:00 (甲∩机动) 与 20:00-21:00 (乙∩机动)
        day = window_begin.date()
        while day <= seg.end.date():
            for start_h, end_h in ((19, 20), (20, 21)):
                zone_begin = datetime.combine(day, datetime.min.time()) + timedelta(
                    hours=start_h
                )
                zone_end = zone_begin + timedelta(hours=end_h - start_h)
                clip_begin = max(window_begin, zone_begin)
                clip_end = min(seg.end, zone_end)
                if clip_begin < clip_end:
                    expected += seg.rate * exact_seconds(clip_end - clip_begin)
            day += timedelta(days=1)
    assert matrix.column_total(PENDING) == expected
    assert expected > 0


def test_cross_midnight_shift_attribution():
    """跨午夜班次: 23:00-01:00 的服务段必须完整落入乙班."""
    segments = [
        ServiceSegment(
            "X1",
            datetime(2026, 11, 20, 23, 0),
            datetime(2026, 11, 21, 1, 0),
            "120.00",
        )
    ]
    matrix = build_transfer_matrix(
        segments, OLD_SHIFTS, NEW_SHIFTS, datetime(2026, 11, 15, 12, 0)
    )
    assert matrix.column_total("乙班") == Fraction("120.00")
    assert matrix.column_total(PENDING) == 0
    assert matrix.column_total(RETAINED) == 0


def test_segment_fully_inside_pending_zone():
    """整段落在重叠待定区 -> 全额挂账, 新旧班都不得入账."""
    segments = [
        ServiceSegment(
            "X2",
            datetime(2026, 11, 20, 19, 0),
            datetime(2026, 11, 20, 21, 0),
            "60.00",
        )
    ]
    matrix = build_transfer_matrix(
        segments, OLD_SHIFTS, NEW_SHIFTS, datetime(2026, 11, 15, 12, 0)
    )
    assert matrix.column_total(PENDING) == Fraction("60.00")
    assert matrix.column_total("甲班") == 0
    assert matrix.column_total("乙班") == 0
    assert matrix.column_total("机动班") == 0


def test_amounts_stay_exact_fractions(matrix):
    """矩阵每个单元格都是 Fraction, 从源头杜绝两套账各自抹小数."""
    for old in matrix.old_shift_names:
        for col in matrix.columns:
            assert isinstance(matrix.cell(old, col), Fraction)


def test_float_amount_rejected():
    """金额不允许 float, 防止二进制浮点污染账本."""
    with pytest.raises(TypeError):
        ServiceSegment(
            "X3", datetime(2026, 11, 1), datetime(2026, 11, 1, 1), 0.1
        )
