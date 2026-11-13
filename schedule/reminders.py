"""
Daily-report reminder lists backed by a business calendar.

This module deliberately does not depend on the rest of :mod:`schedule`.  A
scheduler job can call :meth:`ReminderEngine.poll` once per minute (or once per
polling window) and publish the returned cards.

The important rules implemented here are:

* A source (for example a head office or a store) has its own weekdays and
  explicit business-day exceptions.  An exception scoped to a source never
  affects another source.
* A report for business day ``D`` may be submitted until the end of the
  ``grace_business_days``-th business day after ``D``.  Holidays and make-up
  days therefore move the deadline without special cases in the polling code.
* Receipts cover an inclusive range of calendar days.  Only business days in
  that range clear report obligations, and receipts are scoped to their source.
* Receipts can be revised.  A later revision replaces the earlier revision of
  the same receipt, even when it arrives after a reminder was published.  The
  reminder is withdrawn instead of leaving a stale card in a buyer's inbox.
* One source gets at most one active reminder card.  Repeated polling with the
  same state returns the same card id, so polling twice in one minute cannot
  create duplicate cards.
"""

import datetime
import hashlib
from typing import (
    Dict,
    FrozenSet,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
    Union,
)

DateLike = Union[datetime.date, datetime.datetime, str]
DayRange = Tuple[datetime.date, datetime.date]
Covers = Union[DateLike, Sequence[DateLike]]

_DEFAULT_WORKDAYS = frozenset((0, 1, 2, 3, 4))  # Monday through Friday
_ONE_DAY = datetime.timedelta(days=1)

__all__ = [
    "BusinessCalendar",
    "BusinessException",
    "PollResult",
    "Receipt",
    "Reminder",
    "ReminderEngine",
    "ReminderItem",
    "install",
]


def _as_date(value: DateLike) -> datetime.date:
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    if isinstance(value, str):
        try:
            return datetime.date.fromisoformat(value)
        except ValueError:
            raise ValueError(
                "expected an ISO date (YYYY-MM-DD), got {!r}".format(value)
            )
    raise TypeError("expected a date or datetime, got {!r}".format(value))


def _validate_weekdays(weekdays: Iterable[int]) -> FrozenSet[int]:
    result = frozenset(weekdays)
    if any(not isinstance(day, int) or day < 0 or day > 6 for day in result):
        raise ValueError("weekdays must be integers in the range 0..6")
    return result


class BusinessException:
    """An explicit opening or closing of a source's business day.

    ``is_open=True`` is a make-up working day (for example a Saturday shift);
    ``is_open=False`` is a holiday or another closure.  ``source=None`` makes
    the exception apply to every source.  A source-specific entry always wins
    over a global entry for the same date.
    """

    __slots__ = ("day", "is_open", "source", "note")

    def __init__(
        self,
        day: DateLike,
        is_open: bool,
        source: Optional[str] = None,
        note: str = "",
    ) -> None:
        if not isinstance(is_open, bool):
            raise TypeError("is_open must be a bool")
        self.day = _as_date(day)
        self.is_open = is_open
        self.source = source
        self.note = note

    @property
    def action(self) -> str:
        """``"open"`` for a make-up day, ``"closed"`` for a closure."""

        return "open" if self.is_open else "closed"

    @classmethod
    def closure(
        cls,
        day: DateLike,
        source: Optional[str] = None,
        note: str = "",
    ) -> "BusinessException":
        """Create a closure (holiday) exception."""

        return cls(day, False, source=source, note=note)

    @classmethod
    def makeup(
        cls,
        day: DateLike,
        source: Optional[str] = None,
        note: str = "",
    ) -> "BusinessException":
        """Create a make-up working day exception."""

        return cls(day, True, source=source, note=note)

    @classmethod
    def holiday(
        cls,
        day: DateLike,
        source: Optional[str] = None,
        note: str = "",
    ) -> "BusinessException":
        """Alias for :meth:`closure`."""

        return cls.closure(day, source=source, note=note)

    @classmethod
    def extra_working_day(
        cls,
        day: DateLike,
        source: Optional[str] = None,
        note: str = "",
    ) -> "BusinessException":
        """Alias for :meth:`makeup`."""

        return cls.makeup(day, source=source, note=note)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, BusinessException):
            return NotImplemented
        return (
            self.day == other.day
            and self.is_open == other.is_open
            and self.source == other.source
            and self.note == other.note
        )

    def __hash__(self) -> int:
        return hash((self.day, self.is_open, self.source, self.note))

    def __repr__(self) -> str:
        template = (
            "BusinessException(day={!r}, is_open={!r}, "
            "source={!r}, note={!r})"
        )
        return template.format(self.day, self.is_open, self.source, self.note)


