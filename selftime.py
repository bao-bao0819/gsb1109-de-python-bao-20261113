"""
Compute the exclusive (self) time of tasks reported by a performance
profiler.

A task's inclusive time is the length of its reported start/end interval.
Its exclusive time is what remains after removing the time occupied by its
child calls. Subtracting each child's duration individually is wrong when
children overlap (concurrent siblings), when the same child is reported more
than once, or when a child keeps running after its parent has finished: the
naive sum can exceed the parent's own interval and produce a negative self
time.

Instead, for every task the intervals of its *direct* children are:

1. merged by task id into their union (an identical duplicate report is
   counted once; disjoint duplicate reports keep the gap between them),
2. clipped to the parent's own reported intervals, so a child's detached
   tail is never charged to the parent, and
3. combined into their union before subtraction.

Parent-child relationships are taken only from the explicit ``parent_id``
fields. A record whose interval happens to be contained in another record but
has no parent link to it is never treated as a child. Every unit of time that
is deducted can therefore be traced to at least one real child call covering
it; see :func:`coverage_breakdown`.
"""
from __future__ import absolute_import

from numbers import Real

__all__ = [
    "TaskRecord",
    "compute_self_times",
    "compute_self_segments",
    "child_union_segments",
    "coverage_breakdown",
]


class TaskRecord(object):
    """A single reported task interval with its explicit parent link."""

    __slots__ = ("task_id", "start", "end", "parent_id")

    def __init__(self, task_id, start, end, parent_id=None):
        self.task_id = task_id
        self.start = start
        self.end = end
        self.parent_id = parent_id

    def __iter__(self):
        yield self.task_id
        yield self.start
        yield self.end
        yield self.parent_id

    def __repr__(self):
        return "TaskRecord({0!r}, {1!r}, {2!r}, {3!r})".format(
            self.task_id, self.start, self.end, self.parent_id
        )


_ID_KEYS = ("task_id", "id", "span_id")
_START_KEYS = ("start", "start_time", "begin", "begin_time")
_END_KEYS = ("end", "end_time", "finish", "finish_time")
_PARENT_KEYS = ("parent_id", "parent", "parent_task_id")


def _coerce(record):
    """Normalize any supported input shape into (id, start, end, parent)."""
    if isinstance(record, TaskRecord):
        return record.task_id, record.start, record.end, record.parent_id
    if isinstance(record, dict):
        task_id = _first_present(record, _ID_KEYS)
        start = _first_present(record, _START_KEYS)
        end = _first_present(record, _END_KEYS)
        parent_id = _first_present(record, _PARENT_KEYS)
        return task_id, start, end, parent_id
    if isinstance(record, (list, tuple)):
        if len(record) == 3:
            return record[0], record[1], record[2], None
        if len(record) == 4:
            return record[0], record[1], record[2], record[3]
        raise ValueError(
            "sequence record must have 3 or 4 elements, got {0}".format(
                len(record)
            )
        )
    raise TypeError(
        "unsupported record type {0!r}; expected TaskRecord, dict, "
        "tuple or list".format(type(record).__name__)
    )


def _first_present(mapping, keys):
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def _normalize(records):
    """
    Validate records and group every report by task id.

    Both the task's own running time and its children's coverage are derived
    later from the actual reported intervals, so a gap between two disjoint
    duplicate reports is never fabricated as busy time.
    """
    grouped = {}
    for raw in records:
        task_id, start, end, parent_id = _coerce(raw)
        if task_id is None:
            raise ValueError("every record must have a task id")
        if not isinstance(start, Real) or not isinstance(end, Real):
            raise TypeError(
                "task {0!r}: start and end must be real "
                "numbers".format(task_id)
            )
        if isinstance(start, bool) or isinstance(end, bool):
            raise TypeError(
                "task {0!r}: start and end must be numbers, not bool".format(
                    task_id
                )
            )
        if end < start:
            raise ValueError(
                "task {0!r}: end {1!r} is earlier than start {2!r}".format(
                    task_id, end, start
                )
            )
        if task_id not in grouped:
            grouped[task_id] = {
                "intervals": [(start, end)],
                "parent_id": parent_id,
            }
            continue
        current = grouped[task_id]
        current["intervals"].append((start, end))
        known_parents = set(
            candidate
            for candidate in (current["parent_id"], parent_id)
            if candidate is not None
        )
        if len(known_parents) > 1:
            raise ValueError(
                "task {0!r} reports conflicting parents: {1}".format(
                    task_id, sorted(known_parents, key=str)
                )
            )
        current["parent_id"] = (
            next(iter(known_parents)) if known_parents else None
        )
    return grouped


def _build_children(grouped):
    """Group direct child intervals under each parent using explicit links."""
    known_ids = set(grouped)
    children = dict((task_id, []) for task_id in known_ids)
    for task_id, entry in grouped.items():
        parent_id = entry["parent_id"]
        if parent_id is None or parent_id == task_id:
            continue
        if parent_id not in known_ids:
            # An unknown parent gives no usable parent-child identity;
            # time containment alone must not invent one later.
            continue
        for start, end in entry["intervals"]:
            children[parent_id].append((start, end, task_id))
    return children


def _clamp(interval, parent_start, parent_end):
    """Clip a child interval to the parent's half-open [start, end)."""
    start, end, task_id = interval
    if start < parent_start:
        start = parent_start
    if end > parent_end:
        end = parent_end
    if end <= start:
        return None
    return start, end, task_id


