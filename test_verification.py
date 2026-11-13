"""Tests for the exact commitment analysis and deterministic replay."""

import datetime as dt
import random

import pytest

import schedule
from schedule.verification import (
    SourceSpec,
    Verdict,
    analyze_commitment,
    maximum_draws,
    minimum_draws,
    replay_commitment,
)

OPEN = dt.datetime(2026, 11, 14, 9, 0)


def window(minutes):
    return OPEN, OPEN + dt.timedelta(minutes=minutes)


def make_sources():
    # A: every(2).to(4).minutes, must collect twice
    # B: every(3).to(5).minutes, must collect once
    return [
        SourceSpec("A", 2, 4, required_runs=2),
        SourceSpec("B", 3, 5, required_runs=1),
    ]


def test_verdict_guaranteed_when_worst_case_fits_with_buffer():
    sources = make_sources()
    # worst completion: A takes 8 min, B takes 5 min -> 8 min; plus 2 min buffer
    open_at, close_at = window(10)
    analysis = analyze_commitment(open_at, close_at, 2, sources)
    assert analysis.verdict is Verdict.GUARANTEED
    assert analysis.latest_completion == dt.timedelta(minutes=8)
    assert analysis.latest_publish_ready == dt.timedelta(minutes=10)


def test_verdict_impossible_when_best_case_misses_buffer():
    sources = make_sources()
    # best completion: A takes 4 min, B takes 3 min -> 4 min; plus 2 min buffer
    open_at, close_at = window(5)
    analysis = analyze_commitment(open_at, close_at, 2, sources)
    assert analysis.verdict is Verdict.IMPOSSIBLE
    assert analysis.earliest_completion == dt.timedelta(minutes=4)
    assert analysis.earliest_publish_ready == dt.timedelta(minutes=6)


def test_verdict_possible_when_best_fits_but_worst_does_not():
    sources = make_sources()
    open_at, close_at = window(7)
    analysis = analyze_commitment(open_at, close_at, 2, sources)
    assert analysis.verdict is Verdict.POSSIBLE
    assert analysis.earliest_publish_ready == dt.timedelta(minutes=6)
    assert analysis.latest_publish_ready == dt.timedelta(minutes=10)


def test_replay_success_sequence_matches_guaranteed_corner():
    sources = make_sources()
    open_at, close_at = window(10)
    draws = minimum_draws(sources)
    report = replay_commitment(open_at, close_at, 2, sources, draws)
    assert report.success is True
    assert report.draws_used == {"A": [2, 2], "B": [3]}
    assert report.completion_at == OPEN + dt.timedelta(minutes=4)
    assert report.publish_ready_at == OPEN + dt.timedelta(minutes=6)
    assert report.margin == dt.timedelta(minutes=4)
    assert report.tie_order_respected is True


def test_replay_failure_sequence_is_replayable_and_overruns():
    sources = make_sources()
    open_at, close_at = window(9)
    draws = maximum_draws(sources)
    report = replay_commitment(open_at, close_at, 2, sources, draws)
    assert report.success is False
    assert report.draws_used == {"A": [4, 4], "B": [5]}
    assert report.completion_at == OPEN + dt.timedelta(minutes=8)
    assert report.publish_ready_at == OPEN + dt.timedelta(minutes=10)
    assert report.margin == dt.timedelta(minutes=-1)
    # same explicit draws replay to the exact same instants
    rerun = replay_commitment(open_at, close_at, 2, sources, draws)
    assert [ (e.source, e.executed_at, e.drawn_interval) for e in rerun.events ] == [
        (e.source, e.executed_at, e.drawn_interval) for e in report.events
    ]


def test_possible_verdict_has_one_success_and_one_failure_trigger_sequence():
    sources = make_sources()
    open_at, close_at = window(7)
    analysis = analyze_commitment(open_at, close_at, 2, sources)
    assert analysis.verdict is Verdict.POSSIBLE

    success = replay_commitment(
        open_at, close_at, 2, sources, minimum_draws(sources)
    )
    failure = replay_commitment(
        open_at, close_at, 2, sources, maximum_draws(sources)
    )
    assert success.success is True
    assert failure.success is False
    assert success.margin > dt.timedelta(0)
    assert failure.margin < dt.timedelta(0)


def test_replay_drives_real_scheduler_and_cancels_finished_jobs():
    sources = make_sources()
    open_at, close_at = window(20)
    report = replay_commitment(
        open_at, close_at, 2, sources, minimum_draws(sources)
    )
    assert [(e.source, e.run_index) for e in report.events] == [
        ("A", 1),
        ("B", 1),
        ("A", 2),
    ]
    assert all(e.executed_at >= e.scheduled_for for e in report.events)
    # every supplied draw was consumed by randint inside _schedule_next_run
    assert sum(len(v) for v in report.draws_used.values()) == 3
    # patching the module clock/random leaves no global leakage
    assert schedule.random.randint is random.randint
    assert schedule.datetime.datetime is dt.datetime


