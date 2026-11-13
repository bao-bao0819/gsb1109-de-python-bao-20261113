"""
Data-source watermark driven reconciliation scheduling.

Each upstream source is polled independently and answers with a
:class:`Receipt` carrying a watermark. A watermark is a business-time point
up to which the source guarantees that *all* data has been handed over --
including empty batches, which carry a watermark just like any other reply.

A reconciliation for a statistical window is only dispatched once every
required source has sealed that same window. Sources publish at different
cadences and may re-publish corrections for old windows. Corrections only
produce a new reconciliation version for the windows whose input receipts
actually changed; a late out-of-order watermark can never overwrite a newer
receipt that was already confirmed. Every reconciliation report keeps the
exact receipt (per source) it was built from, so the daily record can always
explain which reply of each source was used.

Terminology:
    - :class:`Watermark` -- the "delivered up to this business time" seal.
    - :class:`Receipt` -- a watermark plus the evidence of what was delivered
      (batch ids, the receipt it supersedes, ...).
    - :class:`ReconciliationReport` -- one immutable version of the
      reconciliation of one window, recording the receipts it is based on.
    - :class:`ReconciliationScheduler` -- tracks per-source receipt logs and
      decides when a window is ready and when a correction requires a new
      version.

Example:
    >>> import datetime as dt
    >>> scheduler = ReconciliationScheduler(datetime.timedelta(hours=1))
    >>> scheduler.register_source("payments")
    >>> scheduler.register_source("ledger")
    >>> scheduler.observe_receipt(make_receipt(
    ...     "payments", dt.datetime(2026, 11, 13, 10, tzinfo=dt.timezone.utc),
    ...     published_at=dt.datetime(2026, 11, 13, 10, 5, tzinfo=dt.timezone.utc),
    ... ))
    []
"""

from __future__ import annotations

import datetime
import logging
import threading
from dataclasses import dataclass, field
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
)

logger = logging.getLogger("schedule.reconciliation")

#: The report is the newest version for its window.
STATUS_CURRENT = "current"

#: The report has been replaced by a newer version for its window.
STATUS_SUPERSEDED = "superseded"


class ReconciliationError(Exception):
    """Base exception for watermark reconciliation scheduling."""


@dataclass(frozen=True)
class Watermark:
    """
    A watermark published by one source.

    It states that the source has fully delivered every piece of data whose
    business time is strictly before ``business_time``. An empty batch still
    publishes a watermark (with ``empty=True``).

    :param source: Name of the source that published the watermark.
    :param business_time: Watermark position on the business-time axis.
    :param published_at: Wall-clock time at which the source published it.
    :param revision: Monotonic per-source revision number. A receipt whose
        revision is lower than one already seen is treated as a stale late
        delivery: kept for audit but never used for selection.
    :param receipt_id: Stable identifier of the carrying receipt.
    :param empty: ``True`` when the poll returned an empty batch.
    """

    source: str
    business_time: datetime.datetime
    published_at: datetime.datetime
    revision: int
    receipt_id: str
    empty: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.business_time, datetime.datetime):
            raise ReconciliationError(
                "watermark business_time must be a datetime.datetime"
            )
        if not isinstance(self.published_at, datetime.datetime):
            raise ReconciliationError(
                "watermark published_at must be a datetime.datetime"
            )
        if not isinstance(self.revision, int) or self.revision < 0:
            raise ReconciliationError("watermark revision must be a non-negative int")
        aware_business = self.business_time.tzinfo is not None
        aware_published = self.published_at.tzinfo is not None
        if aware_business != aware_published:
            raise ReconciliationError(
                "business_time and published_at must both be naive or both aware"
            )


@dataclass(frozen=True)
class Receipt:
    """
    A poll reply: a watermark plus evidence of what was delivered.

    Receipts are immutable and kept in an append-only log per source, so even
    a late or superseded reply remains available for auditing.

    :param watermark: The watermark carried by this reply.
    :param batch_ids: Identifiers of the batches delivered in this reply.
        Empty for an empty-batch watermark.
    :param supersedes: Identifier of the receipt this one corrects, if any.
    :param metadata: Free-form extra evidence stored alongside the receipt.
    """

    watermark: Watermark
    batch_ids: Sequence[str] = field(default_factory=tuple)
    supersedes: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.watermark, Watermark):
            raise ReconciliationError("receipt must carry a Watermark")
        object.__setattr__(self, "batch_ids", tuple(self.batch_ids))
        object.__setattr__(self, "metadata", dict(self.metadata))

    @property
    def source(self) -> str:
        return self.watermark.source

    @property
    def business_time(self) -> datetime.datetime:
        return self.watermark.business_time

    @property
    def published_at(self) -> datetime.datetime:
        return self.watermark.published_at

    @property
    def revision(self) -> int:
        return self.watermark.revision

    @property
    def receipt_id(self) -> str:
        return self.watermark.receipt_id

    @property
    def empty(self) -> bool:
        return self.watermark.empty


