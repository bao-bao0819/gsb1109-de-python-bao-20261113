"""Welding round planner.

Rules implemented:
- Duplicate/overlapping reported work segments are counted once (union).
- Adjacent segments separated by strictly less than ``max_gap`` seconds may
  be joined into one round, but the cumulative gap filled into a single
  round may not exceed ``max_fill`` seconds.
- A round may never cross a shift handover boundary.
- The day's segments are partitioned into the minimum number of rounds;
  among plans with the same number of rounds, the one with the smallest
  total filled-gap time wins.
- Each round reports its real work seconds and its filled-gap seconds.
"""

from __future__ import annotations

import bisect
import json
import sys
from dataclasses import dataclass

MAX_GAP = 2.0   # gaps strictly below this (seconds) may be bridged
MAX_FILL = 3.0  # per-round cumulative bridged-gap cap (seconds)
EPS = 1e-9


@dataclass
class Round:
    start: float
    end: float
    work_seconds: float
    fill_seconds: float

    def as_dict(self) -> dict:
        return {
            "start": _num(self.start),
            "end": _num(self.end),
            "work_seconds": _num(self.work_seconds),
            "fill_seconds": _num(self.fill_seconds),
        }


def _num(value: float):
    return int(value) if float(value).is_integer() else value


def _normalize(intervals):
    """Merge duplicate/overlapping/touching intervals into a union."""
    spans = sorted((float(s), float(e)) for s, e in intervals if e > s)
    merged = []
    for start, end in spans:
        if merged and start <= merged[-1][1] + EPS:
            if end > merged[-1][1]:
                merged[-1][1] = end
        else:
            merged.append([start, end])
    return [tuple(span) for span in merged]


def _clip_to_spans(segments, boundaries):
    """Split segments at shift boundaries; return per-shift-span segments."""
    cuts = sorted(set(float(b) for b in (boundaries or [])))
    pieces = []
    for seg_start, seg_end in segments:
        piece_start = seg_start
        for cut in cuts:
            if piece_start < cut < seg_end - EPS:
                pieces.append((piece_start, cut))
                piece_start = cut
        pieces.append((piece_start, seg_end))
    grouped = []
    for piece in pieces:
        span_index = bisect.bisect_right(cuts, piece[0])
        if grouped and grouped[-1][0] == span_index:
            grouped[-1][1].append(piece)
        else:
            grouped.append((span_index, [piece]))
    return [group for _, group in grouped]


def _plan_span(segments, max_gap, max_fill):
    """Optimally partition one shift span of disjoint, sorted segments."""
    n = len(segments)
    gaps = [segments[i + 1][0] - segments[i][1] for i in range(n - 1)]
    dp = [None] * (n + 1)  # (rounds, total_fill) for the first i segments
    dp[0] = (0, 0.0)
    choice = [-1] * (n + 1)
    for i in range(1, n + 1):
        best = None
        fill = 0.0
        for j in range(i - 1, -1, -1):
            if j < i - 1:
                gap = gaps[j]
                fill += gap
                if gap >= max_gap - EPS or fill > max_fill + EPS:
                    break
            candidate = (dp[j][0] + 1, dp[j][1] + fill)
            if best is None or candidate < best:
                best = candidate
                choice[i] = j
        dp[i] = best
    rounds = []
    i = n
    while i > 0:
        j = choice[i]
        work = sum(end - start for start, end in segments[j:i])
        fill = segments[i - 1][1] - segments[j][0] - work
        rounds.append(
            Round(
                start=segments[j][0],
                end=segments[i - 1][1],
                work_seconds=work,
                fill_seconds=max(fill, 0.0),
            )
        )
        i = j
    rounds.reverse()
    return rounds


def plan_rounds(intervals, shift_boundaries=None, max_gap=MAX_GAP, max_fill=MAX_FILL):
    """Partition a day's welding segments into an optimal list of rounds."""
    segments = _normalize(intervals)
    rounds = []
    for span_segments in _clip_to_spans(segments, shift_boundaries):
        rounds.extend(_plan_span(span_segments, max_gap, max_fill))
    return rounds


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    if argv:
        with open(argv[0], "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    else:
        payload = json.load(sys.stdin)
    rounds = plan_rounds(
        payload.get("intervals", []),
        shift_boundaries=payload.get("shift_boundaries"),
    )
    result = {
        "rounds": [r.as_dict() for r in rounds],
        "total_rounds": len(rounds),
        "total_work_seconds": _num(sum(r.work_seconds for r in rounds)),
        "total_fill_seconds": _num(sum(r.fill_seconds for r in rounds)),
    }
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
