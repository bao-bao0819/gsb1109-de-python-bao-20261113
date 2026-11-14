"""
三个环形互消费数据产品的灰度迁移核对脚本。

场景（一组小清单）
------------------
产品与消费方向（消费者读生产者的输出）：
    A --(B 读 A)--> B --(C 读 B)--> C --(A 读 C)--> A

数据格式本身按 SemVer 版本化，取三个离散格式：
    F1 = 1.0.0   旧格式
    F2 = 2.0.0   新格式
    F3 = 3.0.0   下一代格式（仅 C 的候选版能写）

每个产品“候选版本”给出可读 / 可写的格式范围（用本仓库 semver 的版本比较核对）。
混跑期旧格式 F1 记录必须保留 RETENTION_DAYS 天，仍在窗口内的 F1 记录会卡住
已移除 F1 读能力的新版本。

所有相容性结论都由 src/semver 的 Version 比较实算，而非人工判断。
"""

from __future__ import annotations

from datetime import date, timedelta

from semver import Version

# ----------------------------------------------------------------------
# 1. 小清单：产品候选版本及其读 / 写格式范围
# ----------------------------------------------------------------------
# read  = (下界含, 上界斥)，接受  low <= format < high
# write = 该版本“能够发出”的全部离散格式（灰度期可在其中选定一个 pin）
FORMATS = {
    "F1": Version.parse("1.0.0"),
    "F2": Version.parse("2.0.0"),
    "F3": Version.parse("3.0.0"),
}

PRODUCTS = ("A", "B", "C")

# 消费者 -> 生产者：consumer 读取 producer 的输出
EDGES = [("B", "A"), ("C", "B"), ("A", "C")]

VERSIONS: dict[str, dict[str, dict]] = {
    "A": {
        # 现网旧版：只认 F1，只写 F1
        "1.4.2": {"read": ("1.0.0", "2.0.0"), "write": ["F1"]},
        # 候选版：宽容读（F1/F2），双写（可在混跑期继续 pin F1）
        "2.0.0": {"read": ("1.0.0", "3.0.0"), "write": ["F1", "F2"]},
    },
    "B": {
        "1.6.0": {"read": ("1.0.0", "2.0.0"), "write": ["F1"]},
        # 候选版：大版本升级，彻底移除 F1 读能力，只写 F2
        "2.1.0": {"read": ("2.0.0", "3.0.0"), "write": ["F2"]},
    },
    "C": {
        "1.5.1": {"read": ("1.0.0", "2.0.0"), "write": ["F1"]},
        # 候选版：移除 F1 读，可读 F2/F3，可写 F2/F3（但下游 B2 读不了 F3）
        "3.0.0": {"read": ("2.0.0", "4.0.0"), "write": ["F2", "F3"]},
    },
}

TARGET = {"A": "2.0.0", "B": "2.1.0", "C": "3.0.0"}
CURRENT = {"A": "1.4.2", "B": "1.6.0", "C": "1.5.1"}

# 混跑期旧数据保留天数
RETENTION_DAYS = 7
# 最后一条 F1 旧记录的产生日期（示例）
LAST_F1_RECORD_DATE = date(2026, 11, 13)


def in_range(version: Version, low: str, high: str) -> bool:
    """用仓库的版本比较判断 low <= version < high。"""
    return Version.parse(low) <= version < Version.parse(high)


def edge_compatible(producer: str, producer_ver: str,
                    consumer: str, consumer_ver: str) -> tuple[bool, list[str]]:
    """生产者可写格式与消费者可读范围是否存在交集，返回(是否相容, 可用格式)。"""
    writable = VERSIONS[producer][producer_ver]["write"]
    low, high = VERSIONS[consumer][consumer_ver]["read"]
    common = [f for f in writable if in_range(FORMATS[f], low, high)]
    return bool(common), common


def check_state(state: dict[str, str], title: str,
                pins: dict[str, str] | None = None) -> bool:
    """核对一个部署状态。pins 给出每个生产者选定的实际输出格式。"""
    print(f"\n=== {title}  状态={state}")
    if pins:
        print(f"  输出 pin：{pins}")
    ok_all = True
    for consumer, producer in EDGES:
        ok, common = edge_compatible(producer, state[producer],
                                     consumer, state[consumer])
        pin = (pins or {}).get(producer)
        pin_ok = pin is None or pin in common
        ok_all &= ok and pin_ok
        read_range = VERSIONS[consumer][state[consumer]]["read"]
        write_set = VERSIONS[producer][state[producer]]["write"]
        pin_note = f"，选定 pin={pin} [{'OK' if pin_ok else 'FAIL'}]" if pin else ""
        print(f"  边 {producer}->{consumer}: "
              f"{producer}@{state[producer]} 可写{write_set}  ∩  "
              f"{consumer}@{state[consumer]} 可读[{read_range[0]},{read_range[1]}) "
              f"-> 共用格式 {common or '无'}  [{'OK' if ok else 'FAIL'}]{pin_note}")
    print(f"  => 该状态{'三条读写路径全部相容' if ok_all else '存在断链，不可上线'}")
    return ok_all


def try_single_move(base: dict[str, str], product: str) -> bool:
    """只替换一个产品，其余保持 base，判断是否仍有相容读写路径。"""
    trial = dict(base)
    trial[product] = TARGET[product]
    return check_state(trial, f"试探：仅替换 {product}")


