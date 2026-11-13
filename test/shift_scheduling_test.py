"""
Tests for workstation reconstruction from sparse production logs.
"""
from __future__ import absolute_import

import pytest

from intervaltree import IntervalTree
from shift_scheduling import JobLog, Reconstruction, verify


STATIONS = {
    "A": ["A1", "A2"],
    "B": ["B1"],
}


def build(jobs):
    return Reconstruction(jobs, STATIONS)


def test_half_open_touch_is_a_legal_hand_off():
    jobs = [
        JobLog("J1", 0, 10, "A"),
        JobLog("J2", 10, 20, "A"),
        JobLog("J3", 20, 30, "A"),
    ]
    engine = build(jobs)
    report = engine.classify()

    assert report["status"] == "feasible"
    # Two stations and no overlaps: every job may legally land on either.
    assert report["ambiguous"] == {
        "J1": ["A1", "A2"],
        "J2": ["A1", "A2"],
        "J3": ["A1", "A2"],
    }
    for schedule in report["solutions"]:
        assert verify(jobs, STATIONS, schedule) == []

    # Endpoint-to-endpoint chaining on one station is itself a legal schedule.
    chained = {"J1": "A1", "J2": "A1", "J3": "A1"}
    assert verify(jobs, STATIONS, chained) == []
    assert engine.utilization(chained) == {"A1": 1.0, "A2": 0.0, "B1": 0.0}


def test_overlapping_jobs_spread_across_stations_are_ambiguous():
    jobs = [
        JobLog("J1", 0, 10, "A"),
        JobLog("J2", 0, 10, "A"),
    ]
    report = build(jobs).classify()

    assert report["status"] == "feasible"
    assert report["unique"] == {}
    assert report["ambiguous"] == {"J1": ["A1", "A2"], "J2": ["A1", "A2"]}

    schedule_one, schedule_two = build(jobs).demonstrate_ambiguity("J1")
    assert verify(jobs, STATIONS, schedule_one) == []
    assert verify(jobs, STATIONS, schedule_two) == []
    assert schedule_one["J1"] != schedule_two["J1"]
    assert schedule_one["J2"] != schedule_two["J2"]


def test_inspection_pin_forces_unique_assignment():
    jobs = [
        JobLog("J1", 0, 10, "A", pinned_station="A1"),
        JobLog("J2", 0, 10, "A"),
        JobLog("J3", 10, 20, "A"),
    ]
    report = build(jobs).classify()

    assert report["unique"]["J1"] == "A1"
    assert report["unique"]["J2"] == "A2"
    assert report["ambiguous"]["J3"] == ["A1", "A2"]

    schedule_one, schedule_two = build(jobs).demonstrate_ambiguity("J3")
    assert verify(jobs, STATIONS, schedule_one) == []
    assert verify(jobs, STATIONS, schedule_two) == []
    assert schedule_one["J3"] != schedule_two["J3"]
    # The pinned job and the forced overlapping neighbour never move.
    assert schedule_one["J1"] == schedule_two["J1"] == "A1"
    assert schedule_one["J2"] == schedule_two["J2"] == "A2"


def test_demonstrate_ambiguity_rejects_unique_job():
    jobs = [JobLog("J1", 0, 10, "B")]
    with pytest.raises(ValueError):
        build(jobs).demonstrate_ambiguity("J1")


def test_overload_when_demand_exceeds_capacity():
    jobs = [
        JobLog("J1", 0, 10, "A"),
        JobLog("J2", 2, 12, "A"),
        JobLog("J3", 4, 8, "A"),
    ]
    engine = build(jobs)
    report = engine.classify()

    assert report["status"] == "infeasible"
    assert report["solutions"] == []
    assert report["unschedulable_jobs"] == ["J1", "J2", "J3"]

    windows = report["overload_windows"]["A"]
    assert [(w["begin"], w["end"]) for w in windows] == [(4, 8)]
    assert windows[0]["demand"] == 3
    assert windows[0]["capacity"] == 2
    assert windows[0]["jobs"] == ["J1", "J2", "J3"]


