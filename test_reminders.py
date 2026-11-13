"""Tests for the business-calendar reminder engine."""

import datetime
import unittest

from schedule.reminders import (
    BusinessCalendar,
    BusinessException,
    Receipt,
    ReminderEngine,
)


def dt(year, month, day, hour=9, minute=0, second=0):
    return datetime.datetime(year, month, day, hour, minute, second)


class BusinessCalendarTests(unittest.TestCase):
    def test_source_calendars_are_independent(self):
        calendar = BusinessCalendar(
            default_open_weekdays=(0, 1, 2, 3, 4),
            source_open_weekdays={"store": (0, 1, 2, 3, 4, 5)},
        )

        saturday = datetime.date(2026, 11, 14)
        self.assertTrue(calendar.is_business_day(saturday, "store"))
        self.assertFalse(calendar.is_business_day(saturday, "hq"))

    def test_global_and_source_specific_exceptions(self):
        calendar = BusinessCalendar(
            exceptions=[
                BusinessException.closure(datetime.date(2026, 10, 1)),
                BusinessException.makeup(datetime.date(2026, 10, 3), source="store"),
            ]
        )

        self.assertFalse(calendar.is_business_day(datetime.date(2026, 10, 1), "hq"))
        self.assertFalse(calendar.is_business_day(datetime.date(2026, 10, 1), "store"))
        # A make-up day only applies to the specified source.
        self.assertTrue(calendar.is_business_day(datetime.date(2026, 10, 3), "store"))
        self.assertFalse(calendar.is_business_day(datetime.date(2026, 10, 3), "hq"))

    def test_source_specific_exception_beats_global_entry(self):
        calendar = BusinessCalendar(
            exceptions=[
                BusinessException.closure(datetime.date(2026, 10, 3)),
                BusinessException.makeup(datetime.date(2026, 10, 3), source="store"),
            ]
        )

        self.assertFalse(calendar.is_business_day(datetime.date(2026, 10, 3), "hq"))
        self.assertTrue(calendar.is_business_day(datetime.date(2026, 10, 3), "store"))

    def test_add_business_days_skips_holidays(self):
        calendar = BusinessCalendar(
            exceptions=[BusinessException.closure(datetime.date(2026, 10, 1))]
        )

        # Wednesday Sep 30 plus two business days: Thursday is a holiday,
        # so Friday Oct 2 is the first and Monday Oct 5 the second.
        self.assertEqual(
            calendar.add_business_days(datetime.date(2026, 9, 30), 2),
            datetime.date(2026, 10, 5),
        )
        self.assertEqual(
            calendar.add_business_days(datetime.date(2026, 10, 2), 2),
            datetime.date(2026, 10, 6),
        )

    def test_previous_business_day_walks_back_over_holidays(self):
        calendar = BusinessCalendar(
            exceptions=[
                BusinessException.closure(datetime.date(2026, 10, 1)),
                BusinessException.closure(datetime.date(2026, 10, 2)),
            ]
        )

        self.assertEqual(
            calendar.previous_business_day(datetime.date(2026, 10, 3)),
            datetime.date(2026, 9, 30),
        )
        self.assertEqual(
            calendar.previous_business_day(datetime.date(2026, 10, 5)),
            datetime.date(2026, 10, 5),
        )

    def test_conflicting_exceptions_are_rejected(self):
        with self.assertRaises(ValueError):
            BusinessCalendar(
                exceptions=[
                    BusinessException.closure(datetime.date(2026, 10, 1)),
                    BusinessException.makeup(datetime.date(2026, 10, 1)),
                ]
            )

    def test_invalid_weekday_is_rejected(self):
        with self.assertRaises(ValueError):
            BusinessCalendar(default_open_weekdays=(0, 7))


