"""Unit tests for the backfill drill (schedule.drill)."""

import datetime
from unittest import TestCase, mock

import schedule
from schedule.drill import (
    BackfillDrill,
    DetailSpec,
    DrillContext,
    DrillError,
    MissingInputError,
    Receipt,
    STATUS_BLOCKED,
    STATUS_FAILED,
    STATUS_MISSING,
    STATUS_SUCCESS,
    run_drill,
)
from test_schedule import mock_datetime


def make_receipt(name="receipt", version="v1", payload=None):
    return Receipt(
        name=name,
        version=version,
        produced_at=datetime.datetime(2026, 11, 13, 8, 0, 0),
        payload=payload if payload is not None else {},
    )


class DrillReceiptTests(TestCase):
    def test_receipt_requires_name_and_version(self):
        with self.assertRaises(DrillError):
            Receipt(name="", version="v1", produced_at=datetime.datetime.now())
        with self.assertRaises(DrillError):
            Receipt(name="n", version="", produced_at=datetime.datetime.now())
        with self.assertRaises(DrillError):
            Receipt(name="n", version="v1", produced_at="not-a-datetime")

    def test_receipt_defaults_payload_to_empty_mapping(self):
        receipt = make_receipt()
        self.assertEqual(receipt.payload, {})

    def test_context_receipt_raises_when_absent(self):
        context = DrillContext(datetime.date(2026, 11, 13), {})
        with self.assertRaises(MissingInputError) as caught:
            context.receipt("missing")
        self.assertEqual(caught.exception.missing, frozenset({"missing"}))


