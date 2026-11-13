"""Hourly collector driven by two independent clocks.

The collector reads a wall clock to decide whether the top of the hour
(the business boundary) has been reached, and a monotonic clock to
enforce a minimum gap of ten seconds between consecutive requests.
Both clocks are injected, so tests can replay NTP clock setbacks and
machine sleep deterministically.

Semantics:
- Each hour slot is collected at most once (deduplicated by slot), so a
  wall-clock setback never causes a slot to be claimed twice.
- If the collector wakes up in the middle of an hour (e.g. after sleep
  or a late start), it catches up the current hour's slot; slots whose
  hour has fully passed while suspended are not backfilled.
- A dispatch happens only when at least ``min_interval`` monotonic
  seconds have elapsed since the previous dispatch, regardless of what
  the wall clock says.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Callable, List, Optional

MIN_REQUEST_INTERVAL_SECONDS = 10.0


@dataclass
class DispatchRecord:
    """One dispatched request (领取记录)."""

    slot: datetime.datetime
    business_time: datetime.datetime
    elapsed_since_last: Optional[float]


class HourlyCollector:
    def __init__(
        self,
        wall_now: Callable[[], datetime.datetime],
        monotonic_now: Callable[[], float],
        min_interval: float = MIN_REQUEST_INTERVAL_SECONDS,
    ):
        self._wall_now = wall_now
        self._monotonic_now = monotonic_now
        self._min_interval = min_interval
        self._collected_slots: set[datetime.datetime] = set()
        self._last_dispatch_mono: Optional[float] = None
        self.records: List[DispatchRecord] = []

    def tick(self) -> Optional[DispatchRecord]:
        """Run one polling iteration; dispatch at most one request."""
        wall = self._wall_now()
        slot = wall.replace(minute=0, second=0, microsecond=0)
        if slot in self._collected_slots:
            return None
        mono = self._monotonic_now()
        if (
            self._last_dispatch_mono is not None
            and mono - self._last_dispatch_mono < self._min_interval
        ):
            return None
        elapsed = (
            None
            if self._last_dispatch_mono is None
            else mono - self._last_dispatch_mono
        )
        self._collected_slots.add(slot)
        self._last_dispatch_mono = mono
        record = DispatchRecord(
            slot=slot, business_time=wall, elapsed_since_last=elapsed
        )
        self.records.append(record)
        return record
