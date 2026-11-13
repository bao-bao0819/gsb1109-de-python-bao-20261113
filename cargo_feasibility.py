# -*- coding: utf-8 -*-
"""散货交库可行性账（feasibility ledger）。

每批散货有：

* 可通行的时间窗 ``[start, end)``（分钟）；
* 应交吨数 ``demand``；
* 每分钟最高流量 ``max_rate``（吨/分钟）。

共用输送线的总能力随检修分段变化，用一组
``(begin, end, tons_per_minute)`` 区间表示。

判定思路（全部建立在现有区间操作之上）：

1. 用 ``IntervalTree.slice`` 把所有批次窗口的端点切进能力日历，
   得到若干“初等时段”，每段内线路能力恒定；
2. 用 ``IntervalTree.overlap`` 找出每段落在哪些批次的窗口里
   （空闲时段不会借给窗口外的货）；
3. 建最大流网络：

   * 源 -> 批次 i，容量 = 应交吨数；
   * 批次 i -> 时段 j，容量 = 最高流量 × 段长（仅当段 j 在批次 i 窗内）；
   * 时段 j -> 汇，容量 = 线路能力 × 段长。

4. 最大流 == 总应交  <=>  可行，批次->时段边上的流量即分时安排；
   否则对残余图从源做可达性分析，最小割给出“哪组货至少短了多少”：
   可达的那组批次，即使把它们能用的时段能力全部占满，仍短缺
   ``总应交 - 最大流`` 吨。

单批各自看都够并不代表合起来够——``standalone_capacity`` 只算单批
独占线路的上限，联合可行性必须过 ``feasibility_ledger``。
"""
from collections import deque
from dataclasses import dataclass, field

from intervaltree import Interval, IntervalTree

EPS = 1e-9

__all__ = [
    "Batch",
    "CapacityCalendar",
    "FeasibilityReport",
    "Shortage",
    "feasibility_ledger",
    "standalone_capacity",
]


@dataclass
class Batch:
    """一批散货：时间窗 [start, end) 内应交 demand 吨，流量不超过 max_rate。"""

    name: str
    start: float
    end: float
    demand: float
    max_rate: float

    def __post_init__(self):
        if self.end < self.start:
            raise ValueError("时间窗 end 早于 start: %r" % self.name)
        if self.demand < 0 or self.max_rate < 0:
            raise ValueError("吨数与流量不能为负: %r" % self.name)


class CapacityCalendar:
    """输送线能力日历：能力（吨/分钟）随检修分段变化。

    periods: 可迭代的 ``(begin, end, tons_per_minute)``，半开区间。
    未覆盖的时刻能力为 0（线路停用）。
    """

    def __init__(self, periods=()):
        self.tree = IntervalTree(
            Interval(begin, end, rate)
            for begin, end, rate in periods
            if end > begin
        )

    def add(self, begin, end, rate):
        """登记一段能力区间。"""
        self.tree.addi(begin, end, rate)

    def rate_at(self, moment):
        """某一时刻的线路能力（吨/分钟）；重叠区间取最大。"""
        return max((iv.data for iv in self.tree.at(moment)), default=0.0)

    def total_capacity(self):
        """日历覆盖范围内的总可输送吨数。"""
        return sum(iv.data * (iv.end - iv.begin) for iv in self.tree)


@dataclass
class Shortage:
    """不可行时的欠交证明：哪组货至少短了多少。"""

    batches: list          # 受阻批次名（最小割给出的那一组）
    deficit: float         # 该组至少短缺的吨数
    window: tuple          # 瓶颈时段 (begin, end)；无瓶颈时段时为 None
    group_demand: float    # 该组应交合计
    usable_capacity: float # 该组在瓶颈内实际能拿到的能力上限


@dataclass
class FeasibilityReport:
    """可行性账的结果。"""

    feasible: bool
    total_demand: float
    max_deliverable: float
    schedule: dict = field(default_factory=dict)  # 批次名 -> [(begin, end, tons), ...]
    shortage: Shortage = None