class BusinessCalendar:
    """Business days for one or more sources.

    :param default_open_weekdays: Weekday numbers open for sources without a
        source-specific setting (Monday is ``0``).
    :param source_open_weekdays: Optional mapping of source name to its own
        set of open weekday numbers.
    :param exceptions: The business exception table.  Entries are either
        global (``source=None``) or scoped to one source.
    """

    def __init__(
        self,
        default_open_weekdays: Iterable[int] = _DEFAULT_WORKDAYS,
        source_open_weekdays: Optional[Mapping[str, Iterable[int]]] = None,
        exceptions: Iterable[BusinessException] = (),
    ) -> None:
        self.default_open_weekdays = _validate_weekdays(default_open_weekdays)
        self.source_open_weekdays: Dict[str, FrozenSet[int]] = {
            source: _validate_weekdays(weekdays)
            for source, weekdays in (source_open_weekdays or {}).items()
        }

        parsed: Dict[Tuple[datetime.date, Optional[str]], BusinessException] = {}
        for exception in exceptions:
            if not isinstance(exception, BusinessException):
                raise TypeError("exceptions must contain BusinessException objects")
            key = (exception.day, exception.source)
            previous = parsed.get(key)
            if previous is not None and previous.is_open != exception.is_open:
                raise ValueError(
                    "conflicting business exceptions for {} and source {!r}".format(
                        exception.day, exception.source
                    )
                )
            parsed[key] = exception

        self.exceptions: Tuple[BusinessException, ...] = tuple(parsed.values())
        self._exceptions_by_key = parsed

    def weekdays_for(self, source: Optional[str] = None) -> FrozenSet[int]:
        if source is not None and source in self.source_open_weekdays:
            return self.source_open_weekdays[source]
        return self.default_open_weekdays

    def exception_for(
        self, day: DateLike, source: Optional[str] = None
    ) -> Optional[BusinessException]:
        """Return the exception that applies, preferring a scoped entry."""

        normalized = _as_date(day)
        if source is not None:
            scoped = self._exceptions_by_key.get((normalized, source))
            if scoped is not None:
                return scoped
        return self._exceptions_by_key.get((normalized, None))

    def is_business_day(self, day: DateLike, source: Optional[str] = None) -> bool:
        normalized = _as_date(day)
        exception = self.exception_for(normalized, source)
        if exception is not None:
            return exception.is_open
        return normalized.weekday() in self.weekdays_for(source)

    def business_days(
        self,
        start: DateLike,
        end: DateLike,
        source: Optional[str] = None,
    ) -> Tuple[datetime.date, ...]:
        """Return business days in the inclusive calendar range."""

        first = _as_date(start)
        last = _as_date(end)
        if last < first:
            return ()
        result: List[datetime.date] = []
        current = first
        while current <= last:
            if self.is_business_day(current, source):
                result.append(current)
            current += _ONE_DAY
        return tuple(result)

    def add_business_days(
        self,
        day: DateLike,
        count: int,
        source: Optional[str] = None,
    ) -> datetime.date:
        """Move ``count`` business days after ``day`` (not counting ``day``)."""

        if not isinstance(count, int) or count < 0:
            raise ValueError("count must be a non-negative integer")
        current = _as_date(day)
        remaining = count
        while remaining:
            current += _ONE_DAY
            if self.is_business_day(current, source):
                remaining -= 1
        return current

    def next_business_day(
        self, day: DateLike, source: Optional[str] = None
    ) -> datetime.date:
        """Return the first business day strictly after ``day``."""

        return self.add_business_days(day, 1, source)

    def previous_business_day(
        self, day: DateLike, source: Optional[str] = None
    ) -> datetime.date:
        """Return the last business day on or before ``day``."""

        current = _as_date(day)
        for _ in range(366 * 20):
            if self.is_business_day(current, source):
                return current
            current -= _ONE_DAY
        raise ValueError("no business day found within 20 years before {}".format(day))

    def current_business_day(
        self, day: DateLike, source: Optional[str] = None
    ) -> datetime.date:
        """The business day that ``day`` belongs to for a source.

        On a closure the previous open day is returned.  A make-up day for a
        source is itself a business day for that source only.
        """

        return self.previous_business_day(day, source)

    def deadline_day(
        self,
        business_day: DateLike,
        source: Optional[str] = None,
        grace_business_days: int = 2,
    ) -> datetime.date:
        """Last business day on which a report may be submitted.

        The grace period starts after ``business_day``.  A Friday report with
        two grace days is due on the following Tuesday; holidays in between
        are skipped for the source.
        """

        return self.add_business_days(business_day, grace_business_days, source)


