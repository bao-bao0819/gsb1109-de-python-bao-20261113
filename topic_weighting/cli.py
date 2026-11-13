"""Command line interface for the weighted top-three classification."""

import argparse
import json
import sys

from .analysis import classify, rank_table, weighted_score, TOP_K
from .io_utils import load_records, load_ranges, parse_ranges

VERDICT_LABELS = {
    "guaranteed": "稳进前三",
    "possible": "有机会",
    "impossible": "无缘前三",
}


def build_parser():
    parser = argparse.ArgumentParser(
        prog="topic-weighting",
        description=(
            "Classify manuscripts as guaranteed / possible / impossible for a "
            "top-three place over every admissible non-negative weight vector "
            "that sums to one."
        ),
    )
    parser.add_argument("records", help="CSV or JSON export of per-field relevance")
    parser.add_argument(
        "--ranges",
        help="file with three editorial ranges (CSV field,low,high or JSON)",
    )
    parser.add_argument(
        "--range",
        dest="range_args",
        action="append",
        default=[],
        metavar="LOW,HIGH",
        help="editorial range for one field; repeat three times",
    )
    parser.add_argument(
        "--rank-with",
        metavar="W1,W2,W3",
        help="also print the full ranking under an explicit admissible weight vector",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    return parser


def _resolve_ranges(args):
    if args.ranges:
        return load_ranges(args.ranges)
    if args.range_args:
        return parse_ranges(args.range_args)
    raise SystemExit("provide editorial ranges via --ranges FILE or three --range LOW,HIGH flags")


def main(argv=None):
    args = build_parser().parse_args(argv)
    records, header = load_records(args.records)
    lows, highs = _resolve_ranges(args)
    verdicts = classify(records, lows, highs)

    if args.rank_with:
        weights = tuple(float(part) for part in args.rank_with.split(","))
        if len(weights) != 3:
            raise SystemExit("--rank-with needs exactly three comma-separated weights")
        order = rank_table(records, weights)
        ranking = [
            {
                "rank": index + 1,
                "number": number,
                "score": weighted_score(
                    next(rec["relevance"] for rec in records if rec["number"] == number),
                    weights,
                ),
            }
            for index, number in enumerate(order)
        ]
    else:
        ranking = None
        weights = None

    if args.json:
        payload = {
            "top_k": TOP_K,
            "ranges": [
                {"field": index + 1, "low": lows[index], "high": highs[index]}
                for index in range(3)
            ],
            "verdicts": verdicts,
        }
        if ranking is not None:
            payload["explicit_weights"] = list(weights)
            payload["ranking"] = ranking
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    _print_text(records, header, lows, highs, verdicts, weights, ranking)
    return 0


def _print_text(records, header, lows, highs, verdicts, explicit_weights, ranking):
    print("编辑部批准的权重范围（权重非负、和为 1，所有稿共用同一组权重）:")
    for index in range(3):
        label = header[index + 1] if len(header) > index + 1 else "字段{}".format(index + 1)
        print("  {}: [{:.4g}, {:.4g}]".format(label, lows[index], highs[index]))
    print()
    print("稿号  判定       见证权重 (w1, w2, w3)              该权重下名次")
    for entry in verdicts:
        label = VERDICT_LABELS[entry["verdict"]]
        if entry["weights"] is None:
            weights_text = "-"
            rank_text = "-"
        else:
            weights_text = "({:.6f}, {:.6f}, {:.6f})".format(*entry["weights"])
            rank_text = str(entry["witness_rank"])
        print("{:>4}  {:<8}  {:<34}  {}".format(entry["number"], label, weights_text, rank_text))
    print()
    guaranteed = [entry["number"] for entry in verdicts if entry["verdict"] == "guaranteed"]
    possible = [entry["number"] for entry in verdicts if entry["verdict"] == "possible"]
    impossible = [entry["number"] for entry in verdicts if entry["verdict"] == "impossible"]
    print("无论如何调权都稳进前三: {}".format(guaranteed or "无"))
    print("只是有机会: {}".format(possible or "无"))
    print("无缘前三: {}".format(impossible or "无"))
    if ranking is not None:
        print()
        print("显式权重 {} 下的完整榜单（平分按稿号升序）:".format(
            tuple(round(value, 6) for value in explicit_weights)
        ))
        for row in ranking:
            print("  {}. 稿号 {:<4} 得分 {:.6f}".format(row["rank"], row["number"], row["score"]))


if __name__ == "__main__":
    sys.exit(main())
