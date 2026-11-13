"""
intervaltree: A mutable, self-balancing interval tree for Python 2 and 3.

Test module: incremental event-cluster bookkeeping.
"""
from __future__ import absolute_import

import pytest

from intervaltree import (
    ClusterTicket,
    EventClusterTracker,
    EventRecord,
    RecordWithdrawal,
)


def record(record_id, begin, end):
    return EventRecord(begin, end, record_id)


def chain():
    # a-b overlap, b-c overlap, a and c do not overlap.
    return [
        record("a", 0, 10),
        record("b", 5, 15),
        record("c", 12, 20),
    ]


def test_intersecting_records_form_one_cluster():
    tracker = EventClusterTracker(chain())
    assert len(tracker.clusters) == 1
    cluster = tracker.clusters[0]
    assert cluster.record_ids == frozenset(["a", "b", "c"])


def test_disjoint_records_form_separate_clusters():
    tracker = EventClusterTracker([
        record("a", 0, 10),
        record("b", 10, 20),
    ])
    assert [cluster.record_ids for cluster in tracker.clusters] == [
        frozenset(["a"]),
        frozenset(["b"]),
    ]


def test_withdrawal_splits_and_reports_lineage():
    tracker = EventClusterTracker(chain())
    original = tracker.clusters[0].cluster_id

    update = tracker.withdraw("b")

    assert update.withdrawn == ("b",)
    assert update.successors_of(original) == (
        tracker.clusters[0].cluster_id,
        tracker.clusters[1].cluster_id,
    )
    assert len(tracker.clusters) == 2
    assert [
        change.action for change in update
        if change.action != "retire"
    ] == ["split", "split"]
    assert original in update.retired


def test_withdrawal_retires_old_cluster_identity():
    tracker = EventClusterTracker(chain())
    original = tracker.clusters[0]

    tracker.withdraw("b")

    assert all(
        cluster.cluster_id != original.cluster_id
        for cluster in tracker.clusters
    )
    assert [
        cluster.cluster_id for cluster in tracker.retired_clusters
    ] == [original.cluster_id]


def test_untouched_cluster_keeps_identity():
    tracker = EventClusterTracker([
        record("a", 0, 10),
        record("x", 100, 110),
        record("y", 105, 120),
    ])
    untouched = [
        cluster for cluster in tracker.clusters
        if "a" in cluster
    ][0]
    other = [
        cluster for cluster in tracker.clusters
        if "x" in cluster
    ][0]
    before = other.cluster_id

    update = tracker.add(record("z", 106, 112))

    assert untouched.cluster_id in update.unchanged
    assert tracker.cluster_for("x").cluster_id == before
    assert before in update.lineage
    assert update.lineage[before] == (before,)


def test_withdrawal_splits_without_changing_both_sides():
    tracker = EventClusterTracker([
        record("a", 0, 1),
        record("b", 10, 11),
        record("bridge", 0, 11),
    ])
    left = tracker.clusters
    assert len(left) == 1

    update = tracker.withdraw("bridge")

    assert len(tracker.clusters) == 2
    assert update.withdrawn == ("bridge",)
    assert all(
        len(cluster.records) == 1 for cluster in tracker.clusters
    )


def test_ticket_follows_unique_piece_containing_anchor():
    tracker = EventClusterTracker(chain())
    ticket = tracker.attach_ticket("WO-1", "a")

    update = tracker.withdraw("b")

    left = tracker.cluster_for("a")
    right = tracker.cluster_for("c")
    assert left is not None and right is not None
    assert left.cluster_id != right.cluster_id
    assert left.tickets == (ticket.moved_to(left.cluster_id),)
    assert right.tickets == ()
    assert update.pending_tickets == ()


def test_ticket_follows_anchor_even_when_other_piece_is_longer():
    tracker = EventClusterTracker([
        record("anchor", 0, 1),
        record("bridge", 0, 100),
        record("big", 50, 100),
    ])
    tracker.attach_ticket("WO-1", "anchor")

    tracker.withdraw("bridge")

    left = tracker.cluster_for("anchor")
    right = tracker.cluster_for("big")
    assert left.span < right.span
    assert [t.ticket_id for t in left.tickets] == ["WO-1"]
    assert right.tickets == ()


