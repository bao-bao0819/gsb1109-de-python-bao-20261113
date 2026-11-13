Watermark driven reconciliation
===============================

.. module:: schedule.reconciliation

When several upstream sources must be reconciled per statistical window,
the scheduler cannot run on wall-clock time alone. Each source is polled
independently and answers with a **watermark**: a business-time point up to
which it guarantees that every record has been delivered. Even an empty
batch carries a watermark.

The :class:`ReconciliationScheduler` turns these independent watermarks
into reconciliation dispatch decisions.

Rules it implements
-------------------

* A window is reconciled only once **every required source** has sealed it,
  i.e. published a watermark at or beyond the window's end. Slow sources
  hold back the whole window; when they catch up, every missed window is
  reconciled in order.
* Sources publish at different cadences and may correct old windows. A
  correction is a receipt whose ``supersedes`` names the receipt it
  replaces. Following these lineage links guarantees that a correction
  creates a new report version **only for windows actually built on the
  replaced receipt**. Other windows keep their confirmed version untouched.
* A late receipt carrying an older revision is marked stale on arrival. It
  stays in the append-only audit log but is excluded from sealing and
  selection, so it can never roll back a confirmed newer receipt, nor pull
  the coverage horizon backwards.
* Every :class:`ReconciliationReport` pins the exact :class:`Receipt` used
  from each source. :meth:`ReconciliationReport.describe` produces a
  JSON-friendly daily record that answers precisely which reply of each
  source backs each report version.

Example
-------

.. code-block:: python

    import datetime as dt
    from schedule import ReconciliationScheduler, make_receipt

    UTC = dt.timezone.utc
    HOUR = dt.timedelta(hours)

    def hour(h):
        return dt.datetime(2026, 11, 13, h, tzinfo=UTC)

    scheduler = ReconciliationScheduler(HOUR, start=hour(10))
    scheduler.register_source("payments")
    scheduler.register_source("ledger")

    # Nothing fires until both sources sealed the same window.
    scheduler.observe_receipt(
        make_receipt("payments", hour(11), hour(10, 5), receipt_id="p-w1")
    )
    assert scheduler.ready_windows() == []

    # An empty-batch watermark seals a window just like any other reply.
    scheduler.observe_receipt(
        make_receipt(
            "ledger", hour(11), hour(10, 6), receipt_id="l-w1", empty=True
        )
    )
    report = scheduler.latest_report(hour(10))
    assert report.version == 1

    # A later correction of the old window only re-versions that window.
    scheduler.observe_receipt(
        make_receipt(
            "payments",
            hour(11),
            hour(13),
            revision=2,
            receipt_id="p-fix",
            supersedes="p-w1",
        )
    )
    assert scheduler.latest_report(hour(10)).version == 2
    assert report.status == "superseded"

Independent polling
-------------------

Each source keeps its own fetch cadence through
:meth:`ReconciliationScheduler.poll_source`, which takes a callable
returning a receipt (or ``None`` when a poll brings no reply):

.. code-block:: python

    scheduler.poll_source("payments", payments_client.fetch_receipt)
    scheduler.poll_source("ledger", ledger_client.fetch_receipt)

Reconciliation jobs themselves are registered as listeners and are invoked
exactly once per freshly created report version:

.. code-block:: python

    @scheduler.add_listener
    def run_reconciliation(report):
        reconcile_window(report.window_start, report.window_end, report.basis)

Reference
---------

.. autoclass:: Watermark
   :members:

.. autoclass:: Receipt
   :members:

.. autofunction:: make_receipt

.. autoclass:: ReconciliationReport
   :members:

.. autoclass:: ReconciliationScheduler
   :members:

.. autoexception:: ReconciliationError

.. autodata:: STATUS_CURRENT

.. autodata:: STATUS_SUPERSEDED
