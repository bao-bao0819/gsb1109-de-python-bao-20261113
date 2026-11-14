"""Tests for time-based version occupancy bounds."""

from datetime import datetime, timedelta

import pytest

from semver import (
    Observation,
    VersionDuration,
    VersionShare,
    VersionTimeReport,
    version_time_bounds,
)


def obs(device, timestamp, version):
    return Observation(device, timestamp, version)


def test_empty_input_reports_zero_time_and_no_versions():
    report = version_time_bounds([])

    assert isinstance(report, VersionTimeReport)
    assert report.total_online_time == 0
    assert report.durations == ()
    assert report.duration_for("1.0.0") == VersionDuration("1.0.0", 0, 0)
    assert report.share_for("1.0.0") == VersionShare("1.0.0", 0.0, 0.0)
    assert report.unknown_duration == VersionDuration(None, 0, 0)


def test_single_observation_contributes_no_duration():
    report = version_time_bounds([obs("d1", 5, "1.0.0")])

    assert report.total_online_time == 0
    assert report.duration_for("1.0.0") == VersionDuration("1.0.0", 0, 0)


def test_constant_known_version_is_fixed_full_gap():
    report = version_time_bounds(
        [obs("d1", 0, "1.0.0"), obs("d1", 10, "1.0.0")]
    )

    assert report.total_online_time == 10
    assert report.duration_for("1.0.0") == VersionDuration("1.0.0", 10, 10)
    assert report.share_for("1.0.0") == VersionShare("1.0.0", 1.0, 1.0)
    assert report.unknown_duration == VersionDuration(None, 0, 0)


def test_version_change_gap_is_free_between_endpoint_versions_only():
    report = version_time_bounds(
        [obs("d1", 0, "1.0.0"), obs("d1", 10, "2.0.0")]
    )

    assert report.total_online_time == 10
    assert report.duration_for("1.0.0") == VersionDuration("1.0.0", 0, 10)
    assert report.duration_for("2.0.0") == VersionDuration("2.0.0", 0, 10)
    assert report.unknown_duration == VersionDuration(None, 0, 0)
    assert report.share_for("2.0.0") == VersionShare("2.0.0", 0.0, 1.0)


def test_new_version_not_assumed_to_last_until_end():
    # 1.0.0 observed both before and after the 2.0.0 observation: the
    # upgrade and the filed rollback each live inside an uncertain gap.
    report = version_time_bounds(
        [
            obs("d1", 0, "1.0.0"),
            obs("d1", 4, "2.0.0"),
            obs("d1", 10, "1.0.0"),
        ]
    )

    assert report.total_online_time == 10
    # 2.0.0 only exists inside the two change gaps, never in a fixed one
    assert report.duration_for("2.0.0") == VersionDuration("2.0.0", 0, 10)
    assert report.duration_for("1.0.0") == VersionDuration("1.0.0", 0, 10)


def test_two_change_gaps_cannot_both_favor_same_version_beyond_feasible():
    # Two independent devices, each one A->B gap of length 10.
    report = version_time_bounds(
        [
            obs("d1", 0, "1.0.0"),
            obs("d1", 10, "2.0.0"),
            obs("d2", 0, "1.0.0"),
            obs("d2", 10, "2.0.0"),
        ]
    )

    assert report.total_online_time == 20
    assert report.duration_for("2.0.0") == VersionDuration("2.0.0", 0, 20)
    assert report.duration_for("1.0.0") == VersionDuration("1.0.0", 0, 20)
    # every gap must be fully allocated, so feasibility bounds stay tight
    share = report.share_for("2.0.0")
    assert share.lower_bound == 0.0
    assert share.upper_bound == 1.0


def test_mixed_fixed_and_free_gaps_give_partial_bounds():
    report = version_time_bounds(
        [
            obs("d1", 0, "1.0.0"),
            obs("d1", 10, "1.0.0"),  # fixed 10 on 1.0.0
            obs("d1", 20, "2.0.0"),  # free 10 between 1.0.0 and 2.0.0
            obs("d2", 0, "2.0.0"),
            obs("d2", 30, "2.0.0"),  # fixed 30 on 2.0.0
        ]
    )

    assert report.total_online_time == 50
    assert report.duration_for("1.0.0") == VersionDuration("1.0.0", 10, 20)
    assert report.duration_for("2.0.0") == VersionDuration("2.0.0", 30, 40)
    assert report.share_for("2.0.0") == VersionShare("2.0.0", 0.6, 0.8)


def test_unknown_versions_both_ends_fix_whole_gap_to_unknown():
    report = version_time_bounds(
        [obs("d1", 0, None), obs("d1", 10, None)]
    )

    assert report.total_online_time == 10
    assert report.unknown_duration == VersionDuration(None, 10, 10)
    assert report.durations == (VersionDuration(None, 10, 10),)


def test_known_then_unknown_gap_is_free_between_known_and_unknown_only():
    report = version_time_bounds(
        [obs("d1", 0, "1.0.0"), obs("d1", 10, None)]
    )

    assert report.total_online_time == 10
    assert report.duration_for("1.0.0") == VersionDuration("1.0.0", 0, 10)
    assert report.unknown_duration == VersionDuration(None, 0, 10)
    # another target version must not absorb this gap
    assert report.duration_for("2.0.0") == VersionDuration("2.0.0", 0, 0)


def test_unknown_then_known_gap_same_feasible_set():
    report = version_time_bounds(
        [obs("d1", 0, None), obs("d1", 10, "2.0.0")]
    )

    assert report.duration_for("2.0.0") == VersionDuration("2.0.0", 0, 10)
    assert report.unknown_duration == VersionDuration(None, 0, 10)