class Receipt:
    """A submitted daily-report receipt.

    :param receipt_id: Stable receipt identity.  Revisions keep this id.
    :param source: The calendar/source the receipt belongs to.
    :param submitted_at: When the receipt was submitted.
    :param covers: A date, an inclusive ``(first, last)`` date range, or an
        iterable of dates.  A two-item tuple is treated as an inclusive range;
        a list or other iterable is treated as explicit dates.  Receipts that
        cover several consecutive business days are represented by a range.
    :param revision: Monotonically increasing revision number.  A later
        revision replaces this receipt's earlier revision.
    """

    __slots__ = ("receipt_id", "source", "submitted_at", "revision", "_range", "_days")

    def __init__(
        self,
        receipt_id: str,
        source: str,
        submitted_at: datetime.datetime,
        covers: Covers,
        revision: int = 1,
    ) -> None:
        if not isinstance(receipt_id, str) or not receipt_id:
            raise ValueError("receipt_id must be a non-empty string")
        if not isinstance(source, str) or not source:
            raise ValueError("source must be a non-empty string")
        if not isinstance(submitted_at, datetime.datetime):
            raise TypeError("submitted_at must be a datetime")
        if not isinstance(revision, int) or revision < 1:
            raise ValueError("revision must be a positive integer")

        self.receipt_id = receipt_id
        self.source = source
        self.submitted_at = submitted_at
        self.revision = revision
        self._range, self._days = self._normalize_covers(covers)

    @staticmethod
    def _normalize_covers(
        covers: Covers,
    ) -> Tuple[Optional[DayRange], Tuple[datetime.date, ...]]:
        if isinstance(covers, (datetime.date, datetime.datetime, str)):
            day = _as_date(covers)
            return (day, day), ()
        if isinstance(covers, tuple) and len(covers) == 2:
            first = _as_date(covers[0])
            last = _as_date(covers[1])
            if last < first:
                first, last = last, first
            return (first, last), ()
        if isinstance(covers, bytes):
            raise TypeError("covers must contain dates")
        try:
            days = tuple(sorted({_as_date(day) for day in covers}))
        except TypeError:
            raise TypeError("covers must be a date, a date range, or dates")
        if not days:
            raise ValueError("covers must contain at least one date")
        return None, days

    @property
    def covers_range(self) -> Optional[DayRange]:
        return self._range

    @property
    def covered_days(self) -> Tuple[datetime.date, ...]:
        """All calendar dates covered by the receipt (ranges are expanded)."""

        if self._range is None:
            return self._days
        first, last = self._range
        result: List[datetime.date] = []
        current = first
        while current <= last:
            result.append(current)
            current += _ONE_DAY
        return tuple(result)

    def covers_day(self, day: DateLike) -> bool:
        normalized = _as_date(day)
        if self._range is not None:
            return self._range[0] <= normalized <= self._range[1]
        return normalized in self._days

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Receipt):
            return NotImplemented
        return (
            self.receipt_id == other.receipt_id
            and self.source == other.source
            and self.submitted_at == other.submitted_at
            and self.revision == other.revision
            and self._range == other._range
            and self._days == other._days
        )

    def __repr__(self) -> str:
        coverage = self._range if self._range is not None else self._days
        template = (
            "Receipt(receipt_id={!r}, source={!r}, submitted_at={!r}, "
            "covers={!r}, revision={!r})"
        )
        return template.format(
            self.receipt_id,
            self.source,
            self.submitted_at,
            coverage,
            self.revision,
        )