class DrillExecutionTests(TestCase):
    def setUp(self):
        schedule.clear()
        self.events = []

    def catalog_func(self, context):
        self.events.append("catalog")
        return make_receipt("catalog-data", "cat-v1", {"rows": 2})

    def dictionary_func(self, context):
        self.events.append("dictionary")
        catalog = context.receipt("catalog")
        return make_receipt(
            "dict-data",
            "dict-v1",
            {"based_on": catalog.version},
        )

    def detail_func(self, name):
        def func(context):
            self.events.append(name)
            dictionary = context.dictionary
            return make_receipt(
                name,
                "{}-v1".format(name),
                {"dictionary_version": dictionary.version},
            )

        func.__name__ = name
        return func

    def test_catalog_then_shared_dictionary_then_details(self):
        drill = BackfillDrill(
            "orders",
            self.catalog_func,
            self.dictionary_func,
            [
                DetailSpec("detail-a", self.detail_func("detail-a")),
                DetailSpec("detail-b", self.detail_func("detail-b")),
            ],
        )

        report = drill.run("2026-11-13")

        self.assertEqual(
            self.events, ["catalog", "dictionary", "detail-a", "detail-b"]
        )
        self.assertTrue(report.ok)
        self.assertEqual(report.date, datetime.date(2026, 11, 13))
        self.assertEqual(
            report.get("catalog").receipt.version, "cat-v1"
        )
        self.assertEqual(
            report.get("dictionary").receipt.payload["based_on"], "cat-v1"
        )
        for detail_name in ("detail-a", "detail-b"):
            result = report.get(detail_name)
            self.assertEqual(result.status, STATUS_SUCCESS)
            self.assertEqual(
                result.receipt.payload["dictionary_version"], "dict-v1"
            )

    def test_catalog_receipt_acts_as_dictionary_without_dictionary_stage(self):
        drill = BackfillDrill(
            "orders",
            self.catalog_func,
            details=[DetailSpec("detail-a", self.detail_func("detail-a"))],
        )

        report = drill.run(datetime.date(2026, 11, 13))

        self.assertTrue(report.ok)
        self.assertEqual(
            report.get("detail-a").receipt.payload["dictionary_version"],
            "cat-v1",
        )

    def test_external_receipts_satisfy_detail_inputs(self):
        captured = {}

        def detail_func(context):
            captured["order"] = context.receipt("order-receipt")
            captured["payload"] = context.payload("order-receipt")
            return make_receipt("detail-a", "detail-v1")

        drill = BackfillDrill(
            "orders",
            self.catalog_func,
            details=[
                DetailSpec("detail-a", detail_func, inputs=["order-receipt"])
            ],
        )
        receipt = make_receipt(
            "order-receipt", "order-v9", {"records": 7}
        )

        report = drill.run("2026-11-13", [receipt])

        self.assertTrue(report.ok)
        self.assertEqual(captured["order"].version, "order-v9")
        self.assertEqual(captured["payload"], {"records": 7})

    def test_receipt_mappings_are_accepted_as_external_input(self):
        drill = BackfillDrill(
            "orders",
            self.catalog_func,
            details=[
                DetailSpec(
                    "detail-a",
                    lambda context: make_receipt("detail-a", "d-v1"),
                    inputs=["order-receipt"],
                )
            ],
        )

        report = drill.run(
            "2026-11-13",
            [{"name": "order-receipt", "version": "order-v9", "rows": 3}],
        )

        self.assertTrue(report.ok)
        self.assertEqual(
            report.get("detail-a").status, STATUS_SUCCESS
        )

    def test_missing_verifiable_version_is_listed_this_round(self):
        detail = mock.Mock()
        detail.__name__ = "detail"
        drill = BackfillDrill(
            "orders",
            self.catalog_func,
            details=[
                DetailSpec(
                    "detail-a", detail, inputs=["order-receipt", "other-receipt"]
                )
            ],
        )

        first_round = drill.run("2026-11-13")

        self.assertFalse(first_round.ok)
        self.assertEqual(first_round.missing, ("detail-a",))
        result = first_round.get("detail-a")
        self.assertEqual(result.status, STATUS_MISSING)
        self.assertEqual(
            first_round.missing_inputs["detail-a"],
            frozenset({"order-receipt", "other-receipt"}),
        )
        detail.assert_not_called()

        # A later round that supplies the verifiable receipts succeeds.
        second_round = drill.run(
            "2026-11-13",
            [
                make_receipt("order-receipt", "v1"),
                make_receipt("other-receipt", "v2"),
            ],
        )
        self.assertTrue(second_round.ok)
        detail.assert_called_once()

    def test_catalog_failure_blocks_every_detail(self):
        def broken_catalog(context):
            raise RuntimeError("catalog down")

        drill = BackfillDrill(
            "orders",
            broken_catalog,
            self.dictionary_func,
            [
                DetailSpec("detail-a", self.detail_func("detail-a")),
                DetailSpec("detail-b", self.detail_func("detail-b")),
            ],
        )

        report = drill.run("2026-11-13")

        self.assertEqual(report.failed, ("catalog",))
        self.assertIsInstance(report.get("catalog").error, RuntimeError)
        self.assertEqual(
            set(report.blocked),
            {"dictionary", "detail-a", "detail-b"},
        )
        for name in ("dictionary", "detail-a", "detail-b"):
            self.assertEqual(report.get(name).status, STATUS_BLOCKED)
            self.assertEqual(
                report.get(name).blocked_by, frozenset({"catalog"})
            )
        self.assertEqual(self.events, [])

    def test_dictionary_failure_blocks_details_but_catalog_succeeds(self):
        def broken_dictionary(context):
            raise RuntimeError("dictionary down")

        drill = BackfillDrill(
            "orders",
            self.catalog_func,
            broken_dictionary,
            [DetailSpec("detail-a", self.detail_func("detail-a"))],
        )

        report = drill.run("2026-11-13")

        self.assertTrue(report.get("catalog").ok)
        self.assertEqual(report.failed, ("dictionary",))
        self.assertEqual(report.blocked, ("detail-a",))
        self.assertEqual(
            report.get("detail-a").blocked_by, frozenset({"dictionary"})
        )

    def test_failed_detail_blocks_dependents_but_independent_detail_runs(self):
        def broken_detail(context):
            self.events.append("detail-a")
            raise RuntimeError("detail boom")

        drill = BackfillDrill(
            "orders",
            self.catalog_func,
            details=[
                DetailSpec("detail-a", broken_detail),
                DetailSpec("detail-b", self.detail_func("detail-b")),
                DetailSpec(
                    "detail-c",
                    self.detail_func("detail-c"),
                    blocked_by=["detail-a"],
                ),
            ],
        )

        report = drill.run("2026-11-13")

        self.assertEqual(report.failed, ("detail-a",))
        self.assertTrue(report.get("detail-b").ok)
        self.assertEqual(report.get("detail-c").status, STATUS_BLOCKED)
        self.assertEqual(
            report.get("detail-c").blocked_by, frozenset({"detail-a"})
        )
        self.assertEqual(self.events, ["catalog", "detail-a", "detail-b"])

    def test_plain_return_values_are_wrapped_in_receipts(self):
        drill = BackfillDrill(
            "orders",
            lambda context: 42,
            details=[
                DetailSpec("detail-a", lambda context: ["row-1", "row-2"])
            ],
        )

        report = drill.run("2026-11-13")

        catalog_receipt = report.get("catalog").receipt
        detail_receipt = report.get("detail-a").receipt
        self.assertEqual(catalog_receipt.payload, {"value": 42})
        self.assertEqual(detail_receipt.payload, {"value": ["row-1", "row-2"]})
        self.assertEqual(len(detail_receipt.version), 32)

    def test_mapping_return_value_is_coerced_into_receipt(self):
        drill = BackfillDrill(
            "orders",
            lambda context: {"version": "map-v1", "rows": 9},
        )

        report = drill.run("2026-11-13")

        receipt = report.get("catalog").receipt
        self.assertEqual(receipt.name, "catalog")
        self.assertEqual(receipt.version, "map-v1")
        self.assertEqual(receipt.payload, {"rows": 9})

    def test_run_drill_convenience_wrapper(self):
        report = run_drill(
            "2026-11-13",
            self.catalog_func,
            [DetailSpec("detail-a", self.detail_func("detail-a"))],
        )
        self.assertTrue(report.ok)

    def test_history_collects_every_run(self):
        drill = BackfillDrill("orders", self.catalog_func)
        drill.run("2026-11-13")
        drill.run("2026-11-12")
        self.assertEqual(
            [report.date for report in drill.history],
            [datetime.date(2026, 11, 13), datetime.date(2026, 11, 12)],
        )

    def test_invalid_date_and_receipt_inputs_are_rejected(self):
        drill = BackfillDrill("orders", self.catalog_func)
        with self.assertRaises(DrillError):
            drill.run("13-11-2026")
        with self.assertRaises(DrillError):
            drill.run(datetime.date(2026, 11, 13), receipts=["not-a-receipt"])
        with self.assertRaises(DrillError):
            drill.run(
                datetime.date(2026, 11, 13),
                receipts=[make_receipt("catalog", "v1")],
            )

    def test_detail_spec_validation(self):
        with self.assertRaises(DrillError):
            DetailSpec("catalog", lambda context: None)
        with self.assertRaises(DrillError):
            DetailSpec("dictionary", lambda context: None)
        with self.assertRaises(DrillError):
            DetailSpec("", lambda context: None)
        with self.assertRaises(DrillError):
            DetailSpec("x", "not-callable")

        drill = BackfillDrill("orders", self.catalog_func)
        spec = DetailSpec("detail-a", self.detail_func("detail-a"))
        drill.add_detail(spec)
        with self.assertRaises(DrillError):
            drill.add_detail(spec)
        with self.assertRaises(DrillError):
            drill.add_detail("detail-b", None)

        drill.add_detail(
            "detail-c",
            self.detail_func("detail-c"),
            inputs=["detail-a"],
            blocked_by=["detail-a"],
        )
        self.assertEqual(drill.details[1].name, "detail-c")

    def test_unknown_blocked_by_reference_is_rejected(self):
        drill = BackfillDrill(
            "orders",
            self.catalog_func,
            details=[
                DetailSpec(
                    "detail-a",
                    self.detail_func("detail-a"),
                    blocked_by=["ghost"],
                )
            ],
        )
        with self.assertRaises(DrillError):
            drill.run("2026-11-13")

    def test_forward_blocked_by_reference_is_rejected(self):
        drill = BackfillDrill(
            "orders",
            self.catalog_func,
            details=[
                DetailSpec(
                    "detail-a",
                    self.detail_func("detail-a"),
                    blocked_by=["detail-b"],
                ),
                DetailSpec("detail-b", self.detail_func("detail-b")),
            ],
        )
        with self.assertRaises(DrillError):
            drill.run("2026-11-13")


