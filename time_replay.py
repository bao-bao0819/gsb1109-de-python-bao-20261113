"""Deterministic time-replay harness with two separately driven clocks.

``ReplayClock`` exposes a wall clock and a monotonic clock that can be
advanced independently:

- ``run``     : normal execution, both clocks advance together.
- ``sleep``   : machine suspended, wall clock jumps, monotonic frozen.
- ``setback`` : NTP correction, wall clock moves backwards, monotonic
                untouched.

``Replay`` couples a clock with an ``HourlyCollector`` and ticks the
collector once per simulated polling step while running. No ticks
happen during ``sleep`` or ``setback`` — the machine is not collecting
then — which is what makes split workflows replayable: the dispatch
records depend only on the clock readings at tick points, not on how
a suspension was chunked.
"""

from __future__ import annotations

import datetime
from typing import List

from collector import DispatchRecord, HourlyCollector


class ReplayClock:
    def __init__(self, wall_start: datetime.datetime):
        self.wall = wall_start
        self.mono = 0.0

    def wall_now(self) -> datetime.datetime:
        return self.wall

    def monotonic_now(self) -> float:
        return self.mono

    def run(self, seconds: float) -> None:
        self.wall += datetime.timedelta(seconds=seconds)
        self.mono += seconds

    def sleep(self, seconds: float) -> None:
        self.wall += datetime.timedelta(seconds=seconds)

    def setback(self, seconds: float) -> None:
        self.wall -= datetime.timedelta(seconds=seconds)


class Replay:
    def __init__(self, wall_start: datetime.datetime, **collector_kwargs):
        self.clock = ReplayClock(wall_start)
        self.collector = HourlyCollector(
            self.clock.wall_now, self.clock.monotonic_now, **collector_kwargs
        )

    def run(self, seconds: float, step: float = 1.0) -> None:
        """Simulate normal execution, ticking the collector every step."""
        remaining = float(seconds)
        while remaining > 1e-9:
            delta = min(step, remaining)
            self.clock.run(delta)
            remaining -= delta
            self.collector.tick()

    def sleep(self, seconds: float) -> None:
        """Suspend the machine: wall jumps, monotonic frozen, no ticks."""
        self.clock.sleep(seconds)

    def setback(self, seconds: float) -> None:
        """NTP correction: wall moves backwards, monotonic untouched."""
        self.clock.setback(seconds)


def format_dispatch_log(records: List[DispatchRecord]) -> str:
    lines = [f"{'#':>2}  {'slot':<19}  {'business time':<19}  {'elapsed(s)':>10}"]
    for index, record in enumerate(records, 1):
        elapsed = (
            "-"
            if record.elapsed_since_last is None
            else f"{record.elapsed_since_last:.1f}"
        )
        lines.append(
            f"{index:>2}  {record.slot:%Y-%m-%d %H:%M:%S}  "
            f"{record.business_time:%Y-%m-%d %H:%M:%S}  {elapsed:>10}"
        )
    return "\n".join(lines)


def _demo() -> None:
    start = datetime.datetime(2026, 11, 13, 23, 59, 30)

    print("== NTP setback: wall crosses 10:00 twice ==")
    replay = Replay(datetime.datetime(2026, 11, 14, 9, 59, 50))
    replay.run(20)
    replay.setback(600)
    replay.run(900)
    print(format_dispatch_log(replay.collector.records))

    print()
    print("== Machine sleep across midnight, one chunk vs three ==")
    runs = []
    for chunks in ([5400], [1800, 1800, 1800]):
        replay = Replay(start)
        replay.run(100)
        for chunk in chunks:
            replay.sleep(chunk)
        replay.run(100)
        runs.append(replay.collector.records)
        print(f"sleep chunks {chunks}:")
        print(format_dispatch_log(replay.collector.records))
        print()
    print("records identical:", runs[0] == runs[1])


if __name__ == "__main__":
    _demo()