@dataclass
class _Segment:
    """初等时段：段内线路能力恒定，且不被任何批次窗口端点切开。"""

    begin: float
    end: float
    rate: float  # 吨/分钟

    @property
    def length(self):
        return self.end - self.begin

    @property
    def capacity(self):
        return self.rate * self.length


def _elementary_segments(batches, calendar):
    """把能力日历按所有批次窗口端点切片，返回排序后的初等时段。"""
    tree = IntervalTree(calendar.tree)
    for batch in batches:
        tree.slice(batch.start)
        tree.slice(batch.end)
    by_range = {}
    for iv in tree:
        key = (iv.begin, iv.end)
        by_range[key] = max(by_range.get(key, 0.0), iv.data)
    return [
        _Segment(begin, end, rate)
        for (begin, end), rate in sorted(by_range.items())
    ]


def standalone_capacity(batch, calendar):
    """单批独占输送线时的最大可交吨数（只看自己窗口内的能力）。

    >>> cal = CapacityCalendar([(0, 60, 10.0), (60, 120, 4.0)])
    >>> standalone_capacity(Batch("甲", 0, 120, 800, 10), cal)
    840.0
    """
    total = 0.0
    for iv in calendar.tree.overlap(batch.start, batch.end):
        begin = max(iv.begin, batch.start)
        end = min(iv.end, batch.end)
        total += min(iv.data, batch.max_rate) * (end - begin)
    return total


class _Dinic:
    """列表式邻接的 Dinic 最大流；add_edge 返回正向边以便回读流量。"""

    def __init__(self, n):
        self.graph = [[] for _ in range(n)]

    def add_edge(self, fr, to, cap):
        fwd = [to, cap, len(self.graph[to])]
        rev = [fr, 0.0, len(self.graph[fr])]
        self.graph[fr].append(fwd)
        self.graph[to].append(rev)
        return fwd

    def max_flow(self, source, sink):
        flow = 0.0
        n = len(self.graph)
        while True:
            level = [-1] * n
            level[source] = 0
            queue = deque([source])
            while queue:
                v = queue.popleft()
                for to, cap, _ in self.graph[v]:
                    if cap > EPS and level[to] < 0:
                        level[to] = level[v] + 1
                        queue.append(to)
            if level[sink] < 0:
                return flow
            it = [0] * n

            def push(v, f):
                if v == sink:
                    return f
                edges = self.graph[v]
                while it[v] < len(edges):
                    edge = edges[it[v]]
                    to, cap, rev = edge
                    if cap > EPS and level[to] == level[v] + 1:
                        d = push(to, min(f, cap))
                        if d > EPS:
                            edge[1] -= d
                            self.graph[to][rev][1] += d
                            return d
                    it[v] += 1
                return 0.0

            while True:
                pushed = push(source, float("inf"))
                if pushed <= EPS:
                    break
                flow += pushed

    def reachable_from(self, source):
        """残余图中从 source 可达的节点集合（最小割的源侧）。"""
        seen = [False] * len(self.graph)
        seen[source] = True
        queue = deque([source])
        while queue:
            v = queue.popleft()
            for to, cap, _ in self.graph[v]:
                if cap > EPS and not seen[to]:
                    seen[to] = True
                    queue.append(to)
        return seen