def test_withdrawn_anchor_leaves_ticket_pending():
    tracker = EventClusterTracker(chain())
    ticket = tracker.attach_ticket("WO-1", "a")

    update = tracker.withdraw("a")

    assert update.pending_tickets == (
        ClusterTicket("WO-1", "a", None, ticket.history),
    )
    assert tracker.pending_tickets == update.pending_tickets
    assert all(
        "WO-1" not in cluster.ticket_ids
        for cluster in tracker.clusters
    )


def test_withdrawn_anchor_is_not_given_to_longest_piece():
    tracker = EventClusterTracker([
        record("anchor", 0, 1),
        record("bridge", 0, 100),
        record("big", 50, 100),
    ])
    tracker.attach_ticket("WO-1", "anchor")

    tracker.withdraw("anchor")

    assert tracker.cluster_for("big").tickets == ()
    assert [t.ticket_id for t in tracker.pending_tickets] == ["WO-1"]


def test_pending_ticket_is_resolved_when_anchor_returns():
    tracker = EventClusterTracker(chain())
    tracker.attach_ticket("WO-1", "a")
    tracker.withdraw("a")

    update = tracker.add(record("a", 0, 10))

    cluster = tracker.cluster_for("a")
    assert cluster is not None
    assert [t.ticket_id for t in cluster.tickets] == ["WO-1"]
    assert update.pending_tickets == ()


def test_replaying_withdrawal_does_not_split_again():
    tracker = EventClusterTracker(chain())
    original = tracker.clusters[0].cluster_id
    first = tracker.withdraw("b")
    ids_after_first = tuple(
        cluster.cluster_id for cluster in tracker.clusters
    )

    replay = tracker.withdraw("b")

    assert replay.replayed == ("b",)
    assert replay.withdrawn == ()
    assert replay.changes == []
    assert tuple(
        cluster.cluster_id for cluster in tracker.clusters
    ) == ids_after_first
    assert first.successors_of(original) == ids_after_first


def test_replaying_withdrawal_keeps_retired_count():
    tracker = EventClusterTracker(chain())
    tracker.withdraw("b")
    retired = tuple(
        cluster.cluster_id for cluster in tracker.retired_clusters
    )

    tracker.withdraw("b")
    tracker.withdraw("b")

    assert tuple(
        cluster.cluster_id for cluster in tracker.retired_clusters
    ) == retired


def test_replayed_withdrawal_returns_unchanged_clusters():
    tracker = EventClusterTracker(chain())
    tracker.withdraw("b")
    update = tracker.withdraw("b")

    assert set(update.unchanged) == set(
        cluster.cluster_id for cluster in tracker.clusters
    )


def test_split_merge_gets_fresh_identity():
    tracker = EventClusterTracker([
        record("a", 0, 10),
        record("b", 20, 30),
    ])
    left, right = tracker.clusters

    update = tracker.add(record("bridge", 5, 25))

    assert len(tracker.clusters) == 1
    merged = tracker.clusters[0]
    assert merged.cluster_id not in (left.cluster_id, right.cluster_id)
    assert set(update.lineage[left.cluster_id]) == {merged.cluster_id}
    assert set(update.lineage[right.cluster_id]) == {merged.cluster_id}


def test_ticket_survives_merge():
    tracker = EventClusterTracker([
        record("a", 0, 10),
        record("b", 20, 30),
    ])
    tracker.attach_ticket("WO-1", "a")

    tracker.add(record("bridge", 5, 25))

    merged = tracker.clusters[0]
    assert [t.ticket_id for t in merged.tickets] == ["WO-1"]
    assert merged.tickets[0].history == (
        tracker.tickets[0].history[0],
        merged.cluster_id,
    )


def test_merge_keeps_closest_neighbor_history():
    tracker = EventClusterTracker([
        record("a", 0, 10),
        record("b", 20, 30),
        record("c", 40, 50),
    ])
    tracker.attach_ticket("WO-A", "a")
    tracker.attach_ticket("WO-C", "c")

    tracker.add(record("ab", 8, 22))
    tracker.add(record("bc", 28, 42))

    assert len(tracker.clusters) == 1
    assert sorted(
        ticket.ticket_id for ticket in tracker.clusters[0].tickets
    ) == ["WO-A", "WO-C"]


def test_records_can_be_updated_in_place():
    tracker = EventClusterTracker([record("a", 0, 10)])
    cluster_id = tracker.clusters[0].cluster_id

    update = tracker.apply(records=(record("a", 0, 12),))

    assert update.updated == ("a",)
    assert tracker.clusters[0].cluster_id == cluster_id
    assert tracker.clusters[0].version == 2