class ReceiptTests(unittest.TestCase):
    def test_range_and_explicit_coverage(self):
        receipt = Receipt(
            "r1",
            "store",
            dt(2026, 10, 8),
            (datetime.date(2026, 10, 5), datetime.date(2026, 10, 6)),
        )
        self.assertTrue(receipt.covers_day(datetime.date(2026, 10, 5)))
        self.assertTrue(receipt.covers_day(datetime.date(2026, 10, 6)))
        self.assertFalse(receipt.covers_day(datetime.date(2026, 10, 7)))
        self.assertEqual(
            receipt.covered_days,
            (datetime.date(2026, 10, 5), datetime.date(2026, 10, 6)),
        )

    def test_single_date_and_explicit_iterable(self):
        single = Receipt("r1", "store", dt(2026, 10, 8), datetime.date(2026, 10, 5))
        self.assertEqual(single.covered_days, (datetime.date(2026, 10, 5),))
        self.assertEqual(single.covers_range, (datetime.date(2026, 10, 5),) * 2)

        explicit = Receipt(
            "r2",
            "store",
            dt(2026, 10, 8),
            [datetime.date(2026, 10, 6), datetime.date(2026, 10, 5)],
        )
        self.assertEqual(
            explicit.covered_days,
            (datetime.date(2026, 10, 5), datetime.date(2026, 10, 6)),
        )

    def test_reversed_range_is_normalized(self):
        receipt = Receipt(
            "r1",
            "store",
            dt(2026, 10, 8),
            (datetime.date(2026, 10, 6), datetime.date(2026, 10, 5)),
        )
        self.assertTrue(receipt.covers_day(datetime.date(2026, 10, 5)))
        self.assertTrue(receipt.covers_day(datetime.date(2026, 10, 6)))

    def test_empty_coverage_is_rejected(self):
        with self.assertRaises(ValueError):
            Receipt("r1", "store", dt(2026, 10, 8), [])