def test_unknown_bucket_partial_bounds_across_gap_kinds():
    report = version_time_bounds(
        [
            obs("d1", 0, None),
            obs("d1", 5, None),  # fixed 5 unknown
            obs("d1", 15, "1.0.0"),  # free 10 unknown/1.0.0
            obs("d2", 0, "1.0.0"),
            obs("d2", 20, "1.0.0"),  # fixed 20 on 1.0.0
        ]
    )

    assert report.total_online_time == 35
    assert report.unknown_duration == VersionDuration(None, 5, 15)
    assert report.duration_for("1.0.0") == VersionDuration("1.0.0", 20, 30)


def test_blank_version_string_treated_as_unknown():
    report = version_time_bounds(
        [obs("d1", 0, "  "), obs("d1", 10, "")]
    )

    assert report.unknown_duration == VersionDuration(None, 10, 10)


def test_bounds_partition_total_duration_in_any_extreme_allocation():
    report = version_time_bounds(
        [
            obs("d1", 0, "1.0.0"),
            obs("d1", 10, "2.0.0"),
            obs("d1", 20, None),
            obs("d2", 0, "2.0.0"),
            obs("d2", 15, "2.0.0"),
        ]
    )

    labels = [item.version for item in report.durations]
    assert labels == ["1.0.0", "2.0.0", None]
    # lower bounds never overlap and cannot exceed the total
    assert sum(item.lower_bound for item in report.durations) <= (
        report.total_online_time
    )
    # every gap is fully allocatable: uppers cover the total
    assert sum(item.upper_bound for item in report.durations) >= (
        report.total_online_time
    )


def test_observations_are_sorted_per_device_before_processing():
    report = version_time_bounds(
        [
            obs("d1", 20, "2.0.0"),
            obs("d1", 0, "1.0.0"),
            obs("d1", 10, "1.0.0"),
        ]
    )

    assert report.total_online_time == 20
    assert report.duration_for("1.0.0") == VersionDuration("1.0.0", 10, 20)
    assert report.duration_for("2.0.0") == VersionDuration("2.0.0", 0, 10)


def test_devices_are_grouped_independently():
    report = version_time_bounds(
        [
            obs("d1", 0, "1.0.0"),
            obs("d2", 0, "2.0.0"),
            obs("d1", 10, "1.0.0"),
            obs("d2", 10, "2.0.0"),
        ]
    )

    assert report.total_online_time == 20
    assert report.duration_for("1.0.0") == VersionDuration("1.0.0", 10, 10)
    assert report.duration_for("2.0.0") == VersionDuration("2.0.0", 10, 10)


def test_duplicate_timestamps_contribute_zero_duration():
    report = version_time_bounds(
        [
            obs("d1", 5, "1.0.0"),
            obs("d1", 5, "2.0.0"),
            obs("d1", 15, "2.0.0"),
        ]
    )

    assert report.total_online_time == 10
    assert report.duration_for("2.0.0") == VersionDuration("2.0.0", 10, 10)
    assert report.duration_for("1.0.0") == VersionDuration("1.0.0", 0, 0)


def test_tuple_inputs_and_datetime_timestamps():
    start = datetime(2026, 11, 14, 0, 0, 0)
    report = version_time_bounds(
        [
            ("d1", start, "1.0.0"),
            ("d1", start + timedelta(hours=2), "2.0.0"),
            ("d1", start + timedelta(hours=5), "2.0.0"),
        ]
    )

    assert report.total_online_time == timedelta(hours=5)
    assert report.duration_for("2.0.0") == VersionDuration(
        "2.0.0", timedelta(hours=3), timedelta(hours=5)
    )
    assert report.duration_for("1.0.0") == VersionDuration(
        "1.0.0", timedelta(0), timedelta(hours=2)
    )
    share = report.share_for("2.0.0")
    assert share.lower_bound == pytest.approx(0.6)
    assert share.upper_bound == 1.0


def test_chatty_device_does_not_inflate_share_by_message_count():
    # d1 reports 11 heartbeats over 10 time units on 2.0.0,
    # d2 reports twice over 10 time units on 1.0.0.
    d1 = [obs("d1", t, "2.0.0") for t in range(0, 11)]
    d2 = [obs("d2", 0, "1.0.0"), obs("d2", 10, "1.0.0")]
    report = version_time_bounds(d1 + d2)

    assert report.total_online_time == 20
    assert report.share_for("2.0.0") == VersionShare("2.0.0", 0.5, 0.5)
    assert report.share_for("1.0.0") == VersionShare("1.0.0", 0.5, 0.5)


def test_rollback_cycle_bounds_remain_consistent_on_one_timeline():
    report = version_time_bounds(
        [
            obs("d1", 0, "1.0.0"),
            obs("d1", 10, "2.0.0"),
            obs("d1", 20, "1.0.0"),
            obs("d1", 30, "1.0.0"),
        ]
    )

    assert report.total_online_time == 30
    # last gap is fixed to 1.0.0; first two are free
    assert report.duration_for("1.0.0") == VersionDuration("1.0.0", 10, 30)
    assert report.duration_for("2.0.0") == VersionDuration("2.0.0", 0, 20)
    assert report.unknown_duration == VersionDuration(None, 0, 0)


def test_invalid_version_type_raises_type_error():
    with pytest.raises(TypeError):
        version_time_bounds([obs("d1", 0, 123), obs("d1", 10, 123)])


def test_invalid_observation_shape_raises_type_error():
    with pytest.raises(TypeError):
        version_time_bounds([("d1", 0)])
    with pytest.raises(TypeError):
        version_time_bounds([42])


def test_observation_objects_are_not_mutated():
    rows = [obs("d1", 0, " 1.0.0 "), obs("d1", 10, None)]
    version_time_bounds(rows)
    assert rows[0].version == " 1.0.0 "
    assert rows[1].version is None
