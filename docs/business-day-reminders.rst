Business-day reminders
======================

The :mod:`schedule.reminders` module turns a business calendar and a feed of
daily-report receipts into the list of reminders that buyers should see.  It
is independent from :class:`Scheduler`; :func:`schedule.reminders.install`
connects the two.

Calendars per source
--------------------

A :class:`BusinessCalendar` holds weekday rules, closures and make-up days.
Exceptions can be global or scoped to one source.  A scoped entry always wins
over a global entry for the same date, and it is never applied to another
source.

.. code-block:: python

    import datetime
    from schedule.reminders import BusinessCalendar, BusinessException

    calendar = BusinessCalendar(
        default_open_weekdays=(0, 1, 2, 3, 4),  # HQ: Monday to Friday
        source_open_weekdays={"store-17": (0, 1, 2, 3, 4, 5)},
        exceptions=[
            BusinessException.closure(datetime.date(2026, 10, 1)),
            BusinessException.closure(datetime.date(2026, 10, 2)),
            # Only this store works the make-up Saturday.
            BusinessException.makeup(datetime.date(2026, 10, 3), source="store-17"),
        ],
    )

``is_business_day``, ``business_days``, ``add_business_days`` and
``deadline_day`` all accept an optional ``source`` argument.  Make-up days are
business days for that source only; a closure is a non-business day for every
source it applies to.

Receipts and grace periods
--------------------------

A :class:`Receipt` names its source and may cover a single day, an inclusive
``(first, last)`` range, or an explicit iterable of dates.  Coverage is
matched against business days, so a range that contains a weekend clears only
the business days in it.

A report for business day ``D`` stays open until the end of the
``grace_business_days``-th business day after ``D`` (default: two).  The grace
period uses the same calendar, so a holiday or a make-up day moves the
deadline for that source.  A Friday report with two grace days is due on the
following Tuesday; if the following Monday is a holiday it is due on
Wednesday.

Receipts are revisioned by ``receipt_id``.  A later revision replaces the
earlier content of the same receipt.  An older revision that arrives late is
ignored rather than rolling the state back.  A revision whose
``submitted_at`` is in the future is not visible to an earlier poll.

.. code-block:: python

    from schedule.reminders import Receipt

    engine.add_receipt(
        Receipt(
            receipt_id="store-17-week-40",
            source="store-17",
            submitted_at=datetime.datetime(2026, 10, 9, 18, 30),
            covers=(datetime.date(2026, 10, 5), datetime.date(2026, 10, 9)),
        )
    )

Polling
-------

``ReminderEngine.poll(now, sources=None)`` returns a :class:`PollResult`
with:

* ``reminders``: at most one active card per source, each containing the
  uncovered business days and their deadlines.
* ``withdrawn``: cards that were active in the previous poll and are no
  longer needed, for example because a late revision covered all their days.

Calling ``poll`` repeatedly without changing coverage returns the same
active card ids and no withdrawals.  Polling twice in the same minute
therefore shows a buyer one still-valid card, not a duplicate.

.. code-block:: python

    from schedule.reminders import ReminderEngine, install

    engine = ReminderEngine(
        calendar,
        sources=("hq", "store-17"),
        start_dates={"hq": datetime.date(2026, 9, 28),
                     "store-17": datetime.date(2026, 9, 28)},
        grace_business_days=2,
    )

    def publish(result):
        for card in result.reminders:
            print("active", card.reminder_id, [i.business_day for i in card.items])
        for card in result.withdrawn:
            print("withdrawn", card.reminder_id)

    install(engine, schedule.default_scheduler, every="minute", publish=publish)