def make_receipt(
    source: str,
    business_time: datetime.datetime,
    published_at: datetime.datetime,
    revision: int = 1,
    receipt_id: Optional[str] = None,
    batch_ids: Optional[Sequence[str]] = None,
    empty: bool = False,
    supersedes: Optional[str] = None,
    metadata: Optional[Mapping[str, Any]] = None,
) -> Receipt:
    """
    Build a :class:`Receipt` for ``source`` in one call.

    A stable ``receipt_id`` is generated when none is given, derived from
    source name, revision and publication time.
    """
    if receipt_id is None:
        stamp = published_at.strftime("%Y%m%dT%H%M%S%f")
        receipt_id = "{}:r{}:{}".format(source, revision, stamp)
    watermark = Watermark(
        source=source,
        business_time=business_time,
        published_at=published_at,
        revision=revision,
        receipt_id=receipt_id,
        empty=empty,
    )
    return Receipt(
        watermark=watermark,
        batch_ids=tuple(batch_ids) if batch_ids is not None else (),
        supersedes=supersedes,
        metadata=dict(metadata) if metadata is not None else {},
    )


@dataclass
class ReconciliationReport:
    """
    One version of a reconciliation for one window.

    The window coordinates, version number and the ``basis`` snapshot (the
    exact receipt selected from every source at creation time) never change.
    Only the lifecycle ``status`` flips once, from ``current`` to
    ``superseded``, when a newer version is created; ``superseded_by`` then
    points at that version.
    """

    window_start: datetime.datetime
    window_end: datetime.datetime
    version: int
    status: str
    basis: Dict[str, Receipt]
    created_at: datetime.datetime
    superseded_by: Optional[int] = None

    def receipt_for(self, source: str) -> Receipt:
        """Return the receipt from ``source`` this report is based on."""
        return self.basis[source]

    def describe(self) -> Dict[str, Any]:
        """
        Return a plain-dict, JSON-friendly description of the report.

        Every required source is listed with the precise receipt used, so the
        record can answer "which reply of each source backs this report?".
        """
        return {
            "window_start": self.window_start.isoformat(),
            "window_end": self.window_end.isoformat(),
            "version": self.version,
            "status": self.status,
            "superseded_by": self.superseded_by,
            "created_at": self.created_at.isoformat(),
            "sources": {
                source: {
                    "receipt_id": receipt.receipt_id,
                    "revision": receipt.revision,
                    "business_time": receipt.business_time.isoformat(),
                    "published_at": receipt.published_at.isoformat(),
                    "empty": receipt.empty,
                    "batch_ids": list(receipt.batch_ids),
                    "supersedes": receipt.supersedes,
                }
                for source, receipt in sorted(self.basis.items())
            },
        }


