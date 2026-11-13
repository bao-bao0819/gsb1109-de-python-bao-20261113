"""Tests for watermark driven reconciliation scheduling."""

import datetime as dt
from unittest import TestCase

from schedule.reconciliation import (
    ReconciliationError,
    ReconciliationReport,
    ReconciliationScheduler,
    STATUS_CURRENT,
    STATUS_SUPERSEDED,
    Watermark,
    make_receipt,
)

UTC = dt.timezone.utc
HOUR = dt.timedelta(hours=1)


def t(hour, minute=0, day=13):
    return dt.datetime(2026, 11, day, hour, minute, tzinfo=UTC)


class FakeClock:
    def __init__(self, start):
        self.current = start

    def __call__(self):
        stamp = self.current
        self.current += dt.timedelta(seconds=1)
        return stamp


class ReconciliationSchedulerTests(TestCase):
    def setUp(self):
        self.clock = FakeClock(t(12, 0))
        self.scheduler = ReconciliationScheduler(
            HOUR, start=t(10), clock=self.clock
        )
        self.scheduler.register_source("payments")
        self.scheduler.register_source("ledger")

    def test_window_alignment_follows_epoch(self):
        self.assertEqual(
            self.scheduler.window_for(t(10, 30)),
            t(10, 0),
        )
        self.assertEqual(
            self.scheduler.window_end(t(10, 0)),
            t(11, 0),
        )

    def test_invalid_window_size_rejected(self):
        with self.assertRaises(ReconciliationError):
            ReconciliationScheduler(dt.timedelta(0))
        with self.assertRaises(ReconciliationError):
            ReconciliationScheduler(dt.timedelta(hours=-1))

    def test_misaligned_start_rejected(self):
        scheduler = ReconciliationScheduler(HOUR, start=t(10, 30))
        scheduler.register_source("payments")
        scheduler.register_source("ledger")
        scheduler.observe_receipt(make_receipt("payments", t(11), t(10, 5)))
        with self.assertRaises(ReconciliationError):
            scheduler.observe_receipt(make_receipt("ledger", t(11), t(10, 6)))

    def test_without_start_only_windows_sealed_by_every_source_are_used(self):
        scheduler = ReconciliationScheduler(HOUR, clock=self.clock)
        scheduler.register_source("payments")
        scheduler.register_source("ledger")
        scheduler.observe_receipt(make_receipt("payments", t(13), t(12, 1)))
        reports = scheduler.observe_receipt(
            make_receipt("ledger", t(12), t(12, 2))
        )
        self.assertEqual(
            [report.window_start for report in reports], [t(11)]
        )

    def test_duplicate_source_registration_rejected(self):
        with self.assertRaises(ReconciliationError):
            self.scheduler.register_source("payments")

    def test_unknown_source_rejected(self):
        stray = make_receipt("mystery", t(11), t(10, 5))
        with self.assertRaises(ReconciliationError):
            self.scheduler.observe_receipt(stray)

    def test_no_report_until_every_required_source_seals_window(self):
        reports = self.scheduler.observe_receipt(
            make_receipt("payments", t(11), t(10, 5), batch_ids=["p1"])
        )
        self.assertEqual(reports, [])
        self.assertEqual(self.scheduler.ready_windows(), [])

        reports = self.scheduler.observe_receipt(
            make_receipt("ledger", t(11), t(10, 6), empty=True)
        )
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0].window_start, t(10))
        self.assertEqual(reports[0].window_end, t(11))

    def test_empty_batch_watermark_seals_window(self):
        empty = make_receipt("payments", t(11), t(10, 5), empty=True)
        self.assertTrue(empty.empty)
        self.scheduler.observe_receipt(empty)
        self.scheduler.observe_receipt(
            make_receipt("ledger", t(11), t(10, 6), empty=True)
        )
        report = self.scheduler.latest_report(t(10))
        self.assertIsNotNone(report)
        self.assertTrue(report.receipt_for("payments").empty)

    def test_listener_dispatched_once_per_report_version(self):
        dispatched = []
        self.scheduler.add_listener(dispatched.append)

        self.scheduler.observe_receipt(
            make_receipt("payments", t(12), t(10, 5))
        )
        self.assertEqual(dispatched, [])

        self.scheduler.observe_receipt(
            make_receipt("ledger", t(12), t(10, 6))
        )
        self.assertEqual(len(dispatched), 2)
        self.assertEqual(
            [report.window_start for report in dispatched],
            [t(10), t(11)],
        )

    def test_sources_polled_independently_with_own_cadence(self):
        payments_calls = []
        ledger_calls = []

        def payments_fetcher():
            payments_calls.append(1)
            if len(payments_calls) == 1:
                return None
            return make_receipt("payments", t(11), t(10, 5))

        def ledger_fetcher():
            ledger_calls.append(1)
            return make_receipt("ledger", t(11), t(10, 7))

        self.assertEqual(self.scheduler.poll_source("payments", payments_fetcher), [])
        self.assertEqual(
            self.scheduler.poll_source("payments", payments_fetcher), []
        )
        self.assertEqual(
            self.scheduler.poll_source("ledger", ledger_fetcher),
            self.scheduler.report_history(t(10)),
        )
        self.assertEqual(len(payments_calls), 2)
        self.assertEqual(len(ledger_calls), 1)

    def test_slow_source_holds_back_all_windows_then_catches_up(self):
        self.scheduler.observe_receipt(
            make_receipt("payments", t(13), t(12, 1))
        )
        self.assertEqual(self.scheduler.ready_windows(), [])

        reports = self.scheduler.observe_receipt(
            make_receipt("ledger", t(12), t(12, 2))
        )
        self.assertEqual(
            [report.window_start for report in reports],
            [t(10), t(11)],
        )

    def test_first_report_pins_highest_revision_sealing_receipt(self):
        early = make_receipt(
            "payments", t(11), t(10, 1), revision=1, receipt_id="p-old"
        )
        corrected_before_close = make_receipt(
            "payments",
            t(11),
            t(10, 4),
            revision=2,
            receipt_id="p-new",
            supersedes="p-old",
        )
        self.scheduler.observe_receipt(early)
        self.scheduler.observe_receipt(corrected_before_close)
        self.scheduler.observe_receipt(make_receipt("ledger", t(11), t(10, 5)))

        report = self.scheduler.latest_report(t(10))
        self.assertEqual(report.version, 1)
        self.assertEqual(report.receipt_for("payments").receipt_id, "p-new")

    def test_correction_only_versions_windows_built_on_replaced_receipt(self):
        self._build_two_window_baseline()

        correction = make_receipt(
            "payments",
            t(11, 30),
            t(11, 30),
            revision=3,
            receipt_id="p-fix-w1",
            supersedes="p-w1",
        )
        created = self.scheduler.observe_receipt(correction)

        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].window_start, t(10))
        self.assertEqual(created[0].version, 2)
        self.assertEqual(
            created[0].receipt_for("payments").receipt_id, "p-fix-w1"
        )

        window_two = self.scheduler.latest_report(t(11))
        self.assertEqual(window_two.version, 1)
        self.assertEqual(
            window_two.receipt_for("payments").receipt_id, "p-w2"
        )

        history_w1 = self.scheduler.report_history(t(10))
        self.assertEqual([r.version for r in history_w1], [1, 2])
        self.assertEqual(history_w1[0].status, STATUS_SUPERSEDED)
        self.assertEqual(history_w1[0].superseded_by, 2)
        self.assertEqual(history_w1[1].status, STATUS_CURRENT)
        self.assertIsNone(history_w1[1].superseded_by)

    def test_correction_chain_follows_multiple_generations(self):
        self._build_two_window_baseline()
        fix_one = make_receipt(
            "payments",
            t(11, 30),
            t(11, 30),
            revision=3,
            receipt_id="p-fix-1",
            supersedes="p-w1",
        )
        fix_two = make_receipt(
            "payments",
            t(11, 30),
            t(11, 45),
            revision=4,
            receipt_id="p-fix-2",
            supersedes="p-fix-1",
        )
        self.scheduler.observe_receipt(fix_one)
        created = self.scheduler.observe_receipt(fix_two)

        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].version, 3)
        self.assertEqual(
            created[0].receipt_for("payments").receipt_id, "p-fix-2"
        )
        self.assertEqual(
            [r.version for r in self.scheduler.report_history(t(10))],
            [1, 2, 3],
        )

    def test_late_lower_revision_watermark_cannot_override_confirmed_report(self):
        self._build_two_window_baseline()
        self.scheduler.observe_receipt(
            make_receipt(
                "payments",
                t(11, 30),
                t(11, 30),
                revision=3,
                receipt_id="p-fix-w1",
                supersedes="p-w1",
            )
        )
        before = self.scheduler.latest_report(t(10))

        late = make_receipt(
            "payments",
            t(11),
            t(11, 50),
            revision=0,
            receipt_id="p-late-stale",
        )
        created = self.scheduler.observe_receipt(late)

        self.assertEqual(created, [])
        after = self.scheduler.latest_report(t(10))
        self.assertEqual(after.version, before.version)
        self.assertEqual(
            after.receipt_for("payments").receipt_id, "p-fix-w1"
        )
        logged_ids = [
            r.receipt_id for r in self.scheduler.receipts("payments")
        ]
        self.assertIn("p-late-stale", logged_ids)

    def test_duplicate_receipt_is_idempotent(self):
        receipt = make_receipt("payments", t(11), t(10, 5), receipt_id="dup")
        self.scheduler.observe_receipt(receipt)
        self.assertEqual(self.scheduler.observe_receipt(receipt), [])
        self.assertEqual(len(self.scheduler.receipts("payments")), 1)

    def test_observe_watermark_ingests_empty_seal(self):
        watermark = Watermark(
            source="payments",
            business_time=t(11),
            published_at=t(10, 5),
            revision=1,
            receipt_id="wm-empty",
            empty=True,
        )
        self.scheduler.observe_watermark(watermark)
        self.scheduler.observe_receipt(make_receipt("ledger", t(11), t(10, 6)))
        report = self.scheduler.latest_report(t(10))
        self.assertTrue(report.receipt_for("payments").empty)

    def test_stale_receipt_cannot_pull_back_coverage_horizon(self):
        self._build_two_window_baseline()
        self.assertEqual(
            self.scheduler.ready_windows(), [t(10), t(11)]
        )
        self.scheduler.observe_receipt(
            make_receipt(
                "payments",
                t(10),
                t(12, 30),
                revision=0,
                receipt_id="p-ancient",
            )
        )
        self.assertEqual(
            self.scheduler.ready_windows(), [t(10), t(11)]
        )
        self.assertEqual(
            [r.receipt_id for r in self.scheduler.stale_receipts("payments")],
            ["p-ancient"],
        )
        self.assertEqual(
            self.scheduler.latest_report(t(11)).receipt_for(
                "payments"
            ).receipt_id,
            "p-w2",
        )

    def test_supersede_that_does_not_seal_window_is_ignored_for_it(self):
        self._build_two_window_baseline()
        backward = make_receipt(
            "payments",
            t(10, 30),
            t(11, 40),
            revision=3,
            receipt_id="p-backward",
            supersedes="p-w1",
        )
        created = self.scheduler.observe_receipt(backward)
        self.assertEqual(created, [])
        self.assertEqual(
            self.scheduler.latest_report(t(10)).receipt_for(
                "payments"
            ).receipt_id,
            "p-w1",
        )

    def test_report_describes_exact_receipt_per_source(self):
        self._build_two_window_baseline()
        report = self.scheduler.latest_report(t(10))
        description = report.describe()

        self.assertEqual(description["window_start"], t(10).isoformat())
        self.assertEqual(description["version"], 1)
        self.assertEqual(description["status"], STATUS_CURRENT)
        self.assertEqual(
            sorted(description["sources"]), ["ledger", "payments"]
        )
        self.assertEqual(
            description["sources"]["payments"]["receipt_id"], "p-w1"
        )
        self.assertEqual(
            description["sources"]["payments"]["batch_ids"], ["p1"]
        )
        self.assertEqual(
            description["sources"]["ledger"]["receipt_id"], "l-cover"
        )
        self.assertTrue(description["sources"]["ledger"]["empty"])

    def test_receipt_log_is_append_only_audit_trail(self):
        first = make_receipt("payments", t(11), t(10, 1), receipt_id="p-w1")
        second = make_receipt(
            "payments",
            t(12),
            t(11, 1),
            revision=2,
            receipt_id="p-w2",
        )
        self.scheduler.observe_receipt(first)
        self.scheduler.observe_receipt(second)

        log = self.scheduler.receipts("payments")
        self.assertEqual([r.receipt_id for r in log], ["p-w1", "p-w2"])

    def test_watermark_requires_aware_or_naive_pair(self):
        with self.assertRaises(ReconciliationError):
            Watermark(
                source="payments",
                business_time=t(11),
                published_at=dt.datetime(2026, 11, 13, 10, 5),
                revision=1,
                receipt_id="bad",
            )

    def test_sources_must_share_datetime_awareness(self):
        self.scheduler.observe_receipt(
            make_receipt("payments", t(11), t(10, 5))
        )
        naive = make_receipt(
            "ledger",
            dt.datetime(2026, 11, 13, 11),
            dt.datetime(2026, 11, 13, 10, 6),
        )
        with self.assertRaises(ReconciliationError):
            self.scheduler.observe_receipt(naive)

    def test_start_awareness_must_match_receipts(self):
        scheduler = ReconciliationScheduler(
            HOUR, start=dt.datetime(2026, 11, 13, 10)
        )
        scheduler.register_source("payments")
        scheduler.register_source("ledger")
        with self.assertRaises(ReconciliationError):
            scheduler.observe_receipt(make_receipt("payments", t(11), t(10, 5)))

    def test_reports_listed_by_window_then_version(self):
        self._build_two_window_baseline()
        self.scheduler.observe_receipt(
            make_receipt(
                "payments",
                t(11, 30),
                t(11, 30),
                revision=3,
                receipt_id="p-fix-w1",
                supersedes="p-w1",
            )
        )
        reports = self.scheduler.reports()
        self.assertEqual(
            [
                (report.window_start, report.version)
                for report in reports
            ],
            [(t(10), 1), (t(10), 2), (t(11), 1)],
        )
        self.assertTrue(all(isinstance(r, ReconciliationReport) for r in reports))

    def test_listener_failure_does_not_break_dispatch(self):
        def broken(_report):
            raise RuntimeError("boom")

        seen = []
        self.scheduler.add_listener(broken)
        self.scheduler.add_listener(seen.append)
        self.scheduler.observe_receipt(make_receipt("payments", t(11), t(10, 5)))
        self.scheduler.observe_receipt(make_receipt("ledger", t(11), t(10, 6)))
        self.assertEqual(len(seen), 1)

    def test_make_receipt_generates_stable_id(self):
        receipt = make_receipt("payments", t(11), t(10, 5))
        self.assertTrue(receipt.receipt_id.startswith("payments:r1:"))
        self.assertEqual(receipt.metadata, {})

    def _build_two_window_baseline(self):
        self.scheduler.observe_receipt(
            make_receipt(
                "payments",
                t(11),
                t(10, 5),
                revision=1,
                receipt_id="p-w1",
                batch_ids=["p1"],
            )
        )
        self.scheduler.observe_receipt(
            make_receipt(
                "payments",
                t(12),
                t(11, 5),
                revision=2,
                receipt_id="p-w2",
                batch_ids=["p2"],
            )
        )
        self.scheduler.observe_receipt(
            make_receipt(
                "ledger",
                t(12),
                t(11, 10),
                revision=1,
                receipt_id="l-cover",
                empty=True,
            )
        )
        self.assertEqual(
            [r.window_start for r in self.scheduler.reports()],
            [t(10), t(11)],
        )
