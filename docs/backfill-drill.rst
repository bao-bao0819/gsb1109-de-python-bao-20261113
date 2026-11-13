Backfill drills
===============

A backfill drill re-collects data for one specific date. A drill always
runs its stages in a fixed order:

1. the **catalog** (目录) is collected first;
2. a single **dictionary** (字典) is derived from the catalog and shared by
   every detail;
3. each **detail** (明细) runs against the catalog, the shared dictionary
   and the receipts supplied up front.

The :mod:`schedule.drill` module keeps a drill completely separate from the
routine scheduler: triggering a drill manually never changes a job's next
run.

A first drill
~~~~~~~~~~~~~

.. code-block:: python

    import datetime
    from schedule.drill import BackfillDrill, DetailSpec, Receipt

    def collect_catalog(context):
        # context.date is the date being re-collected.
        return Receipt(
            name="catalog",
            version="catalog-v1",
            produced_at=datetime.datetime.now(),
            payload={"rows": 42},
        )

    def build_dictionary(context):
        catalog = context.receipt("catalog")
        return Receipt(
            name="dictionary",
            version="dict-v1",
            produced_at=datetime.datetime.now(),
            payload={"based_on": catalog.version},
        )

    def collect_orders(context):
        # Every detail sees the same shared dictionary receipt.
        dictionary = context.dictionary
        return Receipt(
            name="orders",
            version="orders-v1",
            produced_at=datetime.datetime.now(),
            payload={"dictionary_version": dictionary.version},
        )

    drill = BackfillDrill(
        "daily-backfill",
        collect_catalog,
        build_dictionary,
        [DetailSpec("orders", collect_orders)],
    )

    report = drill.run("2026-11-13")
    print(report.ok)  # True

Stage functions may also return a plain value or a mapping; the drill wraps
the result in a :class:`~schedule.drill.Receipt` automatically.

Reusing existing successful receipts
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

A detail can declare the verifiable receipts it needs before it is allowed
to run. Pass the existing successful receipts to :meth:`BackfillDrill.run`.
When a verifiable version is not available, the detail is not executed. It
is reported as ``missing`` for this round instead, together with the names
of the missing receipts:

.. code-block:: python

    drill = BackfillDrill(
        "daily-backfill",
        collect_catalog,
        details=[
            DetailSpec(
                "orders",
                collect_orders,
                inputs=["settlement-receipt"],
            )
        ],
    )

    report = drill.run(
        datetime.date(2026, 11, 13),
        receipts=[
            Receipt(
                name="settlement-receipt",
                version="settlement-v7",
                produced_at=datetime.datetime.now(),
            )
        ],
    )

    # Without the receipt:
    # report.missing == ("orders",)
    # report.missing_inputs == {"orders": frozenset({"settlement-receipt"})}

Failures, blocked and independent details
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Stage exceptions never escape :meth:`BackfillDrill.run`; they are captured
in the returned :class:`~schedule.drill.DrillReport`. A failed catalog
blocks the dictionary and every detail, a failed dictionary blocks every
detail, and a failed detail blocks the details that explicitly depend on it
through ``blocked_by``. Independent details still run and produce results:

.. code-block:: python

    drill = BackfillDrill(
        "daily-backfill",
        collect_catalog,
        details=[
            DetailSpec("orders", collect_orders),
            DetailSpec(
                "invoices",
                collect_invoices,
                blocked_by=["orders"],
            ),
        ],
    )

    report = drill.run("2026-11-13")
    report.failed   # ("orders",)
    report.blocked  # ("invoices",)
    report.get("invoices").blocked_by  # frozenset({"orders"})

Attaching a drill to the routine scheduler
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

:meth:`BackfillDrill.schedule` registers an ordinary daily job. The drill
only observes job triggers; it never modifies ``next_run``. Calling
:meth:`BackfillDrill.run` manually therefore neither advances nor delays
the next routine trigger:

.. code-block:: python

    drill.schedule(at="10:30", receipts_provider=load_available_receipts)

    # Manual re-collection for an older date leaves the routine job alone.
    drill.run("2026-11-10")

    # Next day, run_pending() still triggers at 10:30 as usual.
    schedule.run_pending()

Every run, manual or scheduled, appends its report to
:attr:`BackfillDrill.history`.
