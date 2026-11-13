import pytest

from welding_rounds import plan_rounds


def test_duplicate_reports_counted_once():
    rounds = plan_rounds([(0, 10), (0, 10), (5, 12), (3, 8)])
    assert len(rounds) == 1
    assert rounds[0].work_seconds == 12
    assert rounds[0].fill_seconds == 0


def test_gap_under_two_seconds_merges():
    rounds = plan_rounds([(0, 10), (11.5, 15)])
    assert len(rounds) == 1
    assert rounds[0].work_seconds == pytest.approx(13.5)
    assert rounds[0].fill_seconds == pytest.approx(1.5)


def test_gap_of_exactly_two_seconds_does_not_merge():
    rounds = plan_rounds([(0, 10), (12, 15)])
    assert len(rounds) == 2
    assert all(r.fill_seconds == 0 for r in rounds)


def test_five_one_second_gaps_do_not_chain_into_one_round():
    segments = [(i * 2, i * 2 + 1) for i in range(6)]  # six 1s segments, 1s gaps
    rounds = plan_rounds(segments)
    assert len(rounds) == 2
    assert all(r.fill_seconds <= 3 for r in rounds)
    assert sum(r.fill_seconds for r in rounds) == 4
    assert sum(r.work_seconds for r in rounds) == 6


def test_fill_cap_of_three_seconds_per_round():
    segments = [(0, 5), (6, 10), (11.5, 15), (16.5, 20)]  # gaps: 1, 1.5, 1.5
    rounds = plan_rounds(segments)
    assert len(rounds) == 2
    assert all(r.fill_seconds <= 3 for r in rounds)
    assert sum(r.fill_seconds for r in rounds) == pytest.approx(2.5)


def test_shift_boundary_never_crossed():
    rounds = plan_rounds([(0, 10), (11, 15)], shift_boundaries=[10.5])
    assert len(rounds) == 2
    assert rounds[0].end <= 10.5 <= rounds[1].start


def test_segment_spanning_boundary_is_clipped():
    rounds = plan_rounds([(0, 20)], shift_boundaries=[10])
    assert len(rounds) == 2
    assert (rounds[0].start, rounds[0].end) == (0, 10)
    assert (rounds[1].start, rounds[1].end) == (10, 20)
    assert sum(r.work_seconds for r in rounds) == 20


def test_boundary_between_distant_segments_forces_split():
    rounds = plan_rounds([(0, 5), (6, 10)], shift_boundaries=[5.5])
    assert len(rounds) == 2


def test_min_rounds_then_min_total_fill():
    # gaps 1.2, 0.8, 1.2 -> one round would need 3.2s fill (> 3)
    segments = [(0, 1), (2.2, 3.2), (4.0, 5.0), (6.2, 7.2)]
    rounds = plan_rounds(segments)
    assert len(rounds) == 2
    assert sum(r.fill_seconds for r in rounds) == pytest.approx(2.0)


def test_fill_budget_forces_early_split():
    # gaps 1.5, 1.5, 1.5: cheapest 2-round plan fills only 3.0s total
    segments = [(0, 1), (2.5, 3.5), (5, 6), (7.5, 8.5)]
    rounds = plan_rounds(segments)
    assert len(rounds) == 2
    assert sum(r.fill_seconds for r in rounds) == pytest.approx(3.0)


def test_empty_input():
    assert plan_rounds([]) == []


def test_zero_length_segments_ignored():
    rounds = plan_rounds([(5, 5), (0, 4)])
    assert len(rounds) == 1
    assert rounds[0].work_seconds == 4


def test_round_reports_work_and_fill():
    (r,) = plan_rounds([(0, 10), (11, 15)])
    assert (r.start, r.end) == (0, 15)
    assert r.work_seconds == 14
    assert r.fill_seconds == 1