class ReminderEngineTests(unittest.TestCase):
    def setUp(self):
        self.calendar = BusinessCalendar(
            exceptions=[
                # A two-day holiday shared by HQ and the stores.
                BusinessException.closure(datetime.date(2026, 10, 1)),
                BusinessException.closure(datetime.date(2026, 10, 2)),
                # A make-up day that only the stores work.
                BusinessException.makeup(datetime.date(2026, 10, 3), source="store"),
            ]
        )

    def make_engine(self, start=datetime.date(2026, 10, 5), **kwargs):
        kwargs.setdefault("sources", ("hq", "store"))
        kwargs.setdefault("start_date", start)
        return ReminderEngine(self.calendar, **kwargs)

    def test_grace_period_starts_after_the_report_day(self):
        engine = self.make_engine(start=datetime.date(2026, 9, 28))

        # Monday Sep 28 is due on Wednesday Sep 30, so it is not late yet.
        result = engine.poll(dt(2026, 9, 30, 0, 0))
        self.assertEqual(result.reminders, ())
        self.assertEqual(result.withdrawn, ())

        # Thursday Oct 1 is a holiday, but the report is now late.
        result = engine.poll(dt(2026, 10, 1, 9, 0))
        reminder = result.for_source("hq")
        self.assertIsNotNone(reminder)
        self.assertEqual(
            [item.business_day for item in reminder.items],
            [datetime.date(2026, 9, 28)],
        )
        self.assertEqual(
            reminder.items[0].deadline_day,
            datetime.date(2026, 9, 30),
        )

    def test_holidays_extend_each_sources_deadline(self):
        engine = self.make_engine(start=datetime.date(2026, 9, 30))

        # Wednesday Sep 30 plus two business days.  The Thu/Fri holiday
        # pushes the HQ deadline to Tuesday Oct 6.  The store also works the
        # make-up Saturday Oct 3, so its deadline is Monday Oct 5.
        result = engine.poll(dt(2026, 10, 5, 23, 59))
        self.assertEqual(result.reminders, ())

        result = engine.poll(dt(2026, 10, 6, 23, 59))
        self.assertEqual(result.for_source("hq"), None)
        self.assertEqual(
            [(item.business_day, item.deadline_day)
             for item in result.for_source("store").items],
            [(datetime.date(2026, 9, 30), datetime.date(2026, 10, 5))],
        )

        result = engine.poll(dt(2026, 10, 7, 9, 0))
        self.assertEqual(
            [(item.business_day, item.deadline_day)
             for item in result.for_source("hq").items],
            [(datetime.date(2026, 9, 30), datetime.date(2026, 10, 6))],
        )

    def test_temporary_makeup_day_only_applies_to_its_source(self):
        engine = self.make_engine(start=datetime.date(2026, 9, 28))

        result = engine.poll(dt(2026, 10, 7, 9, 0))
        hq_days = [item.business_day for item in result.for_source("hq").items]
        store_days = [item.business_day for item in result.for_source("store").items]

        self.assertNotIn(datetime.date(2026, 10, 3), hq_days)
        self.assertIn(datetime.date(2026, 10, 3), store_days)

    def test_receipt_covering_consecutive_business_days(self):
        engine = self.make_engine()

        result = engine.poll(dt(2026, 10, 9, 9, 0))
        reminder = result.for_source("hq")
        self.assertEqual(
            [item.business_day for item in reminder.items],
            [datetime.date(2026, 10, 5), datetime.date(2026, 10, 6)],
        )

        engine.add_receipt(
            Receipt(
                "week-40",
                "hq",
                dt(2026, 10, 9, 9, 0),
                (datetime.date(2026, 10, 5), datetime.date(2026, 10, 6)),
            )
        )
        result = engine.poll(dt(2026, 10, 9, 9, 1))
        self.assertIsNone(result.for_source("hq"))
        self.assertEqual(len(result.withdrawn), 1)
        self.assertEqual(result.withdrawn[0].reminder_id, reminder.reminder_id)

    def test_receipt_only_clears_its_own_business_days(self):
        engine = self.make_engine()
        engine.add_receipt(
            Receipt(
                "hq-40",
                "hq",
                dt(2026, 10, 13, 9, 0),
                (datetime.date(2026, 10, 5), datetime.date(2026, 10, 7)),
            )
        )

        result = engine.poll(dt(2026, 10, 13, 9, 1))
        reminder = result.for_source("hq")
        self.assertEqual(
            [item.business_day for item in reminder.items],
            [datetime.date(2026, 10, 8)],
        )

    def test_late_revision_withdraws_a_no_longer_needed_reminder(self):
        engine = self.make_engine()

        result = engine.poll(dt(2026, 10, 12, 9, 0))
        reminder = result.for_source("hq")
        self.assertEqual(len(reminder.items), 3)

        # Revision 2 arrives after the first card was published and covers
        # every day in it.
        engine.add_receipt(
            Receipt(
                "week-40",
                "hq",
                dt(2026, 10, 12, 9, 30),
                (datetime.date(2026, 10, 5), datetime.date(2026, 10, 9)),
                revision=2,
            )
        )
        result = engine.poll(dt(2026, 10, 12, 9, 31))
        self.assertIsNone(result.for_source("hq"))
        self.assertEqual(len(result.withdrawn), 1)
        self.assertEqual(result.withdrawn[0].status, "withdrawn")

    def test_revision_can_shrink_a_card_without_withdrawing_it(self):
        engine = self.make_engine()

        first = engine.poll(dt(2026, 10, 12, 9, 0))
        old_id = first.for_source("hq").reminder_id

        engine.add_receipt(
            Receipt(
                "hq-40",
                "hq",
                dt(2026, 10, 12, 9, 30),
                (datetime.date(2026, 10, 5), datetime.date(2026, 10, 6)),
            )
        )
        result = engine.poll(dt(2026, 10, 12, 9, 31))
        reminder = result.for_source("hq")

        self.assertEqual(
            [item.business_day for item in reminder.items],
            [datetime.date(2026, 10, 7)],
        )
        self.assertNotEqual(reminder.reminder_id, old_id)
        self.assertEqual(len(result.withdrawn), 1)
        self.assertEqual(result.withdrawn[0].reminder_id, old_id)

    def test_late_but_older_revision_does_not_roll_back(self):
        engine = self.make_engine()
        engine.add_receipt(
            Receipt(
                "week-40",
                "hq",
                dt(2026, 10, 12),
                (datetime.date(2026, 10, 5), datetime.date(2026, 10, 9)),
                revision=2,
            )
        )
        self.assertFalse(
            engine.add_receipt(
                Receipt(
                    "week-40",
                    "hq",
                    dt(2026, 10, 12),
                    datetime.date(2026, 10, 5),
                    revision=1,
                )
            )
        )

        result = engine.poll(dt(2026, 10, 12, 9, 1))
        self.assertIsNone(result.for_source("hq"))

    def test_future_revision_is_not_visible_yet(self):
        engine = self.make_engine()
        engine.add_receipt(
            Receipt(
                "week-40",
                "hq",
                dt(2026, 10, 12, 10, 0),
                (datetime.date(2026, 10, 5), datetime.date(2026, 10, 9)),
                revision=2,
            )
        )

        result = engine.poll(dt(2026, 10, 12, 9, 0))
        self.assertEqual(len(result.for_source("hq").items), 3)

        result = engine.poll(dt(2026, 10, 12, 10, 1))
        self.assertIsNone(result.for_source("hq"))

    def test_duplicate_poll_in_same_minute_returns_one_still_active_card(self):
        engine = self.make_engine()

        first = engine.poll(dt(2026, 10, 12, 9, 0, 0))
        second = engine.poll(dt(2026, 10, 12, 9, 0, 30))
        third = engine.poll(dt(2026, 10, 12, 9, 0, 59))

        self.assertEqual(len(first.reminders), 2)
        self.assertEqual(len(second.reminders), 2)
        self.assertEqual(len(third.reminders), 2)
        self.assertEqual(
            first.for_source("hq").reminder_id,
            second.for_source("hq").reminder_id,
        )
        self.assertEqual(
            second.for_source("hq").reminder_id,
            third.for_source("hq").reminder_id,
        )
        self.assertEqual(second.withdrawn, ())
        self.assertEqual(third.withdrawn, ())

    def test_one_card_per_source_and_changed_coverage_replaces_card(self):
        engine = self.make_engine(grace_business_days=0)

        result = engine.poll(dt(2026, 10, 7, 9, 0))
        hq = result.for_source("hq")
        store = result.for_source("store")
        self.assertNotEqual(hq.reminder_id, store.reminder_id)
        self.assertEqual(
            [item.business_day for item in hq.items],
            [datetime.date(2026, 10, 5), datetime.date(2026, 10, 6)],
        )

        # A receipt for one HQ day shrinks (but does not empty) its card.
        engine.add_receipt(
            Receipt("hq-1", "hq", dt(2026, 10, 7, 9, 0), datetime.date(2026, 10, 5))
        )
        result = engine.poll(dt(2026, 10, 7, 9, 1))
        self.assertEqual(len(result.reminders), 2)
        self.assertEqual(
            [item.business_day for item in result.for_source("hq").items],
            [datetime.date(2026, 10, 6)],
        )
        self.assertEqual(len(result.withdrawn), 1)
        self.assertEqual(result.withdrawn[0].source, "hq")

    def test_poll_is_idempotent_when_nothing_changes(self):
        engine = self.make_engine()
        first = engine.poll(dt(2026, 10, 12, 9, 0))
        second = engine.poll(dt(2026, 10, 12, 9, 1))
        self.assertEqual(first.reminders, second.reminders)
        self.assertEqual(second.withdrawn, ())

    def test_source_filter_does_not_touch_other_sources(self):
        engine = self.make_engine()

        all_result = engine.poll(dt(2026, 10, 12, 9, 0))
        self.assertEqual(len(all_result.reminders), 2)

        store_only = engine.poll(dt(2026, 10, 12, 9, 1), sources=["store"])
        self.assertEqual(len(store_only.reminders), 1)
        self.assertEqual(store_only.reminders[0].source, "store")

        # The HQ card was not withdrawn just because it was filtered out.
        all_again = engine.poll(dt(2026, 10, 12, 9, 2))
        self.assertEqual(len(all_again.reminders), 2)
        self.assertEqual(all_again.withdrawn, ())

    def test_receipt_from_another_source_does_not_clear_items(self):
        engine = self.make_engine()
        engine.add_receipt(
            Receipt(
                "store-40",
                "store",
                dt(2026, 10, 12),
                (datetime.date(2026, 10, 5), datetime.date(2026, 10, 9)),
            )
        )

        result = engine.poll(dt(2026, 10, 12, 9, 0))
        self.assertIsNotNone(result.for_source("hq"))
        self.assertEqual(
            result.for_source("hq").items[0].business_day,
            datetime.date(2026, 10, 5),
        )
        self.assertIsNone(result.for_source("store"))

    def test_default_start_uses_earliest_covered_receipt_day(self):
        engine = ReminderEngine(
            self.calendar,
            sources=("hq",),
            grace_business_days=0,
        )
        engine.add_receipt(
            Receipt("hq-1", "hq", dt(2026, 10, 7), datetime.date(2026, 10, 5))
        )

        result = engine.poll(dt(2026, 10, 9, 9, 0))
        self.assertEqual(
            [item.business_day for item in result.for_source("hq").items],
            [
                datetime.date(2026, 10, 6),
                datetime.date(2026, 10, 7),
                datetime.date(2026, 10, 8),
            ],
        )

    def test_grace_zero_keeps_the_deadline_day_open(self):
        engine = self.make_engine(grace_business_days=0)

        result = engine.poll(dt(2026, 10, 6, 23, 59))
        self.assertEqual(
            [item.business_day for item in result.for_source("hq").items],
            [datetime.date(2026, 10, 5)],
        )

        result = engine.poll(dt(2026, 10, 7, 0, 0))
        self.assertEqual(
            [item.business_day for item in result.for_source("hq").items],
            [datetime.date(2026, 10, 5), datetime.date(2026, 10, 6)],
        )

    def test_overdue_items_are_still_chased_during_a_closure(self):
        engine = self.make_engine(
            start=datetime.date(2026, 9, 28), grace_business_days=0
        )

        # Oct 1 and Oct 2 are closures, but earlier reports are still late.
        result = engine.poll(dt(2026, 10, 2, 9, 0))
        self.assertEqual(
            [item.business_day for item in result.for_source("hq").items],
            [
                datetime.date(2026, 9, 28),
                datetime.date(2026, 9, 29),
                datetime.date(2026, 9, 30),
            ],
        )

    def test_items_and_deadlines_follow_the_calendar(self):
        engine = self.make_engine(start=datetime.date(2026, 9, 28))

        result = engine.poll(dt(2026, 10, 7, 9, 0))
        items = result.for_source("hq").items
        self.assertEqual(
            items,
            tuple(sorted(items, key=lambda item: item.business_day)),
        )
        self.assertEqual(
            [(item.business_day, item.deadline_day) for item in items],
            [
                (datetime.date(2026, 9, 28), datetime.date(2026, 9, 30)),
                (datetime.date(2026, 9, 29), datetime.date(2026, 10, 5)),
                (datetime.date(2026, 9, 30), datetime.date(2026, 10, 6)),
            ],
        )

    def test_same_revision_with_different_content_is_rejected(self):
        engine = self.make_engine()
        engine.add_receipt(
            Receipt("hq-1", "hq", dt(2026, 10, 7), datetime.date(2026, 10, 5))
        )
        with self.assertRaises(ValueError):
            engine.add_receipt(
                Receipt("hq-1", "hq", dt(2026, 10, 7), datetime.date(2026, 10, 6))
            )

    def test_replaying_an_identical_receipt_is_idempotent(self):
        engine = self.make_engine()
        receipt = Receipt(
            "hq-1", "hq", dt(2026, 10, 7), datetime.date(2026, 10, 5)
        )
        self.assertTrue(engine.add_receipt(receipt))
        self.assertFalse(engine.add_receipt(receipt))

    def test_newer_submission_with_same_revision_is_a_correction(self):
        engine = self.make_engine()
        engine.add_receipt(
            Receipt(
                "hq-40",
                "hq",
                dt(2026, 10, 8),
                (datetime.date(2026, 10, 5), datetime.date(2026, 10, 5)),
            )
        )
        self.assertTrue(
            engine.add_receipt(
                Receipt(
                    "hq-40",
                    "hq",
                    dt(2026, 10, 9),
                    (datetime.date(2026, 10, 5), datetime.date(2026, 10, 6)),
                )
            )
        )

        result = engine.poll(dt(2026, 10, 12, 9, 0))
        self.assertEqual(
            [item.business_day for item in result.for_source("hq").items],
            [datetime.date(2026, 10, 7)],
        )

    def test_iso_date_strings_are_accepted(self):
        calendar = BusinessCalendar(
            exceptions=[BusinessException.closure("2026-10-01")]
        )
        self.assertFalse(
            calendar.is_business_day(datetime.date(2026, 10, 1), "hq")
        )

        receipt = Receipt(
            "hq-1",
            "hq",
            dt(2026, 10, 7),
            ("2026-10-05", "2026-10-06"),
        )
        self.assertTrue(receipt.covers_day(datetime.date(2026, 10, 5)))
        self.assertTrue(receipt.covers_day("2026-10-06"))
        self.assertFalse(receipt.covers_day("2026-10-07"))


if __name__ == "__main__":
    unittest.main()
