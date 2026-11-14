# -*- coding: utf-8 -*-
"""
两期高程格网（施工前/后）土方量的区间界计算。

模型
----
将两期不同分辨率的格网重采样到共同计算单元（面积 A）。
对每个有效单元 i，量测高差 m_i = z_after - z_before（正为填、负为挖）。
真值 d_i = m_i + e_i + delta，其中：
  e_i   : 单元独立垂直误差（两期合成），|e_i| <= E
  delta : 整批共用的基准偏差（两期基准差），|delta| <= B，对所有单元相同
任一测回中 delta 只有一个取值，且各 e_i 同时实现——这是判断各极值
能否同时出现的关键。

由于挖方 A*sum(max(-d_i,0))、填方 A*sum(max(d_i,0))、净方 A*sum(d_i)
对每个 d_i 单调，极值必在顶点 e_i = ±E、delta = ±B 处取得。
记 T = E + B，则（S = sum(m_i)，N = 有效单元数）：
  挖方 [A*sum(max(-m_i - T, 0)), A*sum(max(-m_i + T, 0))]
  填方 [A*sum(max( m_i - T, 0)), A*sum(max( m_i + T, 0))]
  净方 [A*(S - N*T),             A*(S + N*T)]
任一期缺测的单元为空洞，不计入可确认范围，面积 N_void * A 单独上报。
"""

from dataclasses import dataclass, field
from typing import List, Optional, Sequence


@dataclass
class Extremum:
    """一个单项极值及其可实现的误差见证。"""
    value: float
    common_bias: float          # 取得该极值时的整批基准偏差 delta
    indep_sign: int             # 各单元独立误差的符号（e_i = sign * E）
    note: str = ""


@dataclass
class EarthworkBounds:
    cell_area: float
    n_valid: int
    n_void: int
    void_area: float            # 空洞面积，单独上报
    cut_min: Extremum
    cut_max: Extremum
    fill_min: Extremum
    fill_max: Extremum
    net_min: Extremum
    net_max: Extremum
    cut_fill_max_compatible: bool = False   # 挖方最大与填方最大能否同测回出现
    _notes: List[str] = field(default_factory=list)


def compute_bounds(diffs: Sequence[Optional[float]],
                   cell_area: float,
                   indep_err: float,
                   bias_bound: float) -> EarthworkBounds:
    """
    diffs      : 共同计算单元上的量测高差 m_i（后-前）；None 表示空洞
    cell_area  : 共同计算单元面积 A
    indep_err  : 单元独立垂直误差界 E（两期合成，如 e_pre + e_post）
    bias_bound : 整批共用基准偏差界 B（两期基准差之界）
    """
    m = [d for d in diffs if d is not None]
    n = len(m)
    n_void = len(diffs) - n
    a = cell_area
    t = indep_err + bias_bound

    cut_lo = a * sum(max(-mi - t, 0.0) for mi in m)
    cut_hi = a * sum(max(-mi + t, 0.0) for mi in m)
    fill_lo = a * sum(max(mi - t, 0.0) for mi in m)
    fill_hi = a * sum(max(mi + t, 0.0) for mi in m)
    s = sum(m)
    net_lo = a * (s - n * t)
    net_hi = a * (s + n * t)

    return EarthworkBounds(
        cell_area=a,
        n_valid=n,
        n_void=n_void,
        void_area=n_void * a,
        cut_max=Extremum(cut_hi, common_bias=-bias_bound, indep_sign=-1,
                         note="delta=-B, e_i=-E：全体高差下移，挖方最大"),
        cut_min=Extremum(cut_lo, common_bias=+bias_bound, indep_sign=+1,
                         note="delta=+B, e_i=+E：全体高差上移，挖方最小"),
        fill_max=Extremum(fill_hi, common_bias=+bias_bound, indep_sign=+1,
                          note="delta=+B, e_i=+E：全体高差上移，填方最大"),
        fill_min=Extremum(fill_lo, common_bias=-bias_bound, indep_sign=-1,
                          note="delta=-B, e_i=-E：全体高差下移，填方最小"),
        net_max=Extremum(net_hi, common_bias=+bias_bound, indep_sign=+1,
                         note="delta=+B, e_i=+E：与填方最大同一见证"),
        net_min=Extremum(net_lo, common_bias=-bias_bound, indep_sign=-1,
                         note="delta=-B, e_i=-E：与挖方最大同一见证"),
        cut_fill_max_compatible=(t == 0.0 or all(mi == 0.0 for mi in m)),
        _notes=[
            "挖方最大与填方最大不能在同一测回同时出现："
            "前者要求 delta=-B 且 e_i=-E，后者要求 delta=+B 且 e_i=+E，"
            "而 delta 整批唯一、e_i 同次实现。",
            "可同时出现的组合：挖方最大+填方最小+净方最小（delta=-B）；"
            "挖方最小+填方最大+净方最大（delta=+B）。",
        ],
    )


if __name__ == "__main__":
    # 算例：共同单元 25 m^2（5 m 格），独立误差界 E=0.10 m，基准偏差界 B=0.05 m
    diffs = [0.40, -0.30, 0.10, -0.05, None, 0.20, None, -0.15, 0.02]
    r = compute_bounds(diffs, cell_area=25.0, indep_err=0.10, bias_bound=0.05)
    print(f"有效单元 {r.n_valid}，空洞单元 {r.n_void}，空洞面积 {r.void_area:.1f} m^2")
    for name in ("cut", "fill", "net"):
        lo = getattr(r, f"{name}_min")
        hi = getattr(r, f"{name}_max")
        print(f"{name:4s} [{lo.value:9.2f}, {hi.value:9.2f}] m^3   "
              f"min见证 delta={lo.common_bias:+.2f}  max见证 delta={hi.common_bias:+.2f}")
    print("挖方最大与填方最大可同测回出现:", r.cut_fill_max_compatible)
    for note in r._notes:
        print("-", note)