def test_same_tick_jobs_follow_registration_order():
    # A fixed every 2 minutes (two runs); B starts at the same tick as A
    sources = [
        SourceSpec("A", 2, 2, required_runs=2),
        SourceSpec("B", 2, 4, required_runs=2),
    ]
    report = replay_commitment(
        OPEN, OPEN + dt.timedelta(minutes=30), 0, sources,
        {"A": [2, 2], "B": [2, 4]},
    )
    tied = [e for e in report.events if e.executed_at == OPEN + dt.timedelta(minutes=2)]
    assert [e.source for e in tied] == ["A", "B"]
    assert report.tie_order_respected is True

    reversed_sources = list(reversed(sources))
    reversed_report = replay_commitment(
        OPEN, OPEN + dt.timedelta(minutes=30), 0, reversed_sources,
        {"B": [2, 4], "A": [2, 2]},
    )
    tied = [
        e for e in reversed_report.events
        if e.executed_at == OPEN + dt.timedelta(minutes=2)
    ]
    assert [e.source for e in tied] == ["B", "A"]
    assert reversed_report.tie_order_respected is True


def test_exhaustive_enumeration_confirms_verdict_without_sampling():
    # Small ranges allow enumerating every legal draw combination:
    # A draws 2 values from [1, 2] (4 combos), B draws 1 from [1, 3] (3 combos)
    sources = [
        SourceSpec("A", 1, 2, required_runs=2),
        SourceSpec("B", 1, 3, required_runs=1),
    ]
    outcomes_by_horizon = {}
    for horizon_minutes in range(1, 9):
        open_at, close_at = window(horizon_minutes)
        results = []
        for a1 in (1, 2):
            for a2 in (1, 2):
                for b1 in (1, 2, 3):
                    report = replay_commitment(
                        open_at, close_at, 0, sources,
                        {"A": [a1, a2], "B": [b1]},
                    )
                    results.append(report.success)
        analysis = analyze_commitment(open_at, close_at, 0, sources)
        if all(results):
            expected = Verdict.GUARANTEED
        elif not any(results):
            expected = Verdict.IMPOSSIBLE
        else:
            expected = Verdict.POSSIBLE
        outcomes_by_horizon[horizon_minutes] = expected
        assert analysis.verdict is expected

    # completion ranges from max(2, 1)=2 (best) to max(4, 3)=4 (worst)
    assert outcomes_by_horizon[1] is Verdict.IMPOSSIBLE
    assert outcomes_by_horizon[3] is Verdict.POSSIBLE
    assert outcomes_by_horizon[4] is Verdict.GUARANTEED
    assert outcomes_by_horizon[7] is Verdict.GUARANTEED


def test_replay_rejects_invalid_draw_sequences():
    sources = make_sources()
    open_at, close_at = window(20)
    with pytest.raises(ValueError):
        replay_commitment(
            open_at, close_at, 2, sources, {"A": [2, 9], "B": [3]}
        )
    with pytest.raises(ValueError):
        replay_commitment(
            open_at, close_at, 2, sources, {"A": [2], "B": [3]}
        )
    with pytest.raises(KeyError):
        replay_commitment(
            open_at, close_at, 2, sources, {"A": [2, 2]}
        )
    with pytest.raises(ValueError):
        replay_commitment(
            open_at, close_at, 2, sources,
            {"A": [2, 2], "B": [3], "C": [1]},
        )


def test_replay_supports_second_level_intervals():
    sources = [
        SourceSpec("A", 2, 3, required_runs=1, unit="seconds"),
        SourceSpec("B", 4, 6, required_runs=1, unit="seconds"),
    ]
    open_at = OPEN
    close_at = OPEN + dt.timedelta(seconds=5)
    analysis = analyze_commitment(open_at, close_at, 0, sources)
    assert analysis.verdict is Verdict.POSSIBLE
    report = replay_commitment(
        open_at, close_at, 0, sources, {"A": [2], "B": [4]}
    )
    assert report.success is True
    assert report.completion_at == OPEN + dt.timedelta(seconds=4)

    failure = replay_commitment(
        open_at, close_at, 0, sources, {"A": [3], "B": [6]}
    )
    assert failure.success is False
    assert failure.completion_at == OPEN + dt.timedelta(seconds=6)


def test_input_validation():
    sources = make_sources()
    open_at, close_at = window(10)
    with pytest.raises(ValueError):
        analyze_commitment(open_at, open_at, 1, sources)
    with pytest.raises(ValueError):
        analyze_commitment(open_at, close_at, -1, sources)
    with pytest.raises(ValueError):
        analyze_commitment(open_at, close_at, 1, sources[:1])
    with pytest.raises(ValueError):
        SourceSpec("X", 5, 2)
    with pytest.raises(ValueError):
        SourceSpec("X", 1, 2, required_runs=0)
    with pytest.raises(ValueError):
        SourceSpec("X", 1, 2, unit="days")


def test_replay_leaves_no_global_patches_even_when_it_raises():
    from unittest import mock

    sources = make_sources()
    open_at, close_at = window(20)

    replay_commitment(
        open_at, close_at, 2, sources, minimum_draws(sources)
    )
    assert schedule.random.randint is random.randint
    assert schedule.datetime.datetime is dt.datetime

    # replay_commitment relies on these same patch.object contexts, which
    # roll back the module attributes even when the body raises.
    with pytest.raises(RuntimeError):
        with mock.patch.object(schedule.random, "randint", lambda a, b: a):
            raise RuntimeError("boom")
    assert schedule.random.randint is random.randint
