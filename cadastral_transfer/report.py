"""Reconciliation tables and CSV export for an :class:`AreaLedger`."""

from __future__ import annotations

import csv
from pathlib import Path

from .engine import AreaLedger


def _fmt(value: float, digits: int) -> str:
    return f"{value:.{digits}f}"


def old_rows(ledger: AreaLedger, eps: float) -> list[dict[str, float | str]]:
    """Per old parcel: 去向合计 + 退界 + 争议 + 新批重叠 == 原面积."""
    rows = []
    for old_id in ledger.old_ids:
        outgoing = sum(
            area
            for (src, _), area in ledger.transfer.items()
            if src == old_id and area > eps
        )
        retreat = ledger.retreat.get(old_id, 0.0)
        dispute = ledger.dispute_old.get(old_id, 0.0)
        new_overlap = ledger.new_overlap_on_old.get(old_id, 0.0)
        total = outgoing + retreat + dispute + new_overlap
        rows.append(
            {
                "old_id": old_id,
                "area": ledger.old_area[old_id],
                "outgoing": outgoing,
                "retreat": retreat,
                "dispute": dispute,
                "new_overlap": new_overlap,
                "total": total,
                "check": total - ledger.old_area[old_id],
            }
        )
    return rows


def new_rows(ledger: AreaLedger, eps: float) -> list[dict[str, float | str]]:
    """Per new parcel: 来源合计 + 新增填土 + 涉旧争议 + 新批重叠 == 新面积."""
    rows = []
    for new_id in ledger.new_ids:
        received = sum(
            area
            for (_, dst), area in ledger.transfer.items()
            if dst == new_id and area > eps
        )
        fill = ledger.fill.get(new_id, 0.0)
        old_dispute = ledger.old_dispute_on_new.get(new_id, 0.0)
        new_dispute = ledger.dispute_new.get(new_id, 0.0)
        total = received + fill + old_dispute + new_dispute
        rows.append(
            {
                "new_id": new_id,
                "area": ledger.new_area[new_id],
                "received": received,
                "fill": fill,
                "old_dispute": old_dispute,
                "new_dispute": new_dispute,
                "total": total,
                "check": total - ledger.new_area[new_id],
            }
        )
    return rows


def write_reports(
    ledger: AreaLedger,
    out_dir: str | Path,
    eps: float = 1e-9,
    digits: int = 4,
) -> list[Path]:
    """Write the transfer matrix and both reconciliation tables as CSV."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    matrix_path = out / "转移矩阵.csv"
    with matrix_path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow(["旧宗地＼新宗地", *ledger.new_ids])
        for old_id in ledger.old_ids:
            writer.writerow(
                [old_id]
                + [
                    _fmt(ledger.transfer.get((old_id, new_id), 0.0), digits)
                    for new_id in ledger.new_ids
                ]
            )
    written.append(matrix_path)

    old_path = out / "旧批核算.csv"
    with old_path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            ["旧宗地", "原面积", "去向合计", "退界", "争议", "新批重叠", "合计", "校验差"]
        )
        for row in old_rows(ledger, eps):
            writer.writerow(
                [row["old_id"]]
                + [
                    _fmt(float(row[key]), digits)
                    for key in (
                        "area",
                        "outgoing",
                        "retreat",
                        "dispute",
                        "new_overlap",
                        "total",
                        "check",
                    )
                ]
            )
    written.append(old_path)

    new_path = out / "新批核算.csv"
    with new_path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            ["新宗地", "新面积", "来源合计", "新增填土", "涉旧争议", "新批重叠", "合计", "校验差"]
        )
        for row in new_rows(ledger, eps):
            writer.writerow(
                [row["new_id"]]
                + [
                    _fmt(float(row[key]), digits)
                    for key in (
                        "area",
                        "received",
                        "fill",
                        "old_dispute",
                        "new_dispute",
                        "total",
                        "check",
                    )
                ]
            )
    written.append(new_path)

    dispute_path = out / "争议明细.csv"
    with dispute_path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow(["批次", "宗地甲", "宗地乙", "重叠面积"])
        for (a, b), area in sorted(ledger.old_pairs.items()):
            if area > eps:
                writer.writerow(["旧批", a, b, _fmt(area, digits)])
        for (a, b), area in sorted(ledger.new_pairs.items()):
            if area > eps:
                writer.writerow(["新批", a, b, _fmt(area, digits)])
    written.append(dispute_path)
    return written


def summary(ledger: AreaLedger, eps: float = 1e-9, digits: int = 4) -> str:
    """Human-readable recap, including the per-parcel balance check."""
    lines = [
        f"旧批宗地 {len(ledger.old_ids)} 宗, 新批宗地 {len(ledger.new_ids)} 宗, "
        f"有效转移 {sum(1 for a in ledger.transfer.values() if a > eps)} 笔",
    ]
    worst = 0.0
    for row in old_rows(ledger, eps):
        worst = max(worst, abs(float(row["check"])))
        lines.append(
            "旧 {old_id}: 原面积 {area:.{d}f} = 去向 {outgoing:.{d}f} + 退界 "
            "{retreat:.{d}f} + 争议 {dispute:.{d}f} + 新批重叠 {new_overlap:.{d}f}"
            " (差 {check:+.2e})".format(d=digits, **row)
        )
    for row in new_rows(ledger, eps):
        worst = max(worst, abs(float(row["check"])))
        lines.append(
            "新 {new_id}: 新面积 {area:.{d}f} = 来源 {received:.{d}f} + 新增填土 "
            "{fill:.{d}f} + 涉旧争议 {old_dispute:.{d}f} + 新批重叠 "
            "{new_dispute:.{d}f} (差 {check:+.2e})".format(d=digits, **row)
        )
    lines.append(f"最大校验差: {worst:.3e}")
    return "\n".join(lines)
