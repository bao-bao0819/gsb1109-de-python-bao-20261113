# -*- coding: utf-8 -*-
"""班表切换费用转移矩阵.

旧巡检班表切换到新班表时, 按指定生效时刻把旧账中生效时刻之后的费用
迁移到新班科目, 产出旧班 -> 新班的费用转移矩阵, 并保证:

- 只迁移生效时刻之后的部分, 原始服务段本身不被修改;
- 新班表允许跨午夜班次与重叠待定区, 重叠/未覆盖部分的费用挂账;
- 全部金额使用 fractions.Fraction 精确运算, 两套账不在中间环节抹小数;
- 按原班次加总可还原旧账, 按新班次加总与整段重新结算结果对齐.
"""

from .model import (
    ShiftTemplate,
    ShiftInstance,
    ServiceSegment,
    instantiate_range,
    exact_seconds,
)
from .transfer import (
    PENDING,
    RETAINED,
    TransferMatrix,
    build_transfer_matrix,
    resettle_under_new_schedule,
    old_ledger_totals,
)

__all__ = [
    "ShiftTemplate",
    "ShiftInstance",
    "ServiceSegment",
    "instantiate_range",
    "exact_seconds",
    "PENDING",
    "RETAINED",
    "TransferMatrix",
    "build_transfer_matrix",
    "resettle_under_new_schedule",
    "old_ledger_totals",
]
