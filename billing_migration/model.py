# -*- coding: utf-8 -*-
"""班表与服务段的数据模型.

时间一律按 [begin, end) 左闭右开处理; 时长用 Fraction 精确秒数表示,
避免 timedelta.total_seconds() 的浮点误差.
"""

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from fractions import Fraction


def exact_seconds(delta):
    """timedelta -> 精确秒数 (Fraction), 不走浮点."""
    return (
        Fraction(delta.days * 86400 + delta.seconds)
        + Fraction(delta.microseconds, 10**6)
    )


@dataclass(frozen=True)
class ShiftTemplate:
    """班次的每日模板. end <= start 表示跨午夜 (结束时刻在次日)."""

    name: str
    start: time
    end: time

    @property
    def crosses_midnight(self):
        return self.end <= self.start


@dataclass(frozen=True)
class ShiftInstance:
    """班次在某一天的具体时间区间 [begin, end)."""

    name: str
    begin: datetime
    end: datetime

    @staticmethod
    def instantiate(template, day):
        begin = datetime.combine(day, template.start)
        end = datetime.combine(day, template.end)
        if template.crosses_midnight:
            end += timedelta(days=1)
        return ShiftInstance(template.name, begin, end)


def instantiate_range(templates, begin, end):
    """生成覆盖 [begin, end) 的全部班次实例.

    为覆盖跨午夜班次在区间边缘伸进来的部分, 前后各多展开一天.
    返回按 begin 排序的 ShiftInstance 列表.
    """
    instances = []
    day = (begin - timedelta(days=1)).date() if isinstance(begin, datetime) else begin
    last_day = (end + timedelta(days=1)).date() if isinstance(end, datetime) else end
    while day <= last_day:
        for template in templates:
            instances.append(ShiftInstance.instantiate(template, day))
        day += timedelta(days=1)
    instances.sort(key=lambda inst: (inst.begin, inst.end))
    return instances


@dataclass(frozen=True)
class ServiceSegment:
    """原始服务段: 旧账下的计费单元, 迁移过程中保持不变.

    amount 用 Fraction 保存精确金额; 段内费率视为均匀,
    生效时刻之后的部分按时间比例切出并迁移.
    """

    seg_id: str
    begin: datetime
    end: datetime
    amount: Fraction

    def __init__(self, seg_id, begin, end, amount):
        object.__setattr__(self, "seg_id", seg_id)
        object.__setattr__(self, "begin", begin)
        object.__setattr__(self, "end", end)
        object.__setattr__(self, "amount", amount_as_fraction(amount))
        if not begin < end:
            raise ValueError("segment %r: begin must be before end" % seg_id)

    @property
    def duration(self):
        return exact_seconds(self.end - self.begin)

    @property
    def rate(self):
        """每秒费率 (精确)."""
        return self.amount / self.duration


def amount_as_fraction(amount):
    """金额 -> 精确 Fraction. 接受 Fraction/int/str/Decimal, 拒绝 float."""
    if isinstance(amount, Fraction):
        return amount
    if isinstance(amount, float):
        raise TypeError("金额不允许用 float, 请用 str 或 Decimal 以免丢精度")
    return Fraction(amount)