class ReminderItem:
    """One uncovered daily report."""

    __slots__ = ("source", "business_day", "deadline_day")

    def __init__(
        self,
        source: str,
        business_day: DateLike,
        deadline_day: DateLike,
    ) -> None:
        self.source = source
        self.business_day = _as_date(business_day)
        self.deadline_day = _as_date(deadline_day)

    def _key(self) -> Tuple[str, datetime.date, datetime.date]:
        return (self.source, self.business_day, self.deadline_day)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ReminderItem):
            return NotImplemented
        return self._key() == other._key()

    def __hash__(self) -> int:
        return hash(self._key())

    def __repr__(self) -> str:
        return "ReminderItem(source={!r}, business_day={!r}, deadline_day={!r})".format(
            self.source, self.business_day, self.deadline_day
        )


class Reminder:
    """One buyer-facing reminder card for one source."""

    __slots__ = ("reminder_id", "source", "items", "created_at", "status", "revoked_at")

    def __init__(
        self,
        reminder_id: str,
        source: str,
        items: Tuple[ReminderItem, ...],
        created_at: datetime.datetime,
        status: str = "active",
        revoked_at: Optional[datetime.datetime] = None,
    ) -> None:
        self.reminder_id = reminder_id
        self.source = source
        self.items = items
        self.created_at = created_at
        self.status = status
        self.revoked_at = revoked_at

    @property
    def is_active(self) -> bool:
        return self.status == "active"

    def _revoked_copy(self, now: datetime.datetime) -> "Reminder":
        return Reminder(
            self.reminder_id,
            self.source,
            self.items,
            self.created_at,
            status="withdrawn",
            revoked_at=now,
        )

    def _key(self) -> Tuple[str, str, Tuple[ReminderItem, ...], datetime.datetime, str]:
        return (
            self.reminder_id,
            self.source,
            self.items,
            self.created_at,
            self.status,
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Reminder):
            return NotImplemented
        return self._key() == other._key()

    def __hash__(self) -> int:
        return hash(self._key())

    def __repr__(self) -> str:
        template = "Reminder(reminder_id={!r}, source={!r}, items={!r}, status={!r})"
        return template.format(
            self.reminder_id, self.source, self.items, self.status
        )


class PollResult:
    """The outcome of one polling pass."""

    __slots__ = ("polled_at", "reminders", "withdrawn")

    def __init__(
        self,
        polled_at: datetime.datetime,
        reminders: Tuple[Reminder, ...],
        withdrawn: Tuple[Reminder, ...],
    ) -> None:
        self.polled_at = polled_at
        self.reminders = reminders
        self.withdrawn = withdrawn

    def for_source(self, source: str) -> Optional[Reminder]:
        for reminder in self.reminders:
            if reminder.source == source:
                return reminder
        return None

    def __repr__(self) -> str:
        return "PollResult(polled_at={!r}, reminders={!r}, withdrawn={!r})".format(
            self.polled_at, self.reminders, self.withdrawn
        )