def _coverage(parent_start, parent_end, child_intervals):
    """
    Sweep the clipped child intervals and return atomic covered segments.

    Each returned item is ``(segment_start, segment_end, covering_ids)`` where
    ``covering_ids`` is the tuple of direct child tasks active throughout that
    segment, in first-seen order. Only points covered by at least one real
    child appear here.
    """
    events = []
    for interval in child_intervals:
        clipped = _clamp(interval, parent_start, parent_end)
        if clipped is None:
            continue
        start, end, task_id = clipped
        events.append((start, 1, task_id))
        events.append((end, 0, task_id))
    # At an instant where one child ends and another starts, process the end
    # first: intervals are half-open, so the shared boundary costs no time.
    events.sort(key=lambda event: (event[0], event[1]))

    # A task can contribute several disjoint intervals (duplicate reports),
    # so track an activation count: one interval ending must not deactivate
    # the task while a sibling interval of the same task is still running.
    active_counts = {}
    first_seen = []
    previous = parent_start
    breakdown = []
    for moment, kind, task_id in events:
        if moment > previous and active_counts:
            covering = tuple(
                candidate
                for candidate in first_seen
                if active_counts.get(candidate, 0) > 0
            )
            breakdown.append((previous, moment, covering))
        if kind == 1:
            if task_id not in active_counts:
                active_counts[task_id] = 0
                first_seen.append(task_id)
            active_counts[task_id] += 1
        else:
            active_counts[task_id] -= 1
            if active_counts[task_id] == 0:
                del active_counts[task_id]
        previous = moment
    return breakdown


def _merge_segments(segments):
    """Merge touching or overlapping (start, end) segments into their union."""
    if not segments:
        return []
    ordered = sorted((start, end) for start, end in segments)
    union = [[ordered[0][0], ordered[0][1]]]
    for start, end in ordered[1:]:
        if start <= union[-1][1]:
            if end > union[-1][1]:
                union[-1][1] = end
        else:
            union.append([start, end])
    return [(start, end) for start, end in union]


def _residual_segments(parent_start, parent_end, blocked):
    """Subtract merged blocked segments from the parent's own interval."""
    residual = []
    cursor = parent_start
    for start, end in blocked:
        if start > cursor:
            residual.append((cursor, start))
        if end > cursor:
            cursor = end
    if cursor < parent_end:
        residual.append((cursor, parent_end))
    return residual


def _analyze(records):
    grouped = _normalize(records)
    children = _build_children(grouped)
    per_task = {}
    for task_id, entry in grouped.items():
        # The task is only "running" during its actual reported intervals;
        # a gap between disjoint reports is neither self time nor child time.
        own_union = _merge_segments(entry["intervals"])
        child_intervals = children.get(task_id, ())
        breakdown = []
        blocked = []
        residual = []
        for seg_start, seg_end in own_union:
            seg_breakdown = _coverage(seg_start, seg_end, child_intervals)
            breakdown.extend(seg_breakdown)
            seg_blocked = _merge_segments(
                [(b_start, b_end) for b_start, b_end, _ in seg_breakdown]
            )
            blocked.extend(seg_blocked)
            residual.extend(
                _residual_segments(seg_start, seg_end, seg_blocked)
            )
        per_task[task_id] = {
            "blocked": blocked,
            "residual": residual,
            "coverage": breakdown,
        }
    return per_task


def compute_self_segments(records):
    """
    Return each task's exclusive time as uncovered interval segments.

    :param records: iterable of :class:`TaskRecord`, dicts, or sequences of
        ``(task_id, start, end)`` / ``(task_id, start, end, parent_id)``.
    :return: mapping ``task_id -> [(seg_start, seg_end), ...]`` of the parts
        of the task's interval not covered by any direct child, in order.
    """
    return dict(
        (task_id, list(detail["residual"]))
        for task_id, detail in _analyze(records).items()
    )


def compute_self_times(records):
    """
    Return each task's exclusive (self) time.

    The result is always non-negative: child time is removed as a union
    clipped to the parent's interval, never as a raw sum of durations.

    :param records: see :func:`compute_self_segments`.
    :return: mapping ``task_id -> self_time``.
    """
    self_times = {}
    for task_id, detail in _analyze(records).items():
        self_times[task_id] = sum(
            (end - start) for start, end in detail["residual"]
        )
    return self_times


def child_union_segments(records):
    """
    Return the union of each task's direct-child intervals, clipped to it.

    :param records: see :func:`compute_self_segments`.
    :return: mapping ``task_id -> [(seg_start, seg_end), ...]`` of merged
        child-covered segments inside the task's interval.
    """
    return dict(
        (task_id, list(detail["blocked"]))
        for task_id, detail in _analyze(records).items()
    )


def coverage_breakdown(records):
    """
    Return traceable child coverage for every task.

    :param records: see :func:`compute_self_segments`.
    :return: mapping ``task_id -> [(seg_start, seg_end, covering_ids), ...]``
        where every deducted segment names the direct child task ids that
        cover it. Segments tile exactly the union returned by
        :func:`child_union_segments`.
    """
    return dict(
        (task_id, [tuple(item) for item in detail["coverage"]])
        for task_id, detail in _analyze(records).items()
    )
