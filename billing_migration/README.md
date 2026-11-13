# 班表切换费用转移矩阵

本月中途从旧巡检班表切换到新班表时, 产出**旧账 → 新账的费用转移矩阵**,
并保证两套账精确对账、互不抹小数。

## 口径

- **只迁生效时刻之后**: 每个原始服务段保持不变, 仅把 `effective_at` 之后的
  部分按段内均匀费率切出迁移; 之前的部分计入 `留存(生效前)` 列, 留在旧账。
- **跨午夜班次**: 班表模板中 `end <= start` 视为跨午夜, 实例化时结束时刻
  自动顺延到次日 (`model.ShiftTemplate.crosses_midnight`)。
- **重叠待定区挂账**: 新班表实例化后, 同一时刻被两个及以上班次覆盖
  (或没有任何班次覆盖的空档) 的费用计入 `挂账(待定区)` 列, 待裁定后再分派,
  不重复入账也不遗漏。
- **精确金额**: 所有金额用 `fractions.Fraction` 运算; 金额入参拒绝 `float`,
  只接受 `str` / `Decimal` / `Fraction`; 展示层的四舍五入仅用于阅读。

## 对账不变量

1. **旧账还原**: 矩阵每行合计(含留存) == 旧账该班总额。
2. **新账对齐**: 矩阵每列合计 == 对每个原始服务段生效后整段按新班表
   重新结算的结果; 重结算用独立的事件扫描线算法实现
   (`transfer.resettle_under_new_schedule`), 与矩阵的边界切片算法互为校验。
3. **总量守恒**: 矩阵总额 == 原始服务段总额。

## 用法

```bash
python3 -m billing_migration.demo          # 打印转移矩阵 + 对账报告
python3 -m pytest billing_migration/tests/ # 不变量测试
```

```python
from billing_migration import (
    ShiftTemplate, ServiceSegment, build_transfer_matrix,
    old_ledger_totals, resettle_under_new_schedule,
)

matrix = build_transfer_matrix(segments, old_shifts, new_shifts, effective_at)
print(matrix.format())                     # 旧班(行) × 新班(列) 转移矩阵
```

## 文件

- `model.py` — 班表模板/实例、服务段、精确秒数与金额
- `transfer.py` — 转移矩阵构建(边界切片 + 区间树)、独立重结算(扫描线)、旧账汇总
- `demo.py` — 2026-11 月中途切换的完整示例(含跨午夜与重叠待定区)
- `tests/test_transfer.py` — 对账不变量与边界情形测试
