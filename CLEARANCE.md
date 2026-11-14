# 沿程导线净空分析（含误差与栅格空洞）

`srtm.clearance` 在一条测线上逐里程计算导线对地面的净空，并按测量误差把
每个里程分为四档，输出连续里程段和状态变化位置的误差括号。

## 输入

测站 CSV（`load_stations`，表头固定）：

| 列 | 含义 |
| --- | --- |
| `mileage` | 里程（米），必须严格递增 |
| `latitude`, `longitude` | 测站经纬度（十进制度） |
| `wire_elevation` | 导线标高（米） |
| `wire_sigma` | 导线标高的 1σ 测量误差（米） |

地面来自本地栅格：一个 `GeoElevationFile`（单个 `.hgt`）或
`GeoElevationData`（多个瓦片）。栅格误差用 `raster_sigma` 给定，可为标量
或 `sigma(row, column)` 回调。

## 插值与误差口径

1. 导线：相邻测站之间按里程线性插值。导线误差默认按独立误差传播
   `sqrt(((1-t)σ0)² + (tσ1)²)`（`wire_error_mode='rss'`）；需要最保守口径
   时用 `'envelope'`，取线性误差包络 `(1-t)σ0 + tσ1`。
2. 地面（指定的格内插值口径）：严格复用本库的 IDW，即
   `GeoElevationFile._InverseDistanceWeighted(radius=1)` 的十字五点反距离
   加权（中心 + 南北东西邻点，权重 ∝ 1/距离）。`idw_ground` 的数值结果与
   库函数逐点一致（见 `test_clearance.py::IdwTests`），并额外暴露归一化
   权重用于误差传播：`σ_ground² = Σ(wᵢσᵢ)²`。
3. 栅格空洞：值为 `None` 的像元一律跳过，绝不以零海拔替代。十字邻域内有效
   像元少于 `min_support`（默认 2）时，该里程判为 `UNKNOWN`。每个缺失邻点
   可通过 `void_sigma` 计入额外不确定度。
4. 合成净空误差：`σ = sqrt(σ_wire² + σ_ground²)`，置信带为
   `c ± kσ`（默认 `k=1.96`，即 95% 双侧带）。

## 四档判定（限值 `required_clearance`）

| 状态 | 条件 | 含义 |
| --- | --- | --- |
| `DEFICIENT` 肯定不足 | 置信带上界 `< 限值` | 即使误差都朝有利方向，净空仍不够 |
| `SUFFICIENT` 确定足够 | 置信带下界 `≥ 限值` | 即使误差都朝不利方向，净空仍够 |
| `POSSIBLE` 可能不足 | 置信带跨越限值 | 证据不足，需复测 |
| `UNKNOWN` 数据不足 | 栅格无法支撑插值（空洞/无瓦片） | 需补测地面，禁止按零海拔处理 |

## 输出

`analyze_clearance` 返回 `ClearanceResult`：

- `samples`：每个采样里程的导线/地面标高、净空、σ、置信带和状态。
- `segments`：同状态的连续里程段。每段给出里程范围、最危险里程及其净空
  置信带、段内最小有效十字像元数。
- `transitions`：每次状态变化的里程括号
  `[mileage_left, mileage_right]`（括号半宽 = 半个采样步距）、两侧置信带和
  处置建议。涉及 `POSSIBLE` 或 `UNKNOWN` 的变化点建议派人复测；
  `DEFICIENT↔SUFFICIENT` 的直接跳变证据已足，现场按括号里程核定即可。

`write_reports` 写出 `clearance_segments.csv` 和
`clearance_transitions.csv`。

## 用法

库调用：

```python
from srtm import clearance
stations = clearance.load_stations('stations.csv')
result = clearance.analyze_clearance(
    stations, tile_or_tilemap,
    required_clearance=8.0,
    confidence=1.96,
    raster_sigma=2.0,
    void_sigma=3.0,
    min_support=2,
    wire_error_mode='rss')
clearance.write_reports(result, 'reports')
```

命令行（本地 `.hgt`，无需联网）：

```
python clearance_report.py stations.csv \
    --hgt-dir test_files --required-clearance 8.0 \
    --raster-sigma 2.0 --void-sigma 3.0 --out reports
```

合成数据端到端演示（含山脊、空洞、最危险点落在两站之间）：

```
python clearance_demo.py
```

## 测量员处置规则

- 优先复测 `UNKNOWN` 段（补测地面）和所有 `POSSIBLE` 段。
- `DEFICIENT` 段置信带上界仍低于限值，证据已足，直接安排处理，不必复测；
  段内“最危险里程”优先处理。
- `SUFFICIENT` 段置信带下界仍高于限值，证据已足，无需复测。
- 变化位置以 `clearance_transitions.csv` 的里程括号为准，括号半宽即该变化
  点的位置不确定度。