class ReconciliationScheduler:
    """
    Schedules reconciliation jobs from per-source watermarks.

    Each source is polled independently and fed its replies through
    :meth:`observe_receipt`. The scheduler keeps an append-only receipt log
    per source and, for every window all required sources have sealed,
    maintains a versioned chain of :class:`ReconciliationReport` objects.

    A window ``[start, end)`` is sealed by a source once one of its receipts
    reaches a ``business_time`` >= ``end``. A new report version is created
    only when the receipt selected for at least one source changes, so a
    correction can never trigger a re-run of a version that did not depend on
    the corrected receipt.

    Receipt selection is lineage based. The first report of a window pins the
    earliest receipt that sealed it. Afterwards the pinned receipt is only
    replaced by following the ``supersedes`` chain: a correcting receipt
    names the receipt it replaces, so only windows actually built on the
    replaced receipt move to a new version. A late receipt with a lower
    revision is marked stale on arrival, recorded in the append-only audit
    log but excluded from every selection decision, so it can never displace
    the confirmed newer one.

    :param window_size: Size of the statistical windows. Windows are aligned
        to the UNIX epoch, so ``timedelta(hours=1)`` gives clock-hour windows
        and ``timedelta(days=1)`` UTC-day windows.
    :param start: Start of the first statistical window under management.
        A watermark only gives an upper bound ("complete up to ..."), so the
        lower bound of the window range must be supplied explicitly. It must
        lie on a window boundary. When omitted, the scheduler conservatively
        starts at the earliest window sealed by every source's first receipt.
    :param clock: Callable returning the current wall-clock datetime, used
        for ``created_at`` timestamps. Injectable for testing.
    """

    _EPOCH = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)

    def __init__(
        self,
        window_size: datetime.timedelta,
        *,
        start: Optional[datetime.datetime] = None,
        clock: Callable[[], datetime.datetime] = datetime.datetime.now,
        on_report: Optional[Callable[["ReconciliationReport"], None]] = None,
    ) -> None:
        if (
            not isinstance(window_size, datetime.timedelta)
            or window_size <= datetime.timedelta(0)
        ):
            raise ReconciliationError("window_size must be a positive timedelta")
        self.window_size = window_size
        if start is not None and not isinstance(start, datetime.datetime):
            raise ReconciliationError("start must be a datetime.datetime")
        self._start = start
        self._clock = clock
        self._lock = threading.RLock()
        self._receipts: Dict[str, List[Receipt]] = {}
        self._seen_receipt_ids: Dict[str, Set[str]] = {}
        self._stale_receipt_ids: Dict[str, Set[str]] = {}
        self._highest_revision: Dict[str, int] = {}
        self._reports: Dict[datetime.datetime, List[ReconciliationReport]] = {}
        self._listeners: List[Callable[["ReconciliationReport"], None]] = []
        if on_report is not None:
            self.add_listener(on_report)

    def add_listener(
        self, listener: Callable[["ReconciliationReport"], None]
    ) -> Callable[["ReconciliationReport"], None]:
        """
        Register a callback invoked once for every freshly created report.

        This is where the reconciliation job itself is dispatched; a listener
        is never called twice for the same report version. Returns the
        listener so it can also be used as a decorator.
        """
        if not callable(listener):
            raise ReconciliationError("listener must be callable")
        self._listeners.append(listener)
        return listener

    def _dispatch(self, report: "ReconciliationReport") -> None:
        for listener in list(self._listeners):
            try:
                listener(report)
            except Exception:  # pragma: no cover - defensive logging path
                logger.exception(
                    "Reconciliation listener %r failed for window %s version %s",
                    listener,
                    report.window_start.isoformat(),
                    report.version,
                )

    # -- registration -----------------------------------------------------

    def register_source(
        self, source: str, initial: Optional[Receipt] = None
    ) -> None:
        """
        Register a required source.

        :param source: Unique source name.
        :param initial: Optional baseline receipt (for example a revision 0
            watermark at the epoch) recorded immediately.
        """
        with self._lock:
            if source in self._receipts:
                raise ReconciliationError(
                    "source {!r} is already registered".format(source)
                )
            self._receipts[source] = []
            self._seen_receipt_ids[source] = set()
            self._stale_receipt_ids[source] = set()
            self._highest_revision[source] = -1
            if initial is not None:
                self.observe_receipt(initial)

    @property
    def sources(self) -> Tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._receipts))

    # -- polling inputs ---------------------------------------------------

    def poll_source(
        self, source: str, fetcher: Callable[[], Optional[Receipt]]
    ) -> List[ReconciliationReport]:
        """
        Poll one source through ``fetcher`` and ingest its reply.

        This is the integration point for independent per-source polling:
        each source keeps its own cadence and its own fetch call. A fetch
        returning ``None`` (no reply this round) changes nothing.
        """
        receipt = fetcher()
        if receipt is None:
            return []
        return self.observe_receipt(receipt)

    def observe_watermark(self, watermark: Watermark) -> List[ReconciliationReport]:
        """Ingest a bare watermark (for example an empty-batch seal)."""
        receipt = Receipt(watermark=watermark, batch_ids=())
        return self.observe_receipt(receipt)

    def observe_receipt(self, receipt: Receipt) -> List[ReconciliationReport]:
        """
        Append one source receipt and reconcile every affected window.

        Returns the reports newly created by this receipt (possibly several
        windows at once, possibly a new version of an already confirmed
        window). Never returns reports that were already known.
        """
        source = receipt.source
        with self._lock:
            if source not in self._receipts:
                raise ReconciliationError(
                    "unknown source {!r}; register it first".format(source)
                )
            aware = receipt.business_time.tzinfo is not None
            if self._start is not None:
                start_aware = self._start.tzinfo is not None
                if start_aware != aware:
                    raise ReconciliationError(
                        "receipt from {!r} is {} but configured start is {} "
                        "aware; datetimes must agree".format(
                            source,
                            "aware" if aware else "naive",
                            "" if start_aware else "not",
                        )
                    )
            for other_source, other_receipts in self._receipts.items():
                if other_receipts:
                    other_aware = other_receipts[0].business_time.tzinfo is not None
                    if other_aware != aware:
                        raise ReconciliationError(
                            "receipt from {!r} is {} but source {!r} uses {} "
                            "datetimes; all sources must agree".format(
                                source,
                                "aware" if aware else "naive",
                                other_source,
                                "aware" if other_aware else "naive",
                            )
                        )
                    break
            if receipt.receipt_id in self._seen_receipt_ids[source]:
                logger.debug(
                    "Ignoring duplicate receipt %s from source %s",
                    receipt.receipt_id,
                    source,
                )
                return []

            is_stale = receipt.revision < self._highest_revision[source]
            if is_stale:
                logger.warning(
                    "Late out-of-order receipt %s revision %s from source %s: "
                    "kept for audit but cannot displace revision %s",
                    receipt.receipt_id,
                    receipt.revision,
                    source,
                    self._highest_revision[source],
                )
                self._stale_receipt_ids[source].add(receipt.receipt_id)

            self._receipts[source].append(receipt)
            self._seen_receipt_ids[source].add(receipt.receipt_id)
            self._highest_revision[source] = max(
                self._highest_revision[source], receipt.revision
            )
            return self._reconcile()

    #-- window math -------------------------------------------------------

    def window_for(self, moment: datetime.datetime) -> datetime.datetime:
        """Return the start of the window containing ``moment``."""
        epoch = self._EPOCH
        if moment.tzinfo is None:
            epoch = epoch.replace(tzinfo=None)
        delta = moment - epoch
        aligned = (delta // self.window_size) * self.window_size
        return epoch + aligned

    def _sealed_window_for(
        self, watermark_time: datetime.datetime
    ) -> datetime.datetime:
        """
        Return the earliest window sealed by a watermark at ``watermark_time``.

        A watermark on a window boundary seals the window ending at that
        boundary, not the window starting there.
        """
        containing = self.window_for(watermark_time)
        if containing == watermark_time:
            return containing - self.window_size
        return containing

    def window_end(self, window_start: datetime.datetime) -> datetime.datetime:
        return window_start + self.window_size

    def _window_stamps(
        self,
    ) -> Tuple[Optional[datetime.datetime], Optional[datetime.datetime]]:
        """Return (first sealed window start, common coverage horizon)."""
        if not self._receipts or not all(self._receipts.values()):
            return None, None
        if self._start is not None:
            if self.window_for(self._start) != self._start:
                raise ReconciliationError(
                    "start {} is not aligned to a window boundary".format(
                        self._start.isoformat()
                    )
                )
            earliest = self._start
        else:
            active_receipts = [
                receipt
                for receipts in self._receipts.values()
                for receipt in receipts
                if receipt.receipt_id
                not in self._stale_receipt_ids[receipt.source]
            ]
            earliest = min(
                self._sealed_window_for(receipt.business_time)
                for receipt in active_receipts
            )
        horizon = min(
            max(
                receipt.business_time
                for receipt in receipts
                if receipt.receipt_id not in self._stale_receipt_ids[source]
            )
            for source, receipts in self._receipts.items()
        )
        return earliest, horizon

    def _ready_windows(self) -> List[datetime.datetime]:
        earliest, horizon = self._window_stamps()
        if earliest is None or horizon is None:
            return []
        windows: List[datetime.datetime] = []
        start = earliest
        while self.window_end(start) <= horizon:
            windows.append(start)
            start = self.window_end(start)
        return windows

    def ready_windows(self) -> List[datetime.datetime]:
        """Return the starts of windows sealed by every required source."""
        with self._lock:
            return self._ready_windows()

    #-- selection and versioning -----------------------------------------

    def _candidates(self, source: str, window_end: datetime.datetime) -> List[Receipt]:
        stale = self._stale_receipt_ids[source]
        return [
            receipt
            for receipt in self._receipts[source]
            if receipt.receipt_id not in stale
            and receipt.business_time >= window_end
        ]

    @staticmethod
    def _revision_key(receipt: Receipt) -> Tuple[int, datetime.datetime, str]:
        return receipt.revision, receipt.published_at, receipt.receipt_id

    def _select_first(
        self, source: str, window_end: datetime.datetime
    ) -> Receipt:
        """
        Select the receipt that originally delivered this window.

        Among all receipts sealing the window, take the one with the lowest
        watermark -- the first reply that reached the window's end. When two
        replies share a watermark (a correction published before the window
        was ever reconciled), prefer the higher revision.
        """
        candidates = self._candidates(source, window_end)
        return min(
            candidates,
            key=lambda receipt: (
                receipt.business_time,
                -receipt.revision,
                receipt.published_at,
                receipt.receipt_id,
            ),
        )

    def _resolve(
        self,
        source: str,
        window_end: datetime.datetime,
        current: Optional[Receipt],
    ) -> Receipt:
        """
        Resolve the receipt a window should pin for one source.

        Without a pinned receipt, take the earliest sealing receipt.
        Otherwise follow the ``supersedes`` chain as far as it currently
        reaches, so only corrections of the pinned receipt can move it.
        """
        if current is None:
            return self._select_first(source, window_end)

        pinned = current
        log = self._receipts[source]
        stale = self._stale_receipt_ids[source]
        while True:
            successors = [
                receipt
                for receipt in log
                if receipt.supersedes == pinned.receipt_id
                and receipt.receipt_id not in stale
                and receipt.business_time >= window_end
            ]
            if not successors:
                return pinned
            pinned = max(successors, key=self._revision_key)

    def _reconcile(self) -> List[ReconciliationReport]:
        created: List[ReconciliationReport] = []
        for window_start in self._ready_windows():
            end = self.window_end(window_start)
            history = self._reports.setdefault(window_start, [])
            previous = history[-1] if history else None
            selection = {
                source: self._resolve(
                    source, end, previous.basis[source] if previous else None
                )
                for source in self._receipts
            }
            if previous is not None:
                if all(
                    previous.basis[source].receipt_id == receipt.receipt_id
                    for source, receipt in selection.items()
                ):
                    continue
                previous.status = STATUS_SUPERSEDED
                new_version = previous.version + 1
                previous.superseded_by = new_version
            else:
                new_version = 1

            report = ReconciliationReport(
                window_start=window_start,
                window_end=end,
                version=new_version,
                status=STATUS_CURRENT,
                basis=dict(selection),
                created_at=self._clock(),
            )
            history.append(report)
            created.append(report)
            self._dispatch(report)
        return created

    #-- read side ---------------------------------------------------------

    def receipts(self, source: str) -> Tuple[Receipt, ...]:
        """Return the full append-only receipt log of one source."""
        with self._lock:
            if source not in self._receipts:
                raise ReconciliationError("unknown source {!r}".format(source))
            return tuple(self._receipts[source])

    def stale_receipts(self, source: str) -> Tuple[Receipt, ...]:
        """
        Return receipts rejected as out of date on arrival.

        They remain in the audit log but never participate in window sealing
        or report selection.
        """
        with self._lock:
            if source not in self._receipts:
                raise ReconciliationError("unknown source {!r}".format(source))
            stale = self._stale_receipt_ids[source]
            return tuple(
                receipt
                for receipt in self._receipts[source]
                if receipt.receipt_id in stale
            )

    def reports(self) -> List[ReconciliationReport]:
        """Return every report version, ordered by window then version."""
        with self._lock:
            return [
                report
                for window_start in sorted(self._reports)
                for report in self._reports[window_start]
            ]

    def report_history(
        self, window_start: datetime.datetime
    ) -> List[ReconciliationReport]:
        """Return all versions ever produced for one window."""
        with self._lock:
            return list(self._reports.get(window_start, ()))

    def latest_report(
        self, window_start: datetime.datetime
    ) -> Optional[ReconciliationReport]:
        """Return the current report version of a window, if any."""
        with self._lock:
            history = self._reports.get(window_start)
            return history[-1] if history else None