class DrillScheduleTests(TestCase):
    def setUp(self):
        schedule.clear()

    def test_manual_drill_does_not_change_routine_next_run(self):
        def catalog_func(context):
            return make_receipt("catalog-data", "cat-v1")

        drill = BackfillDrill("orders", catalog_func)

        with mock_datetime(2026, 11, 13, 9, 0):
            job = drill.schedule(at="10:30")
            self.assertIs(drill.job, job)
            self.assertEqual(
                job.next_run, datetime.datetime(2026, 11, 13, 10, 30)
            )

            # Manual drill for an older date must not touch the routine job.
            manual_report = drill.run("2026-11-10")
            self.assertTrue(manual_report.ok)
            self.assertEqual(
                job.next_run, datetime.datetime(2026, 11, 13, 10, 30)
            )

        # The routine trigger fires at its scheduled time and reschedules.
        with mock_datetime(2026, 11, 13, 10, 30):
            schedule.run_pending()
            self.assertEqual(
                job.next_run, datetime.datetime(2026, 11, 14, 10, 30)
            )

        # Another manual run the next day neither advances nor delays it.
        with mock_datetime(2026, 11, 14, 9, 0):
            drill.run("2026-11-11")
            self.assertEqual(
                job.next_run, datetime.datetime(2026, 11, 14, 10, 30)
            )

        with mock_datetime(2026, 11, 14, 10, 30):
            schedule.run_pending()
            self.assertEqual(
                job.next_run, datetime.datetime(2026, 11, 15, 10, 30)
            )

    def test_scheduled_drill_uses_receipts_provider(self):
        provider = mock.Mock(
            return_value=[make_receipt("order-receipt", "v1")]
        )
        detail = mock.Mock(return_value=make_receipt("detail-a", "d-v1"))
        drill = BackfillDrill(
            "orders",
            lambda context: make_receipt("catalog-data", "cat-v1"),
            details=[DetailSpec("detail-a", detail, inputs=["order-receipt"])],
        )

        with mock_datetime(2026, 11, 13, 9, 0):
            drill.schedule(at="10:30", receipts_provider=provider)

        with mock_datetime(2026, 11, 13, 10, 30):
            schedule.run_pending()

        provider.assert_called_once()
        detail.assert_called_once()
        self.assertTrue(drill.history[-1].ok)

    def test_scheduled_drill_without_time_runs_daily(self):
        drill = BackfillDrill(
            "orders", lambda context: make_receipt("catalog-data", "cat-v1")
        )
        with mock_datetime(2026, 11, 13, 9, 0):
            job = drill.schedule()
            self.assertEqual(job.next_run, datetime.datetime(2026, 11, 14, 9, 0))

    def test_drill_can_only_be_scheduled_once_and_unscheduled(self):
        drill = BackfillDrill(
            "orders", lambda context: make_receipt("catalog-data", "cat-v1")
        )
        with mock_datetime(2026, 11, 13, 9, 0):
            job = drill.schedule(at="10:30")
            with self.assertRaises(DrillError):
                drill.schedule()
            self.assertEqual(len(schedule.get_jobs()), 1)
            drill.unschedule()
            self.assertEqual(len(schedule.get_jobs()), 0)
            self.assertIsNone(drill.job)
        # Unschedule is idempotent.
        drill.unschedule()
        self.assertIsNone(drill.job)

    def test_failing_receipts_provider_does_not_break_scheduler(self):
        def broken_provider():
            raise RuntimeError("provider down")

        drill = BackfillDrill(
            "orders",
            lambda context: make_receipt("catalog-data", "cat-v1"),
        )
        with mock_datetime(2026, 11, 13, 9, 0):
            job = drill.schedule(at="10:30", receipts_provider=broken_provider)

        with mock_datetime(2026, 11, 13, 10, 30):
            schedule.run_pending()

        # The job remains scheduled and gets its normal next run.
        self.assertEqual(job.next_run, datetime.datetime(2026, 11, 14, 10, 30))
        self.assertEqual(drill.history, [])
