"""Command line interface: build an area-transfer table from two directories.

Usage:
    python -m cadastral_transfer OLD_DIR NEW_DIR [--out OUT_DIR]
"""

from __future__ import annotations

import argparse
import sys

from .engine import compute_area_ledger
from .parcels import load_parcels
from .report import summary, write_reports


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cadastral_transfer",
        description=(
            "从两个地籍交付批次的几何目录产生新旧宗地面积转移表; "
            "按真实重叠计量, 不按编号延续身份"
        ),
    )
    parser.add_argument("old_dir", help="旧批次几何目录 (.geojson/.json/.wkt)")
    parser.add_argument("new_dir", help="新批次几何目录 (.geojson/.json/.wkt)")
    parser.add_argument(
        "--out", "-o", default="transfer_result", help="输出目录 (默认 transfer_result)"
    )
    parser.add_argument(
        "--eps",
        type=float,
        default=1e-9,
        help="面积阈值, 小于该值的重叠(如贴边)不计入 (默认 1e-9)",
    )
    parser.add_argument("--digits", type=int, default=4, help="输出小数位数 (默认 4)")
    args = parser.parse_args(argv)

    old_parcels = load_parcels(args.old_dir)
    new_parcels = load_parcels(args.new_dir)
    ledger = compute_area_ledger(old_parcels, new_parcels, eps=args.eps)
    written = write_reports(ledger, args.out, eps=args.eps, digits=args.digits)
    print(summary(ledger, eps=args.eps, digits=args.digits))
    print("已写出:")
    for path in written:
        print(f"  {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
