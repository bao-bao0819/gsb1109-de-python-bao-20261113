#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
Incremental event-cluster bookkeeping on top of IntervalTree.

Event records are device-abnormality segments reported by different
sources. Records whose ranges intersect belong to the same event cluster.
An operator may attach a work-order ticket to a cluster by anchoring it
to one of the cluster's records.

Clusters keep a stable identity across incremental updates. When a
withdrawal disconnects a cluster, the update reports the old cluster's
lineage and moves each ticket to the piece that still contains its
anchor. A ticket whose anchor was withdrawn stays pending for manual
assignment instead of being guessed onto the longest piece. Replaying
a withdrawal that was already applied changes nothing.
"""

try:
    string_types = (basestring,)  # Python 2
except NameError:
    string_types = (str,)  # Python 3

from collections import defaultdict

from .interval import Interval
from .intervaltree import IntervalTree

__all__ = [
    "Cluster",
    "ClusterChange",
    "ClusterTicket",
    "ClusterUpdate",
    "EventClusterTracker",
    "EventRecord",
    "RecordWithdrawal",
]


class EventRecord(Interval):
    """
    One source record of a device abnormality.

    The record is a half-open interval ``[begin, end)``. ``record_id``
    identifies the source record across incremental updates and is the
    anchor to which a work-order ticket can be attached. ``data`` is an
    optional payload belonging to the source record.
    """

    __slots__ = ()

    def __new__(cls, begin, end, record_id, data=None):
        return super(EventRecord, cls).__new__(
            cls, begin, end, (record_id, data)
        )

    @property
    def record_id(self):
        return self.data[0]

    @property
    def payload(self):
        return self.data[1]

    def __repr__(self):
        return "EventRecord({0!r}, {1!r}, {2!r}, {3!r})".format(
            self.begin, self.end, self.record_id, self.payload
        )


class RecordWithdrawal(object):
    """
    An explicit withdrawal event for one source record.

    Withdrawals are idempotent: applying the same withdrawal again is
    reported as a replay and does not split any cluster.
    """

    __slots__ = ("record_id",)

    def __init__(self, record_id):
        self.record_id = record_id

    def __repr__(self):
        return "RecordWithdrawal({0!r})".format(self.record_id)

    def __eq__(self, other):
        if not isinstance(other, RecordWithdrawal):
            return NotImplemented
        return self.record_id == other.record_id

    def __ne__(self, other):
        result = self.__eq__(other)
        if result is NotImplemented:
            return result
        return not result


class ClusterTicket(object):
    """
    A work-order ticket anchored to one source record.

    ``cluster_id`` is None while the ticket waits in the pending pool,
    either because its anchor has not arrived yet or because the anchor
    was withdrawn. ``history`` lists the cluster ids the ticket has been
    assigned to, oldest first.
    """

    __slots__ = ("ticket_id", "anchor", "cluster_id", "history")

    def __init__(self, ticket_id, anchor, cluster_id=None, history=()):
        self.ticket_id = ticket_id
        self.anchor = anchor
        self.cluster_id = cluster_id
        self.history = tuple(history)

    @property
    def assigned(self):
        return self.cluster_id is not None

    @property
    def pending(self):
        return self.cluster_id is None

    def copy(self):
        return ClusterTicket(
            self.ticket_id, self.anchor, self.cluster_id, self.history
        )

    def moved_to(self, cluster_id):
        copy = self.copy()
        copy.cluster_id = cluster_id
        if not copy.history or copy.history[-1] != cluster_id:
            copy.history = copy.history + (cluster_id,)
        return copy

    def unassigned(self):
        copy = self.copy()
        copy.cluster_id = None
        return copy

    def to_dict(self):
        return {
            "ticket_id": self.ticket_id,
            "anchor": self.anchor,
            "cluster_id": self.cluster_id,
            "pending": self.pending,
            "history": list(self.history),
        }

    def __eq__(self, other):
        if not isinstance(other, ClusterTicket):
            return NotImplemented
        return (
            self.ticket_id == other.ticket_id and
            self.anchor == other.anchor and
            self.cluster_id == other.cluster_id and
            self.history == other.history
        )

    def __ne__(self, other):
        result = self.__eq__(other)
        if result is NotImplemented:
            return result
        return not result

    def __repr__(self):
        return (
            "ClusterTicket({0!r}, anchor={1!r}, cluster_id={2!r})".format(
                self.ticket_id, self.anchor, self.cluster_id
            )
        )


class Cluster(object):
    """
    A connected group of event records sharing one identity.

    ``version`` increases whenever the cluster's record membership
    changes, and is carried unchanged by a ticket-only update.
    ``predecessors`` names the old cluster ids this identity descends
    from in its most recent change.
    """

    __slots__ = (
        "cluster_id", "version", "records", "tickets", "predecessors"
    )

    def __init__(
        self, cluster_id, version, records, tickets=(), predecessors=()
    ):
        self.cluster_id = cluster_id
        self.version = version
        self.records = frozenset(records)
        self.tickets = tuple(tickets)
        self.predecessors = tuple(predecessors)

    @property
    def record_ids(self):
        return frozenset(record.record_id for record in self.records)

    @property
    def ticket_ids(self):
        return frozenset(ticket.ticket_id for ticket in self.tickets)

    def copy(self):
        return Cluster(
            self.cluster_id, self.version, self.records,
            [ticket.copy() for ticket in self.tickets], self.predecessors,
        )

    @property
    def begin(self):
        return min(record.begin for record in self.records)

    @property
    def end(self):
        return max(record.end for record in self.records)

    @property
    def span(self):
        return self.end - self.begin

    @property
    def range(self):
        return Interval(self.begin, self.end)

    def __contains__(self, record_or_id):
        if isinstance(record_or_id, EventRecord):
            record_or_id = record_or_id.record_id
        return any(
            record.record_id == record_or_id for record in self.records
        )

    def to_dict(self):
        return {
            "cluster_id": self.cluster_id,
            "version": self.version,
            "begin": self.begin,
            "end": self.end,
            "records": sorted(self.record_ids, key=repr),
            "tickets": [ticket.to_dict() for ticket in self.tickets],
            "predecessors": list(self.predecessors),
        }

    def __eq__(self, other):
        if not isinstance(other, Cluster):
            return NotImplemented
        return (
            self.cluster_id == other.cluster_id and
            self.version == other.version and
            self.records == other.records and
            self.tickets == other.tickets and
            self.predecessors == other.predecessors
        )

    def __ne__(self, other):
        result = self.__eq__(other)
        if result is NotImplemented:
            return result
        return not result

    def __repr__(self):
        return (
            "Cluster({0!r}, version={1!r}, records={2!r}, tickets={3!r})"
            .format(
                self.cluster_id, self.version,
                sorted(self.record_ids, key=repr), self.tickets,
            )
        )


class ClusterChange(object):
    """
    What happened to one cluster in an incremental update.

    ``action`` is one of ``create``, ``update``, ``split``, ``merge``,
    ``split_merge`` or ``retire``. Split pieces and merged clusters get
    a fresh identity and list the old ids in ``predecessors``. A retire
    change has no live cluster; its old id is ``retired_cluster_id``.
    """

    __slots__ = ("action", "cluster", "predecessors")

    def __init__(self, action, cluster=None, predecessors=()):
        self.action = action
        self.cluster = cluster
        self.predecessors = tuple(predecessors)

    @property
    def cluster_id(self):
        return self.cluster.cluster_id if self.cluster is not None else None

    @property
    def retired_cluster_id(self):
        if self.cluster is None and self.predecessors:
            return self.predecessors[0]
        return None

    @property
    def tickets(self):
        return self.cluster.tickets if self.cluster is not None else ()

    @property
    def alive(self):
        return self.cluster is not None

    def to_dict(self):
        result = {
            "action": self.action,
            "cluster_id": self.cluster_id,
            "predecessors": list(self.predecessors),
            "tickets": [ticket.to_dict() for ticket in self.tickets],
        }
        if self.cluster is not None:
            result["records"] = sorted(self.cluster.record_ids, key=repr)
            result["begin"] = self.cluster.begin
            result["end"] = self.cluster.end
            result["version"] = self.cluster.version
        else:
            result["retired_cluster_id"] = self.retired_cluster_id
        return result

    def __repr__(self):
        return (
            "ClusterChange({0!r}, cluster_id={1!r}, predecessors={2!r})"
            .format(self.action, self.cluster_id, self.predecessors)
        )


class ClusterUpdate(object):
    """
    The incremental result of applying records and withdrawals.

    ``changes`` reports creations, updates and split/merge pieces first,
    then retirements. ``unchanged`` names clusters whose identity,
    membership and tickets did not move. ``lineage`` maps every old
    cluster id that was present before the update to the live successor
    ids now derived from it; a retired id maps to an empty tuple.

    ``pending_tickets`` are the tickets that currently wait for manual
    assignment because their anchor record is absent. A withdrawal names
    the record in ``withdrawn`` if it existed, in ``replayed`` if it had
    already been withdrawn before, and in ``unknown`` otherwise.
    """

    __slots__ = (
        "changes", "unchanged", "pending_tickets",
        "added", "updated", "withdrawn", "replayed", "ignored", "unknown",
    )

    def __init__(
        self, changes=(), unchanged=(), pending_tickets=(),
        added=(), updated=(), withdrawn=(), replayed=(), ignored=(),
        unknown=(),
    ):
        self.changes = list(changes)
        self.unchanged = tuple(unchanged)
        self.pending_tickets = tuple(pending_tickets)
        self.added = tuple(added)
        self.updated = tuple(updated)
        self.withdrawn = tuple(withdrawn)
        self.replayed = tuple(replayed)
        self.ignored = tuple(ignored)
        self.unknown = tuple(unknown)

    @property
    def retired(self):
        return tuple(
            change.retired_cluster_id
            for change in self.changes
            if change.action == "retire"
        )

    @property
    def lineage(self):
        result = {}
        for cluster_id in self.unchanged:
            result[cluster_id] = [cluster_id]
        for change in self.changes:
            if change.action == "retire":
                result.setdefault(change.retired_cluster_id, [])
                continue
            cluster_id = change.cluster_id
            if change.action == "update" and (
                change.predecessors == (cluster_id,)
            ):
                result[cluster_id] = [cluster_id]
                continue
            for predecessor in change.predecessors:
                successors = result.setdefault(predecessor, [])
                if cluster_id not in successors:
                    successors.append(cluster_id)
        return dict(
            (cluster_id, tuple(successors))
            for cluster_id, successors in result.items()
        )

    def change_for(self, cluster_id):
        for change in self.changes:
            if change.cluster_id == cluster_id:
                return change
        return None

    def successors_of(self, cluster_id):
        """
        Return the live cluster ids that descend from an old id.

        An unchanged or continuously updated cluster is its own
        successor. A retired id has no successors. A split piece or a
        merged cluster may give one or several.
        """
        return self.lineage.get(cluster_id, ())

    def to_dict(self):
        return {
            "changes": [change.to_dict() for change in self.changes],
            "unchanged": list(self.unchanged),
            "pending_tickets": [
                ticket.to_dict() for ticket in self.pending_tickets
            ],
            "lineage": dict(
                (cluster_id, list(successors))
                for cluster_id, successors in self.lineage.items()
            ),
            "added": list(self.added),
            "updated": list(self.updated),
            "withdrawn": list(self.withdrawn),
            "replayed": list(self.replayed),
            "ignored": list(self.ignored),
            "unknown": list(self.unknown),
        }

    def __iter__(self):
        return iter(self.changes)

    def __len__(self):
        return len(self.changes)

    def __repr__(self):
        return (
            "ClusterUpdate(changes={0!r}, unchanged={1!r}, "
            "pending_tickets={2!r})".format(
                self.changes, self.unchanged, self.pending_tickets
            )
        )


class EventClusterTracker(object):
    """
    Incrementally tracks connected clusters of event records and tickets.

    Identity rules:

    * A cluster whose membership and tickets are untouched keeps its id.
    * A cluster that changes but stays one piece keeps its id and gets
      a higher version.
    * When one cluster breaks apart, the old id is retired and every
      piece gets a fresh id. The pieces' lineage points at the old id.
    * When clusters join, the result gets a fresh id whose lineage
      names all the old ids.

    Tickets follow the record they are anchored to. On a split, only
    the piece containing the anchor inherits the ticket. If the anchor
    record is withdrawn, the ticket is left pending instead of being
    assigned to the longest piece.
    """

    def __init__(self, records=()):
        self._records = {}
        self._clusters = {}
        self._retired = {}
        self._pending = {}
        self._withdrawn_ids = set()
        self._next_cluster_id = 1
        if records:
            self.apply(records=records)

    @property
    def clusters(self):
        return tuple(sorted(
            self._clusters.values(),
            key=lambda cluster: (
                cluster.begin, cluster.end, cluster.cluster_id
            ),
        ))

    @property
    def retired_clusters(self):
        return tuple(sorted(
            self._retired.values(),
            key=lambda cluster: cluster.cluster_id,
        ))

    @property
    def pending_tickets(self):
        return tuple(sorted(
            self._pending.values(),
            key=lambda ticket: (ticket.ticket_id, repr(ticket.anchor)),
        ))

    @property
    def tickets(self):
        result = []
        for cluster in self.clusters:
            result.extend(cluster.tickets)
        return tuple(sorted(
            result, key=lambda ticket: (ticket.ticket_id, repr(ticket.anchor))
        ))

    def records(self):
        return tuple(sorted(
            self._records.values(),
            key=lambda record: (
                record.begin, record.end, repr(record.record_id)
            ),
        ))

    def cluster_for(self, record_id):
        for cluster in self.clusters:
            if record_id in cluster:
                return cluster
        return None

    def apply(self, events=None, records=(), withdrawals=()):
        """
        Apply a batch of records and withdrawals atomically.

        ``events`` may mix ``EventRecord`` objects (add or update them)
        and ``RecordWithdrawal`` objects or record-id strings (withdraw
        them). Alternatively pass ``records`` and ``withdrawals``
        explicitly. Withdrawing an already-absent record is reported as
        a replay and has no structural effect.
        """
        incoming_records = []
        withdraw_ids = []

        if events is not None:
            for event in self._iter_items(events):
                if isinstance(event, EventRecord):
                    incoming_records.append(event)
                elif isinstance(event, RecordWithdrawal):
                    withdraw_ids.append(event.record_id)
                elif isinstance(event, string_types):
                    withdraw_ids.append(event)
                else:
                    raise TypeError(
                        "events must be EventRecord or RecordWithdrawal, "
                        "got {0!r}".format(event)
                    )
        if records:
            if isinstance(records, EventRecord):
                incoming_records.append(records)
            else:
                incoming_records.extend(records)
        if withdrawals:
            if isinstance(withdrawals, string_types):
                withdraw_ids.append(withdrawals)
            else:
                withdraw_ids.extend(withdrawals)

        for record in incoming_records:
            if not isinstance(record, EventRecord):
                raise TypeError(
                    "records must be EventRecord objects, got {0!r}"
                    .format(record)
                )
            if record.begin >= record.end:
                raise ValueError(
                    "event records must not be null: {0!r}".format(record)
                )

        to_withdraw = set(withdraw_ids)
        added = []
        updated = []
        ignored = []
        withdrawn = []
        replayed = []
        unknown = []

        for record in incoming_records:
            record_id = record.record_id
            if record_id in to_withdraw:
                ignored.append(record_id)
                continue
            existing = self._records.get(record_id)
            if existing is None:
                self._records[record_id] = record
                self._withdrawn_ids.discard(record_id)
                added.append(record_id)
            elif existing == record:
                ignored.append(record_id)
            else:
                self._records[record_id] = record
                self._withdrawn_ids.discard(record_id)
                updated.append(record_id)

        for record_id in withdraw_ids:
            if record_id in self._records:
                del self._records[record_id]
                self._withdrawn_ids.add(record_id)
                withdrawn.append(record_id)
            elif record_id in self._withdrawn_ids:
                replayed.append(record_id)
            else:
                unknown.append(record_id)

        if not added and not updated and not withdrawn:
            return ClusterUpdate(
                unchanged=tuple(
                    cluster.cluster_id for cluster in self.clusters
                ),
                pending_tickets=self.pending_tickets,
                added=added,
                updated=updated,
                withdrawn=withdrawn,
                replayed=replayed,
                ignored=ignored,
                unknown=unknown,
            )

        old_clusters = dict(
            (cluster_id, cluster.copy())
            for cluster_id, cluster in self._clusters.items()
        )
        changes, unchanged, pending = self._reconcile(old_clusters)

        return ClusterUpdate(
            changes=changes,
            unchanged=unchanged,
            pending_tickets=pending,
            added=added,
            updated=updated,
            withdrawn=withdrawn,
            replayed=replayed,
            ignored=ignored,
            unknown=unknown,
        )

    def update(self, events=None, records=(), withdrawals=()):
        """
        Alias for :meth:`apply`.
        """
        return self.apply(
            events=events, records=records, withdrawals=withdrawals
        )

    def add(self, record):
        """
        Add one ``EventRecord`` and return the incremental update.
        """
        return self.apply(records=(record,))

    def withdraw(self, record_or_id):
        """
        Withdraw one record, by object or id, and return the update.
        """
        if isinstance(record_or_id, EventRecord):
            record_or_id = record_or_id.record_id
        return self.apply(withdrawals=(record_or_id,))

    def attach_ticket(self, ticket_id, anchor):
        """
        Attach a work-order ticket to the record ``anchor``.

        ``anchor`` is either a record id or an ``EventRecord``. If the
        anchor is present, the ticket is assigned to its cluster
        immediately. Otherwise it waits in the pending pool. Returns the
        ticket.
        """
        if isinstance(anchor, EventRecord):
            anchor = anchor.record_id
        if any(
            ticket.ticket_id == ticket_id
            for ticket in self._all_tickets()
        ):
            raise ValueError(
                "ticket {0!r} already exists".format(ticket_id)
            )

        ticket = ClusterTicket(ticket_id, anchor)
        cluster = self.cluster_for(anchor)
        if cluster is None:
            self._pending[ticket_id] = ticket
            return ticket

        ticket = ticket.moved_to(cluster.cluster_id)
        self._put_ticket(cluster.cluster_id, ticket)
        return ticket

    def assign_ticket(self, ticket_id, anchor):
        """
        Resolve a pending ticket by re-anchoring it to a live record.

        This is the manual dispatch path: a ticket whose original anchor
        was withdrawn cannot be moved automatically, so an operator picks
        a record of the destination cluster. Returns the ticket.
        """
        if ticket_id not in self._pending:
            raise KeyError(
                "no pending ticket {0!r}".format(ticket_id)
            )
        if isinstance(anchor, EventRecord):
            anchor = anchor.record_id

        cluster = self.cluster_for(anchor)
        if cluster is None:
            raise KeyError(
                "no live cluster contains anchor {0!r}".format(anchor)
            )

        ticket = self._pending.pop(ticket_id)
        ticket = ClusterTicket(
            ticket.ticket_id, anchor, None, ticket.history,
        ).moved_to(cluster.cluster_id)
        self._put_ticket(cluster.cluster_id, ticket)
        return ticket

    def _all_tickets(self):
        for cluster in self._clusters.values():
            for ticket in cluster.tickets:
                yield ticket
        for ticket in self._pending.values():
            yield ticket

    def _iter_items(self, items):
        if isinstance(items, (EventRecord, RecordWithdrawal)) or (
            isinstance(items, string_types)
        ):
            return (items,)
        return items

    def _put_ticket(self, cluster_id, ticket):
        cluster = self._clusters[cluster_id]
        tickets = [
            ticket if existing.ticket_id == ticket.ticket_id else existing
            for existing in cluster.tickets
        ]
        if not any(
            existing.ticket_id == ticket.ticket_id
            for existing in cluster.tickets
        ):
            tickets.append(ticket)
        self._clusters[cluster_id] = Cluster(
            cluster.cluster_id, cluster.version, cluster.records,
            tickets, cluster.predecessors,
        )

    def _components(self):
        """
        Return connected components as a list of frozensets of records.

        Components are ordered by their leftmost record.
        """
        records = self.records()
        tree = IntervalTree()
        for record in records:
            tree.add(record)

        visited = set()
        components = []
        for record in records:
            if record.record_id in visited:
                continue
            pending = [record]
            visited.add(record.record_id)
            members = set([record])
            while pending:
                current = pending.pop()
                for other in tree.overlap(current):
                    if other.record_id not in visited:
                        visited.add(other.record_id)
                        members.add(other)
                        pending.append(other)
            components.append(frozenset(members))

        components.sort(
            key=lambda members: (
                min(record.begin for record in members),
                max(record.end for record in members),
                sorted(repr(record.record_id) for record in members),
            )
        )
        return components

    def _reconcile(self, old_clusters):
        components = self._components()

        old_by_record = {}
        for cluster_id, cluster in old_clusters.items():
            for record in cluster.records:
                old_by_record[record.record_id] = cluster_id

        predecessors = []
        old_successors = defaultdict(list)
        for index, members in enumerate(components):
            old_ids = sorted(
                set(
                    old_by_record[record.record_id]
                    for record in members
                    if record.record_id in old_by_record
                ),
                key=repr,
            )
            predecessors.append(tuple(old_ids))
            for cluster_id in old_ids:
                old_successors[cluster_id].append(index)

        plan = []
        for index, members in enumerate(components):
            old_ids = predecessors[index]
            cluster_id = None
            version = 1
            change_predecessors = ()

            if (
                len(old_ids) == 1 and
                len(old_successors[old_ids[0]]) == 1
            ):
                old = old_clusters[old_ids[0]]
                cluster_id = old_ids[0]
                if old.records == members:
                    version = old.version
                    change_predecessors = old.predecessors
                else:
                    version = old.version + 1
                    change_predecessors = (cluster_id,)

            if cluster_id is None:
                cluster_id = self._allocate_cluster_id(old_clusters)
                if not old_ids:
                    action = "create"
                elif len(old_ids) == 1:
                    action = "split"
                elif any(
                    len(old_successors[old_id]) > 1 for old_id in old_ids
                ):
                    action = "split_merge"
                else:
                    action = "merge"
                change_predecessors = tuple(old_ids)
            else:
                action = (
                    None
                    if old_clusters[cluster_id].records == members
                    else "update"
                )

            plan.append({
                "cluster_id": cluster_id,
                "version": version,
                "members": members,
                "action": action,
                "predecessors": tuple(change_predecessors),
                "old_id": (
                    cluster_id
                    if cluster_id in old_clusters
                    else None
                ),
            })

        tickets_by_cluster, pending = self._reassign_tickets(plan)
        kept_ids = set(
            item["cluster_id"] for item in plan
            if item["old_id"] is not None
        )

        new_clusters = []
        changes = []
        unchanged = []
        for item in plan:
            tickets = tickets_by_cluster.get(item["cluster_id"], ())
            cluster = Cluster(
                item["cluster_id"], item["version"], item["members"],
                tickets,
                item["predecessors"],
            )
            new_clusters.append(cluster)

            action = item["action"]
            if action is not None and item["old_id"] is not None:
                old = old_clusters[item["old_id"]]
                if old.tickets != tickets:
                    action = "update"
            if action is None:
                unchanged.append(cluster.cluster_id)
            else:
                changes.append(ClusterChange(
                    action, cluster, item["predecessors"],
                ))

        changes.sort(key=lambda change: (
            change.cluster.begin,
            change.cluster.end,
            repr(change.cluster.cluster_id),
        ))

        for cluster_id in sorted(old_clusters, key=repr):
            if cluster_id in kept_ids:
                continue
            changes.append(ClusterChange(
                "retire", None, (cluster_id,),
            ))
            self._retired[cluster_id] = old_clusters[cluster_id]

        self._clusters = dict(
            (cluster.cluster_id, cluster) for cluster in new_clusters
        )
        self._pending = pending
        return changes, tuple(sorted(unchanged, key=repr)), tuple(
            sorted(
                pending.values(),
                key=lambda ticket: (ticket.ticket_id, repr(ticket.anchor)),
            )
        )

    def _reassign_tickets(self, plan):
        record_to_cluster = {}
        for item in plan:
            for record in item["members"]:
                record_to_cluster[record.record_id] = item["cluster_id"]

        tickets = []
        for cluster in self._clusters.values():
            tickets.extend(cluster.tickets)
        tickets.extend(self._pending.values())

        by_cluster = defaultdict(list)
        pending = {}
        for ticket in tickets:
            target = record_to_cluster.get(ticket.anchor)
            if target is None:
                pending[ticket.ticket_id] = ticket.unassigned()
            else:
                by_cluster[target].append(ticket.moved_to(target))

        for cluster_id in by_cluster:
            by_cluster[cluster_id].sort(
                key=lambda ticket: (
                    ticket.ticket_id, repr(ticket.anchor)
                )
            )
        return by_cluster, pending

    def _allocate_cluster_id(self, old_clusters):
        while True:
            cluster_id = "cluster-{0}".format(self._next_cluster_id)
            self._next_cluster_id += 1
            if cluster_id not in old_clusters:
                return cluster_id
