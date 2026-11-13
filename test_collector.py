"""Time-replay tests for the hourly collector.

All scenarios run on fake clocks: the wall clock and the monotonic
clock are advanced separately, so NTP setbacks and machine sleep are
replayed deterministically. Crossing midnight costs a few simulated
seconds — the tests never wait for the real midnight.
"""

import datetime

from collector import MIN_REQUEST_INTERVAL_SECONDS
from time_replay import Replay

# A few seconds before (fake) midnight, so hour rollover is cheap.
BEFORE_MIDNIGHT = datetime.datetime(2026, 11, 13, 23, 59, 30)


def slot(day, hour):
    return datetime.datetime(2026, 11, day, hour, 0, 0)


def test_dispatches_on_the_hour_with_business_time_and_interval():
    replay = Replay(BEFORE_MIDNIGHT)
    replay.run(90)  # crosses (fake) midnight

    first, second = replay.collector.records
    # Late start: the current hour's slot is caught up immediately.
    assert first.slot == slot(13, 23)
    assert first.business_time == datetime.datetime(2026, 11, 13, 23, 59, 31)
    assert first.elapsed_since_last is None
    # The midnight slot fires on the wall-clock hour boundary.
    assert second.slot == slot(14, 0)
    assert second.business_time == datetime.datetime(2026, 11, 14, 0, 0, 0)
    assert second.elapsed_since_last == 29.0


def test_monotonic_gate_delays_dispatch_past_the_hour_boundary():
    replay = Replay(datetime.datetime(2026, 11, 13, 23, 59, 55))
    replay.run(20)

    first, second = replay.collector.records
    assert first.slot == slot(13, 23)
    # Wall clock reaches 00:00:00 only 4s after the first dispatch, so
    # the midnight slot waits until 10 monotonic seconds have elapsed.
    assert second.slot == slot(14, 0)
    assert second.business_time == datetime.datetime(2026, 11, 14, 0, 0, 6)
    assert second.elapsed_since_last == MIN_REQUEST_INTERVAL_SECONDS


def test_ntp_setback_neither_duplicates_nor_loses_slots():
    replay = Replay(datetime.datetime(2026, 11, 14, 9, 59, 50))
    replay.run(20)  # crosses 10:00, collects slots 09:00 and 10:00
    replay.setback(600)  # NTP pulls the wall clock back 10 minutes
    replay.run(900)  # wall clock crosses 10:00 a second time

    records = replay.collector.records
    assert [r.slot for r in records] == [slot(14, 9), slot(14, 10)]
    assert records[1].business_time == datetime.datetime(2026, 11, 14, 10, 0, 1)
    # Every dispatch respects the 10s monotonic gap despite the setback.
    for record in records[1:]:
        assert record.elapsed_since_last >= MIN_REQUEST_INTERVAL_SECONDS


def test_sleep_advances_wall_clock_but_not_elapsed_time():
    replay = Replay(datetime.datetime(2026, 11, 14, 22, 59, 50))
    replay.run(20)  # collects slots 22:00 and 23:00
    replay.sleep(3600)  # suspended across the midnight boundary
    replay.run(30)

    third = replay.collector.records[2]
    # Wall clock jumped an hour past midnight; monotonic time did not.
    assert third.slot == slot(15, 0)
    assert third.business_time == datetime.datetime(2026, 11, 15, 0, 0, 11)
    assert third.elapsed_since_last == MIN_REQUEST_INTERVAL_SECONDS


def _replay_with_sleep_chunks(chunks):
    replay = Replay(BEFORE_MIDNIGHT)
    replay.run(100)  # collects slots 23:00 and 00:00
    for chunk in chunks:
        replay.sleep(chunk)  # suspended: collection does not resume here
    replay.run(100)
    return replay.collector.records


def test_split_sleep_yields_identical_claim_records():
    whole = _replay_with_sleep_chunks([5400])
    split = _replay_with_sleep_chunks([1800, 1800, 1800])

    assert whole == split
    assert [r.slot for r in whole] == [slot(13, 23), slot(14, 0), slot(14, 1)]
    # The post-sleep dispatch catches up the 01:00 slot at 01:31:11 wall
    # time, with only 71 monotonic seconds elapsed since the previous one.
    assert whole[2].business_time == datetime.datetime(2026, 11, 14, 1, 31, 11)
    assert whole[2].elapsed_since_last == 71.0