def test_partial_overload_window_boundaries():
    jobs = [
        JobLog("J1", 0, 20, "A"),
        JobLog("J2", 0, 20, "A"),
        JobLog("J3", 5, 15, "A"),
    ]
    report = build(jobs).classify()
    windows = report["overload_windows"]["A"]

    assert [(w["begin"], w["end"]) for w in windows] == [(5, 15)]


def test_mixed_shift_contains_all_three_categories():
    jobs = [
        JobLog("J1", 0, 10, "A", pinned_station="A1"),  # unique via inspection
        JobLog("J2", 0, 10, "A"),                        # forced onto A2, unique
        JobLog("J3", 10, 20, "A"),                       # free pick, ambiguous
        JobLog("J4", 0, 30, "B"),                        # only B station, unique
    ]
    report = build(jobs).classify()

    assert report["unique"] == {"J1": "A1", "J2": "A2", "J4": "B1"}
    assert report["ambiguous"] == {"J3": ["A1", "A2"]}
    assert report["unschedulable_jobs"] == []

    schedule = report["solutions"][0]
    assert verify(jobs, STATIONS, schedule) == []
    idle = report["idle_windows"]
    # A2 only serves the forced J2 [0,10); B1 runs J4 for the whole shift,
    # so at least one station is busy at every instant of the shift.
    assert idle["A2"] == [{"begin": 10, "end": 30}]
    assert idle["B1"] == []
    assert idle["__any_station__"] == []


def test_verify_flags_double_booking_and_wrong_type():
    jobs = [
        JobLog("J1", 0, 10, "A"),
        JobLog("J2", 5, 15, "A"),
    ]
    bad = {"J1": "A1", "J2": "A1"}
    violations = verify(jobs, STATIONS, bad)
    assert any("double-booked" in line for line in violations)

    wrong_type = {"J1": "B1", "J2": "A2"}
    violations = verify(jobs, STATIONS, wrong_type)
    assert any("needs type" in line for line in violations)


def test_interval_tree_touch_semantics_underpin_fit_test():
    tree = IntervalTree.from_tuples([(0, 10, "J1")])
    assert not tree.overlap(10, 20)
    assert tree.overlap(9, 11)
    engine = build([JobLog("J1", 0, 10, "A")])
    fresh = engine._fresh_trees()
    fresh["A1"].addi(0, 10, "J1")
    assert engine._fits(fresh["A1"], 10, 20)
    assert not engine._fits(fresh["A1"], 9, 11)


def test_conflicting_inspection_pins_are_rejected_upfront():
    jobs = [
        JobLog("J1", 0, 10, "A", pinned_station="A1"),
        JobLog("J2", 5, 15, "A", pinned_station="A1"),
    ]
    with pytest.raises(ValueError):
        build(jobs)


def test_every_enumerated_schedule_is_legal():
    jobs = [
        JobLog("J1", 0, 10, "A"),
        JobLog("J2", 0, 10, "A"),
        JobLog("J3", 10, 20, "A"),
        JobLog("J4", 5, 25, "B"),
    ]
    engine = build(jobs)
    schedules, truncated = engine.all_solutions(limit=100)
    assert not truncated
    assert len(schedules) == 4
    for schedule in schedules:
        assert verify(jobs, STATIONS, schedule) == []


def test_uniqueness_can_propagate_from_another_jobs_pin():
    jobs = [
        JobLog("J1", 0, 20, "A"),
        JobLog("J2", 0, 20, "A", pinned_station="A1"),
    ]
    report = build(jobs).classify()
    # J1 has no inspection record itself but overlaps the pinned J2 fully.
    assert report["unique"] == {"J1": "A2", "J2": "A1"}


