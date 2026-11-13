# 工位归属还原（shift_scheduling）

生产日志只保留每张单（job）的工序开始/结束时刻与工位**类型**，仅少量巡检
记录把某张单钉在具体工位上。本模块在满足以下车间规则的前提下，还原每张单
落在了哪台物理工位，并据此计算设备利用率。

## 规则与建模

- 工序开始后不得换位：每张单的整段 `[start, end)` 必须整体落在同一台工位。
- 同一工位不能同时做两单：同一工位上的区间不得重叠。
- 结束时刻恰好等于下一单开始时刻允许衔接：区间采用半开 `[start, end)`
  语义，与 `intervaltree.IntervalTree` 的原生约定一致——`tree.overlap(a, b)`
  对端点相碰天然返回空，无需额外特判。
- 工位类型限制：单只能排到同类型的物理工位；巡检记录（`pinned_station`）
  把候选集合收缩为一台。

每台工位维护一棵区间树，候选工位合法性判定就是一次
`not tree.overlap(start, end)`。

## 求解与三分类

`Reconstruction._solve` 用 MRV（最少候选优先）回溯枚举整班排位：

1. 每一步挑选当前合法工位最少的单，快速触发剪枝；
2. 工位区间树上 `addi` / `removei` 完成试探与回退；
3. 枚举上限 `solution_limit` 防止解空间爆炸，`truncated` 标志报告是否截断。

对枚举到的全部整班排位取交集，得到三分类：

- **唯一归属（unique）**：在所有可行排位中该单都落在同一台工位。注意唯一性
  可以由巡检钉点或其他单的约束传播间接产生（见
  `test_uniqueness_can_propagate_from_another_jobs_pin`）。
- **存在不同归属（ambiguous）**：该单在不同可行排位中可落在至少两台工位。
  `demonstrate_ambiguity(job_id)` 直接给出两份完整整班排位，两者对该单的
  归属不同，但都满足其余全部日志（含巡检钉点、类型匹配、无重叠）。
- **根本排不下的时间段（overload_windows）**：按工位类型做时间轴扫描，
  任一时刻并行需求超过该类型工位数量的基本片段合并为过载窗口，记录
  `begin/end/demand/capacity/jobs`；处于这些窗口内的单列入
  `unschedulable_jobs`。这是比"某张单排不下"更精确的第三类答案——
  排不下的是产能不足的那段时间。
  还有一种没有瞬时过载却仍然排不下的情况：巡检钉点之间的约束传播互相
  冲突（重叠图可染但预着色不可扩展）。此时 `pin_witness` 给出临时解除
  全部钉点后的一份可行排位、以及它违反的钉点清单，直接定位根因。

## 输出物

- `idle_windows`：每台工位在整班跨度内的空闲区间，`__any_station__`
  汇总全厂所有工位同时空闲的时段。
- `utilization(schedule)`：给定排位下各工位忙时占整班比例。
- `verify(...)`：独立校验函数，检查覆盖完整性、类型匹配、巡检钉点与
  同站重叠，返回全部违规描述；演示与测试中每份排位都经它复核。

## 使用方式

```python
from shift_scheduling import JobLog, Reconstruction

jobs = [
    JobLog("J1", 0, 12, "A", pinned_station="A1"),
    JobLog("J2", 0, 12, "A"),
]
engine = Reconstruction(jobs, {"A": ("A1", "A2"), "B": ("B1",)})
report = engine.classify()
plan_a, plan_b = engine.demonstrate_ambiguity("J2")
```

CSV 入口见 `shift_scheduling/logio.py`，完整示例运行：

```sh
python -m examples.demo
```