def feasibility_ledger(batches, calendar):
    """做一张可行性账。

    可行：``report.feasible`` 为真，``report.schedule`` 给出各批的分时
    安排 ``[(begin, end, tons), ...]``，每段流量不超过该批最高流量，
    同时段各批合计不超过线路能力。

    不可行：``report.shortage`` 指出哪组货至少短了多少吨、瓶颈时段、
    该组应交合计与实际能拿到的能力上限。

    >>> cal = CapacityCalendar([(0, 60, 10.0), (60, 120, 4.0)])
    >>> rep = feasibility_ledger([Batch("甲", 0, 120, 600, 10)], cal)
    >>> rep.feasible, rep.total_demand, rep.max_deliverable
    (True, 600, 600)
    >>> rep.schedule["甲"]
    [(0, 60, 600)]
    """
    batches = list(batches)
    if not isinstance(calendar, CapacityCalendar):
        calendar = CapacityCalendar(calendar)
    segments = _elementary_segments(batches, calendar)
    window_tree = IntervalTree(
        Interval(b.start, b.end, i)
        for i, b in enumerate(batches)
        if b.end > b.start
    )

    n, m = len(batches), len(segments)
    source, sink = 0, n + m + 1
    dinic = _Dinic(n + m + 2)
    for i, batch in enumerate(batches):
        dinic.add_edge(source, 1 + i, batch.demand)
    plan_edges = {}  # (批次下标, 时段下标) -> (正向边, 原始容量)
    for j, seg in enumerate(segments):
        for iv in window_tree.overlap(seg.begin, seg.end):
            i = iv.data
            cap = batches[i].max_rate * seg.length
            plan_edges[i, j] = (dinic.add_edge(1 + i, 1 + n + j, cap), cap)
        dinic.add_edge(1 + n + j, sink, seg.capacity)

    total_demand = sum(b.demand for b in batches)
    delivered = dinic.max_flow(source, sink)

    schedule = {b.name: [] for b in batches}
    for (i, j), (edge, cap) in plan_edges.items():
        tons = cap - edge[1]
        if tons > EPS:
            seg = segments[j]
            schedule[batches[i].name].append((seg.begin, seg.end, tons))

    if delivered >= total_demand - EPS:
        return FeasibilityReport(
            feasible=True,
            total_demand=total_demand,
            max_deliverable=total_demand,
            schedule=schedule,
        )

    reachable = dinic.reachable_from(source)
    group = [batches[i] for i in range(n) if reachable[1 + i]]
    seg_idx = [j for j in range(m) if reachable[1 + n + j]]
    window = None
    if seg_idx:
        window = (
            min(segments[j].begin for j in seg_idx),
            max(segments[j].end for j in seg_idx),
        )
    deficit = total_demand - delivered
    group_demand = sum(b.demand for b in group)
    return FeasibilityReport(
        feasible=False,
        total_demand=total_demand,
        max_deliverable=delivered,
        schedule=schedule,
        shortage=Shortage(
            batches=[b.name for b in group],
            deficit=deficit,
            window=window,
            group_demand=group_demand,
            usable_capacity=group_demand - deficit,
        ),
    )


def _demo():
    """演示：单批各自看都够，合起来不够；空闲时段借不出去。"""
    calendar = CapacityCalendar([
        (0, 60, 10.0),    # 正常
        (60, 120, 4.0),   # 检修降载
        (120, 180, 10.0), # 恢复
    ])
    batches = [
        Batch("甲", 0, 120, 800, 10),
        Batch("乙", 30, 180, 700, 10),
    ]
    print("== 单批独占线路的上限 ==")
    for b in batches:
        cap = standalone_capacity(b, calendar)
        print("  %s: 应交 %6.0f t, 独占可交 %6.0f t -> %s"
              % (b.name, b.demand, cap, "够" if cap >= b.demand else "不够"))

    report = feasibility_ledger(batches, calendar)
    print("== 联合可行性账 ==")
    print("  总应交 %.0f t, 实际最多可交 %.0f t"
          % (report.total_demand, report.max_deliverable))
    if report.feasible:
        for name, entries in report.schedule.items():
            for begin, end, tons in entries:
                print("  %s: [%5.0f, %5.0f) 交 %7.1f t (%4.1f t/min)"
                      % (name, begin, end, tons, tons / (end - begin)))
    else:
        s = report.shortage
        print("  不可行：%s 这一组至少短 %.0f t" % ("、".join(s.batches), s.deficit))
        print("  该组应交 %.0f t，瓶颈时段 [%s, %s) 内最多只能拿到 %.0f t"
              % (s.group_demand,
                 "%.0f" % s.window[0], "%.0f" % s.window[1],
                 s.usable_capacity))

    print("== 去掉乙之后的可行账 ==")
    report = feasibility_ledger([batches[0]], calendar)
    for name, entries in report.schedule.items():
        for begin, end, tons in entries:
            print("  %s: [%5.0f, %5.0f) 交 %7.1f t (%4.1f t/min)"
                  % (name, begin, end, tons, tons / (end - begin)))


if __name__ == "__main__":
    _demo()