# ----------------------------------------------------------------------
# 2. 现网 -> 灰度 -> 同步切换 的逐步核对
# ----------------------------------------------------------------------
print("#" * 72)
print("# 第一部分：逐步替换，每一步用 semver 实算三条读写路径")
print("#" * 72)

s0 = dict(CURRENT)
check_state(s0, "步骤 0 现网基线（全部 F1）",
            pins={"A": "F1", "B": "F1", "C": "F1"})

print("\n--- 从基线出发，逐个试探谁能先换 ---")
first_movers = []
for p in PRODUCTS:
    if try_single_move(s0, p):
        first_movers.append(p)
print(f"\n可单独先换的产品：{first_movers}（预期仅 A，靠宽容读 + 双写当桥）")

# 步骤 1：A 单独灰度，输出继续 pin F1，保证 B1 不断流
s1 = {"A": TARGET["A"], "B": CURRENT["B"], "C": CURRENT["C"]}
check_state(s1, "步骤 1 A 灰度（A2 输出 pin=F1，读兼容 C1 的 F1）",
            pins={"A": "F1", "B": "F1", "C": "F1"})

print("\n--- A 已灰度后，再逐个试探 B / C 能否单独换 ---")
for p in ("B", "C"):
    try_single_move(s1, p)

# 步骤 2：保留期到期日才允许 {B,C} 同换；同一时刻 A 输出 pin 从 F1 翻到 F2
cutover_date = date(2026, 11, 20)
earliest_cutover = LAST_F1_RECORD_DATE + timedelta(days=RETENTION_DAYS)
assert cutover_date >= earliest_cutover, "切换日早于保留期，F1 旧记录仍被占用"
s2 = dict(TARGET)
check_state(s2, f"步骤 2 {cutover_date} 同步切换 {{B,C}}，A 输出翻 pin=F2",
            pins={"A": "F2", "B": "F2", "C": "F2"})
print("  注：F1 存量记录此日刚满保留期，仍冻结留存；在线链路已全部走 F2。")

# 步骤 3：保留期窗口关闭，清理 F1，A2 关闭 F1 双写/读通道
purge_date = cutover_date + timedelta(days=1)
print(f"\n=== 步骤 3 {purge_date} 保留期结束：归档/删除 F1 记录，A2 停用 F1 通道 ===")
print("  此步无部署变更，仅数据清理；清理后全链路不存在 F1 读写依赖。")

# ----------------------------------------------------------------------
# 3. 若只能一次同步切换：直达全目标态所需的最小同换集
# ----------------------------------------------------------------------
print("\n" + "#" * 72)
print("# 第二部分：只允许一次同步切换时，必须同换的最小产品集")
print("#" * 72)
print("# 判定标准：切换后所有产品均须在目标版本且三条边都相容；")
print("# 未纳入同换集的产品保持旧版，因此只要还存在旧版即不算完成迁移。")

from itertools import combinations

def cutover_subset_reaches_target(subset: tuple[str, ...]) -> bool:
    """同换 subset 后，未同换的仍为旧版；检查这一瞬间每条边是否相容。"""
    state = dict(CURRENT)
    for p in subset:
        state[p] = TARGET[p]
    return all(
        edge_compatible(producer, state[producer], consumer, state[consumer])[0]
        for consumer, producer in EDGES
    )

working: list[tuple[str, ...]] = []
for size in (1, 2, 3):
    for subset in combinations(PRODUCTS, size):
        ok = cutover_subset_reaches_target(subset)
        reaches = set(subset) == set(PRODUCTS)
        note = "可行且直达全目标" if ok and reaches else (
            "中间态相容但未到全目标" if ok else "存在断链，不可行")
        print(f"  同换 {set(subset)}（其余留旧版）: {note}")
        if ok and reaches:
            working.append(subset)
minimal = min(working, key=len)
print(f"\n最小可一次切换成功的同换集合：{set(minimal)}")
print("原因：B2、C2 都已移除 F1 读能力，而旧版上游只能写 F1；环形依赖")
print("A<-C<-B<-A 上没有任何一个产品能独自先动，三家只能打包同换。")

# ----------------------------------------------------------------------
# 4. 保留期：哪些旧记录卡住切换，最早可切日期
# ----------------------------------------------------------------------
print("\n" + "#" * 72)
print(f"# 第三部分：混跑期 F1 旧记录保留 {RETENTION_DAYS} 天的约束")
print("#" * 72)

blocking = [
    ("B 的输入/回溯存量", "A1（及灰度期 pin F1 的 A2）写出的 F1 记录",
     "B2 读区间 [2.0.0,3.0.0) 已无 F1"),
    ("C 的输入/回溯存量", "B1 写出的 F1 记录",
     "C2 读区间 [2.0.0,4.0.0) 已无 F1"),
]
for store, records, reason in blocking:
    print(f"  卡住 {store}：{records}；原因 {reason}")

earliest = LAST_F1_RECORD_DATE + timedelta(days=RETENTION_DAYS)
print(f"\n  最后一条 F1 记录：{LAST_F1_RECORD_DATE}")
print(f"  最早允许的 {{B,C}} 同步切换日：{earliest}"
      f"（= 最后 F1 记录 + {RETENTION_DAYS} 天）")
print("  切换前 A2 必须保留 F1 读能力并持续双写；窗口内 F1 记录要么自然到期、")
print("  要么先做前向转换，否则 B2/C2 无法解析即断流。")