class ReminderEngine:
    """Build reminder cards from a calendar and the receipts seen so far.

    :param calendar: Shared business calendar.
    :param receipts: Receipts already ingested.  They are evaluated as of the
        poll time, so a receipt whose ``submitted_at`` is in the future does
        not clear an item yet.
    :param start_date: First report day to consider when a source has no
        source-specific start date.  If omitted, the earliest submitted
        receipt date is used, falling back to the poll date.
    :param start_dates: Optional mapping of source to its first report day.
    :param grace_business_days: Number of business days after a report day
        during which submission is still allowed.  Defaults to two.
    """

    def __init__(
        self,
        calendar: BusinessCalendar,
        receipts: Iterable[Receipt] = (),
        *,
        sources: Iterable[str] = (),
        start_date: Optional[DateLike] = None,
        start_dates: Optional[Mapping[str, DateLike]] = None,
        grace_business_days: int = 2,
    ) -> None:
        if not isinstance(calendar, BusinessCalendar):
            raise TypeError("calendar must be a BusinessCalendar")
        if not isinstance(grace_business_days, int) or grace_business_days < 0:
            raise ValueError("grace_business_days must be a non-negative integer")

        self.calendar = calendar
        self.grace_business_days = grace_business_days
        self.sources: Tuple[str, ...] = self._validate_sources(sources)
        self.start_date = None if start_date is None else _as_date(start_date)
        self.start_dates: Dict[str, datetime.date] = {
            source: _as_date(day)
            for source, day in (start_dates or {}).items()
        }

        # key: (source, receipt_id); value: latest version seen
        self._receipts: Dict[Tuple[str, str], Receipt] = {}
        self._receipt_history: Dict[Tuple[str, str], List[Receipt]] = {}
        self._active: Dict[str, Reminder] = {}

        for receipt in receipts:
            self.add_receipt(receipt)

    @staticmethod
    def _version(receipt: Receipt) -> Tuple[int, datetime.datetime]:
        """Ordering used to resolve competing versions of one receipt."""

        return (receipt.revision, receipt.submitted_at)

    def add_receipt(self, receipt: Receipt) -> bool:
        """Ingest a receipt or revision.

        Returns ``True`` when the stored state changed.  An older or identical
        version is ignored, which makes ingestion idempotent when a feed
        replays receipts.  A later submission with the same revision number is
        accepted as a correction and wins on ``submitted_at``; this keeps feeds
        that do not maintain revision counters usable.
        """

        if not isinstance(receipt, Receipt):
            raise TypeError("receipt must be a Receipt")

        key = (receipt.source, receipt.receipt_id)
        history = self._receipt_history.setdefault(key, [])
        version = self._version(receipt)

        for existing in history:
            if self._version(existing) == version:
                if existing == receipt:
                    return False
                raise ValueError(
                    "receipt {!r} for source {!r} was already ingested with "
                    "different content at revision {} and submitted_at {}".format(
                        receipt.receipt_id,
                        receipt.source,
                        receipt.revision,
                        receipt.submitted_at,
                    )
                )

        current = self._receipts.get(key)
        if current is not None and version < self._version(current):
            # A revision that arrives late but is older than the current one
            # must not roll the receipt back.
            return False

        history.append(receipt)
        self._receipts[key] = receipt
        return True

    def register_source(self, source: str, start_date: DateLike) -> None:
        """Register a source and the first day for which it owes reports."""

        if not isinstance(source, str) or not source:
            raise ValueError("source must be a non-empty string")
        self.start_dates[source] = _as_date(start_date)

    @staticmethod
    def _validate_sources(sources: Iterable[str]) -> Tuple[str, ...]:
        result: List[str] = []
        for source in sources:
            if not isinstance(source, str) or not source:
                raise ValueError("sources must contain non-empty strings")
            if source not in result:
                result.append(source)
        return tuple(result)

    def _sources(self) -> Tuple[str, ...]:
        known: Set[str] = set(self.sources)
        known.update(self.start_dates)
        known.update(self.calendar.source_open_weekdays)
        known.update(
            exception.source
            for exception in self.calendar.exceptions
            if exception.source is not None
        )
        known.update(source for source, _ in self._receipts.keys())
        known.update(self._active.keys())
        return tuple(sorted(known))

    def _start_for(self, source: str, now: datetime.datetime) -> datetime.date:
        if source in self.start_dates:
            return self.start_dates[source]
        if self.start_date is not None:
            return self.start_date
        covered_dates = [
            receipt.covered_days[0]
            for (receipt_source, _), history in self._receipt_history.items()
            if receipt_source == source
            for receipt in history
            if receipt.submitted_at <= now and receipt.covered_days
        ]
        if covered_dates:
            return min(covered_dates)
        return now.date()

    def _receipts_as_of(
        self, now: datetime.datetime
    ) -> Tuple[Dict[Tuple[str, str], Receipt], Dict[str, Set[datetime.date]]]:
        """Select the effective revision per receipt as of ``now``.

        A revision with a future ``submitted_at`` is not visible yet, so it
        cannot withdraw or add coverage prematurely.  Coverage is materialized
        per source for cheap lookups during a polling pass.
        """

        selected: Dict[Tuple[str, str], Receipt] = {}
        coverage: Dict[str, Set[datetime.date]] = {}
        for key, history in self._receipt_history.items():
            visible = [
                receipt for receipt in history if receipt.submitted_at <= now
            ]
            if not visible:
                continue
            receipt = max(visible, key=self._version)
            selected[key] = receipt
            covered = coverage.setdefault(receipt.source, set())
            for day in receipt.covered_days:
                covered.add(day)
        return selected, coverage

    def _items_for_source(
        self,
        source: str,
        now: datetime.datetime,
        coverage: Set[datetime.date],
    ) -> Tuple[ReminderItem, ...]:
        start = self._start_for(source, now)
        # On a closure the previous business day is still the one being
        # evaluated.  It is not late merely because the branch is closed.
        evaluated_day = self.calendar.current_business_day(now.date(), source)
        if evaluated_day < start:
            return ()

        items: List[ReminderItem] = []
        for business_day in self.calendar.business_days(start, evaluated_day, source):
            deadline = self.calendar.deadline_day(
                business_day, source, self.grace_business_days
            )
            if now.date() <= deadline:
                # The submission window is still open (it may end today).
                continue
            if business_day in coverage:
                continue
            items.append(ReminderItem(source, business_day, deadline))
        return tuple(items)

    @staticmethod
    def _make_id(
        source: str, items: Tuple[ReminderItem, ...], now: datetime.datetime
    ) -> str:
        minute = now.replace(second=0, microsecond=0)
        payload = "|".join(
            "{}/{}/{}".format(
                item.source,
                item.business_day.isoformat(),
                item.deadline_day.isoformat(),
            )
            for item in items
        )
        digest = hashlib.sha1(
            "{}\n{}\n{}".format(source, minute.isoformat(), payload).encode("utf-8")
        ).hexdigest()[:10]
        return "{}-{}-{}".format(source, minute.strftime("%Y%m%dT%H%M"), digest)

    def poll(
        self,
        now: Optional[datetime.datetime] = None,
        sources: Optional[Iterable[str]] = None,
    ) -> PollResult:
        """Generate the active reminder list for a polling pass.

        Calling this repeatedly with the same state returns the same active
        card id and no withdrawal events.  When coverage changes, the old card
        is returned in ``withdrawn`` and replaced by at most one new card for
        that source.
        """

        polled_at = datetime.datetime.now() if now is None else now
        if not isinstance(polled_at, datetime.datetime):
            raise TypeError("now must be a datetime")

        if sources is None:
            selected_sources = self._sources()
        else:
            selected_sources = tuple(sorted({source for source in sources}))
            for source in selected_sources:
                if not isinstance(source, str) or not source:
                    raise ValueError("sources must contain non-empty strings")

        _, coverage = self._receipts_as_of(polled_at)

        active: List[Reminder] = []
        withdrawn: List[Reminder] = []

        for source in selected_sources:
            items = self._items_for_source(
                source, polled_at, coverage.get(source, set())
            )
            previous = self._active.get(source)

            if not items:
                if previous is not None:
                    revoked = previous._revoked_copy(polled_at)
                    del self._active[source]
                    withdrawn.append(revoked)
                continue

            if previous is not None and previous.items == items:
                active.append(previous)
                continue

            if previous is not None:
                revoked = previous._revoked_copy(polled_at)
                withdrawn.append(revoked)

            reminder = Reminder(
                self._make_id(source, items, polled_at),
                source,
                items,
                polled_at,
                status="active",
            )
            self._active[source] = reminder
            active.append(reminder)

        return PollResult(
            polled_at=polled_at,
            reminders=tuple(active),
            withdrawn=tuple(withdrawn),
        )


def install(
    engine: ReminderEngine,
    scheduler,
    every: str = "minute",
    sources: Optional[Iterable[str]] = None,
    publish=None,
):
    """Register ``engine.poll`` as a recurring scheduler job.

    ``publish`` receives each :class:`PollResult`.  The helper is deliberately
    small: it shows the intended integration without adding scheduler-specific
    state to the engine.
    """

    job = getattr(scheduler.every(), every)

    def poll_and_publish():
        result = engine.poll(sources=sources)
        if publish is not None:
            publish(result)
        return result

    return job.do(poll_and_publish)