def test_adjacent_overload_segments_merge_with_unioned_jobs():
    stations = {"A": ["A1"]}
    jobs = [
        JobLog("K1", 0, 20, "A"),
        JobLog("K2", 0, 10, "A"),
        JobLog("K3", 10, 20, "A"),
    ]
    report = Reconstruction(jobs, stations).classify()
    windows = report["overload_windows"]["A"]

    assert len(windows) == 1
    assert (windows[0]["begin"], windows[0]["end"]) == (0, 20)
    assert windows[0]["demand"] == 2
    assert windows[0]["jobs"] == ["K1", "K2", "K3"]


def test_pins_can_make_a_shift_infeasible_without_overload():
    # Overlap graph is the path J1-J2-J3 (clique size 2, so no overload).
    # A two-colouring forces the path endpoints onto the SAME station, but
    # the inspection pins demand different stations: precolouring fails.
    jobs = [
        JobLog("J1", 0, 10, "A", pinned_station="A1"),
        JobLog("J2", 5, 15, "A"),
        JobLog("J3", 10, 20, "A", pinned_station="A2"),
    ]
    report = build(jobs).classify()

    assert report["status"] == "infeasible"
    assert report["overload_windows"] == {}
    witness = report["pin_witness"]
    assert witness is not None

    unpinned = [
        JobLog(job.job_id, job.start, job.end, job.station_type) for job in jobs
    ]
    # The witness schedule is a legal placement once pins are ignored.
    assert verify(unpinned, STATIONS, witness["relaxed_schedule"]) == []
    # Checked against the raw log, the only complaints are violated pins.
    relaxed_violations = verify(jobs, STATIONS, witness["relaxed_schedule"])
    assert relaxed_violations and all(
        "pinned to" in line for line in relaxed_violations
    )
    assert report["unschedulable_jobs"] == ["J3"]
    assert witness["violated_pins"]["J3"]["pinned_to"] == "A2"


def test_csv_roundtrip(tmp_path):
    from shift_scheduling.logio import load_jobs, load_station_roster

    log_path = tmp_path / "log.csv"
    roster_path = tmp_path / "roster.csv"
    log_path.write_text(
        "job_id,start,end,station_type,pinned_station\n"
        "J1,0,10,A,A1\n"
        "J2,0,10,A,\n",
        encoding="utf-8",
    )
    roster_path.write_text(
        "station_type,station\nA,A1\nA,A2\nB,B1\n", encoding="utf-8"
    )

    jobs = load_jobs(str(log_path))
    roster = load_station_roster(str(roster_path))
    assert jobs[0] == JobLog("J1", 0, 10, "A", pinned_station="A1")
    assert jobs[1].pinned_station is None
    assert set(roster) == {"A", "B"}

    report = Reconstruction(jobs, roster).classify()
    assert report["unique"] == {"J1": "A1", "J2": "A2"}


def test_large_shift_classifies_fast():
    jobs = [
        JobLog("J%02d" % index, index, index + 2, "A")
        for index in range(40)
    ]
    engine = Reconstruction(jobs, STATIONS, solution_limit=100)
    report = engine.classify()

    # The sliding overlap chain forces the two stations to alternate, so only
    # two mirror schedules survive despite no inspection pins at all.
    assert report["status"] == "feasible"
    assert report["truncated"] is False
    assert len(report["solutions"]) == 2
    for schedule in report["solutions"]:
        assert verify(jobs, STATIONS, schedule) == []


def test_enumeration_cap_is_reported_when_solutions_explode():
    # Non-overlapping jobs are each free on both A stations: 2**10 schedules.
    jobs = [JobLog("J%02d" % i, i * 10, i * 10 + 5, "A") for i in range(10)]
    report = Reconstruction(jobs, STATIONS, solution_limit=100).classify()

    assert report["status"] == "feasible"
    assert report["truncated"] is True
    assert len(report["solutions"]) == 100
    for schedule in report["solutions"]:
        assert verify(jobs, STATIONS, schedule) == []