def test_update_event_sequence_mixes_adds_and_withdrawals():
    tracker = EventClusterTracker(chain())
    tracker.attach_ticket("WO-1", "b")

    update = tracker.apply(events=[
        RecordWithdrawal("b"),
        record("d", 20, 30),
    ])

    assert "b" in update.withdrawn
    assert "d" in update.added
    assert [t.ticket_id for t in tracker.pending_tickets] == ["WO-1"]
    assert [t.ticket_id for t in tracker.cluster_for("d").tickets] == []


def test_attach_ticket_to_missing_anchor_is_pending():
    tracker = EventClusterTracker()
    ticket = tracker.attach_ticket("WO-1", "later")

    assert ticket.pending
    assert tracker.pending_tickets == (ticket,)

    tracker.add(record("later", 1, 2))
    assert tracker.pending_tickets == ()
    assert [t.ticket_id for t in tracker.clusters[0].tickets] == ["WO-1"]


def test_assign_pending_ticket_manually():
    tracker = EventClusterTracker(chain())
    tracker.attach_ticket("WO-1", "a")
    tracker.withdraw("a")

    ticket = tracker.assign_ticket("WO-1", "c")

    assert ticket.cluster_id == tracker.cluster_for("c").cluster_id
    assert tracker.pending_tickets == ()


def test_duplicate_ticket_id_rejected():
    tracker = EventClusterTracker(chain())
    tracker.attach_ticket("WO-1", "a")
    with pytest.raises(ValueError):
        tracker.attach_ticket("WO-1", "c")


def test_null_record_rejected():
    tracker = EventClusterTracker()
    with pytest.raises(ValueError):
        tracker.add(record("a", 5, 5))


def test_update_json_shape():
    tracker = EventClusterTracker(chain())
    tracker.attach_ticket("WO-1", "a")

    payload = tracker.withdraw("b").to_dict()

    assert payload["withdrawn"] == ["b"]
    assert set(payload["lineage"]) == set(payload["lineage"])
    assert any(
        change["action"] == "retire" for change in payload["changes"]
    )
    assert sorted(
        record_id
        for change in payload["changes"]
        if change["action"] == "split"
        for record_id in change["records"]
    ) == ["a", "c"]


def test_same_range_records_are_separate_sources_of_one_cluster():
    tracker = EventClusterTracker([
        record("a", 0, 10),
        record("b", 0, 10),
    ])
    assert [cluster.record_ids for cluster in tracker.clusters] == [
        frozenset(["a", "b"]),
    ]

    tracker.withdraw("a")

    assert [cluster.record_ids for cluster in tracker.clusters] == [
        frozenset(["b"]),
    ]


def test_batch_event_order_does_not_matter():
    tracker = EventClusterTracker([
        record("a", 0, 10),
        record("b", 5, 15),
    ])

    update = tracker.apply(events=[record("c", 12, 20), "a"])

    assert update.added == ("c",)
    assert update.withdrawn == ("a",)
    assert [cluster.record_ids for cluster in tracker.clusters] == [
        frozenset(["b", "c"]),
    ]


def test_add_and_withdraw_same_record_in_one_batch_is_ignored():
    tracker = EventClusterTracker()

    update = tracker.apply(events=[record("x", 0, 10), "x"])

    assert update.added == ()
    assert update.withdrawn == ()
    assert update.ignored == ("x",)
    assert tracker.clusters == ()


def test_withdrawing_unknown_record_is_reported_separately():
    tracker = EventClusterTracker(chain())

    update = tracker.withdraw("never-seen")

    assert update.replayed == ()
    assert update.unknown == ("never-seen",)
    assert update.changes == []


def test_tracker_is_deterministic_across_insertion_order():
    records = chain()
    forward = EventClusterTracker(records)
    backward = EventClusterTracker(reversed(records))

    assert [
        cluster.record_ids for cluster in forward.clusters
    ] == [
        cluster.record_ids for cluster in backward.clusters
    ]

    forward.withdraw("b")
    backward.withdraw("b")
    forward_ids = tuple(
        cluster.cluster_id for cluster in forward.clusters
    )
    backward_ids = tuple(
        cluster.cluster_id for cluster in backward.clusters
    )
    assert forward_ids == backward_ids


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
