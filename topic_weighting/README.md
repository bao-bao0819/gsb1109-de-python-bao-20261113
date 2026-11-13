# 专题搜索加权前三判定

给定每篇稿在三个专题字段上的相关度，以及三个权重各自的编辑部批准范围，
本工具对每篇稿给出三类判定：

- **稳进前三（guaranteed）**：在所有可行权重下都能进入前三；
- **有机会（possible）**：存在某一组可行权重使其进入前三，并附上这组实际可用的权重；
- **无缘前三（impossible）**：任何可行权重都无法使其进入前三。

## 规则口径

- 权重 `w1, w2, w3` 非负、和为 1，且每个 `wi` 落在批准区间 `[low_i, high_i]` 内；
- 所有稿使用**同一组**权重，不存在为不同稿分别取极值的情况；
- 稿 `m` 的得分为 `S_m = r_m1*w1 + r_m2*w2 + r_m3*w3`，按得分降序排名；
- 完全平分时按**稿号升序**（稿号小者列前），因此任意固定权重下排名是一个全序。

## 为什么不会出现"假榜"

错误做法是逐对手单独取最优：让某稿的强项字段权重取上限，同时让每个对手各自
的强项字段权重取下限。这些条件彼此冲突，无法由同一组权重同时满足，拼出来的榜单
是假的。

本工具把可行权重集合表示为单纯形与六个区间半平面交成的**凸多边形**（可退化为线段
或单点），而"对手 j 压过稿 m"只是一条线性半平面。于是：

- 稿 m 稳进前三，当且仅当**不存在**三个对手能在同一点上同时压过它——通过对所有
  三元组做半平面裁剪精确判定；
- 稿 m 有机会，当且仅当能找到一个可行点，使压过它的对手不超过两个，并返回该点作为
  见证权重。

所有可行性、平分、严格不等式判断都用 `fractions.Fraction` 精确完成（输入小数按字符串
精确转分数），没有浮点 epsilon 翻转名次的问题；只有对外展示时才转成浮点数。见证权重
统一构造在可行区域的严格内点（恒平分情形除外），因此转成浮点后仍保留正裕度、排名不变。

测试中的独立预言机枚举全部对等分线与可行多边形的交点和有理网格点，对随机实例逐一
核对判定结果与见证合法性。

## 命令行用法

```bash
python -m topic_weighting topic_weighting/examples/relevance.csv \
    --ranges topic_weighting/examples/ranges.csv
```

也可直接传三个区间：

```bash
python -m topic_weighting export.csv --range 0.2,0.5 --range 0.2,0.5 --range 0.2,0.5
```

加 `--json` 输出机器可读结果；加 `--rank-with w1,w2,w3` 可打印某组具体权重下的完整榜单，
便于人工核验平分按稿号的处理。数据支持 CSV（首行表头，首列稿号，后三列相关度）或
JSON（`[{"number": 1, "relevance": [..,..,..]}]`）。

## 库 API

```python
from topic_weighting import classify, rank_table

records = [
    {"number": 1, "relevance": (3, 3, 0)},
    {"number": 2, "relevance": (3, 0, 3)},
]
lows, highs = (0.2, 0.2, 0.2), (0.5, 0.5, 0.5)

for entry in classify(records, lows, highs):
    print(entry["number"], entry["verdict"], entry["weights"], entry["witness_rank"])

print(rank_table(records, (0.5, 0.3, 0.2)))
```

`entry["weights"]` 为 `None` 当且仅当判定为 `impossible`；其余情况下它是一组满足全部
约束、且 `witness_rank <= 3` 的实际可用权重。

## 测试

```bash
python -m pytest tests/test_topic_weighting.py
```
