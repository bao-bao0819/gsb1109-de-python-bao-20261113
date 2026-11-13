# -*- coding: utf-8 -*-
"""cargo_feasibility 的测试：可行性账、分时安排约束与欠交证明。"""
import pytest

from cargo_feasibility import (
    Batch,
    CapacityCalendar,
    feasibility_ledger,
    standalone_capacity,
)

EPS = 1e-6


def assert_schedule_valid(report, batches, calendar):
    """逐条核对分时安排：交足、不出窗、不超速、同段合计不超线路能力。"""
    by_name = {b.name: b for b in batches}
    for name, entries in report.schedule.items():
        batch = by_name[name]
        assert abs(sum(t for _, _, t in entries) - batch.demand) < EPS
        for begin, end, tons in entries:
            assert batch.start - EPS <= begin
            assert end <= batch.end + EPS
            assert tons <= batch.max_rate * (end - begin) + EPS

    points = set()
    for iv in calendar.tree:
        points.update((iv.begin, iv.end))
    for entries in report.schedule.values():
        for begin, end, _ in entries:
            points.update((begin, end))
    points = sorted(points)
    for begin, end in zip(points[:-1], points[1:]):
        used = sum(
            tons / (e - b)
            for entries in report.schedule.values()
            for b, e, tons in entries
            if b <= begin and end <= e
        )
        assert used <= calendar.rate_at((begin + end) / 2) + EPS


def maintenance_calendar():
    return CapacityCalendar([
        (0, 60, 10.0),
        (60, 120, 4.0),   # 检修降载
        (120, 180, 10.0),
    ])


def test_feasible_schedule_respects_all_constraints():
    calendar = maintenance_calendar()
    batches = [
        Batch("甲", 0, 120, 600, 10),
        Batch("乙", 30, 90, 240, 8),
    ]
    report = feasibility_ledger(batches, calendar)
    assert report.feasible
    assert report.shortage is None
    assert abs(report.max_deliverable - 840) < EPS
    assert_schedule_valid(report, batches, calendar)


def test_individually_feasible_but_jointly_short():
    calendar = maintenance_calendar()
    batches = [
        Batch("甲", 0, 120, 800, 10),
        Batch("乙", 30, 180, 700, 10),
    ]
    for b in batches:  # 单批各自看都够
        assert standalone_capacity(b, calendar) >= b.demand

    report = feasibility_ledger(batches, calendar)
    assert not report.feasible
    shortage = report.shortage
    assert abs(shortage.deficit - 60.0) < EPS  # 总能力 1440 < 应交 1500
    assert set(shortage.batches) == {"甲", "乙"}
    assert abs(shortage.group_demand - 1500.0) < EPS
    assert abs(shortage.usable_capacity - 1440.0) < EPS
    assert 0 <= shortage.window[0] < shortage.window[1] <= 180


def test_shortage_pinpoints_the_deficient_group():
    calendar = CapacityCalendar([(0, 120, 10.0)])
    batches = [
        Batch("甲", 0, 60, 700, 20),   # 窗内只有 600 t 能力
        Batch("乙", 60, 120, 100, 10), # 自己窗内绰绰有余
    ]
    report = feasibility_ledger(batches, calendar)
    assert not report.feasible
    shortage = report.shortage
    assert shortage.batches == ["甲"]  # 欠交的是甲，不冤枉乙
    assert abs(shortage.deficit - 100.0) < EPS
    assert shortage.window == (0, 60)
    assert abs(shortage.usable_capacity - 600.0) < EPS


def test_idle_time_cannot_be_lent_outside_window():
    calendar = CapacityCalendar([(0, 20, 1.0)])
    batch = Batch("甲", 0, 10, 20, 5)  # 窗内最多 10 t，[10,20) 空闲借不来
    report = feasibility_ledger([batch], calendar)
    assert not report.feasible
    shortage = report.shortage
    assert shortage.batches == ["甲"]
    assert abs(shortage.deficit - 10.0) < EPS
    assert shortage.window == (0, 10)


def test_maintenance_shutdown_means_zero_capacity():
    calendar = CapacityCalendar([(0, 60, 10.0)])  # 60 之后线路停用
    batch = Batch("甲", 0, 120, 700, 10)
    report = feasibility_ledger([batch], calendar)
    assert not report.feasible
    assert abs(report.shortage.deficit - 100.0) < EPS


def test_rate_limit_is_respected_even_with_spare_line_capacity():
    calendar = CapacityCalendar([(0, 100, 10.0)])
    batch = Batch("甲", 0, 100, 1000, 5)  # 线路够，自己流量不够
    report = feasibility_ledger([batch], calendar)
    assert not report.feasible
    assert abs(report.shortage.deficit - 500.0) < EPS


def test_empty_batch_list_is_feasible():
    report = feasibility_ledger([], maintenance_calendar())
    assert report.feasible
    assert report.total_demand == 0


def test_standalone_capacity_clips_to_window():
    calendar = maintenance_calendar()
    assert standalone_capacity(Batch("甲", 0, 120, 800, 10), calendar) == 840.0
    assert standalone_capacity(Batch("乙", 30, 150, 700, 10), calendar) == 840.0
    # 窗口外能力不计；自身流量上限也计入
    assert standalone_capacity(Batch("丙", 0, 180, 0, 3), calendar) == 540.0


def test_calendar_accepts_plain_periods():
    report = feasibility_ledger(
        [Batch("甲", 0, 10, 50, 10)],
        [(0, 10, 5.0)],
    )
    assert report.feasible
    assert report.schedule["甲"] == [(0, 10, 50.0)]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
