Reference
=========

.. module:: schedule

This part of the documentation covers all the interfaces of schedule.

Main Interface
--------------

.. autodata:: default_scheduler
.. autodata:: jobs

.. autofunction:: every
.. autofunction:: run_pending
.. autofunction:: run_all
.. autofunction:: get_jobs
.. autofunction:: clear
.. autofunction:: cancel_job
.. autofunction:: next_run
.. autofunction:: idle_seconds


Classes
-------

.. autoclass:: schedule.Scheduler
   :members:
   :undoc-members:

.. autoclass:: schedule.Job
   :members:
   :undoc-members:

Business-day reminders
----------------------

.. automodule:: schedule.reminders

.. autoclass:: schedule.reminders.BusinessCalendar
   :members:

.. autoclass:: schedule.reminders.BusinessException
   :members:

.. autoclass:: schedule.reminders.Receipt
   :members:

.. autoclass:: schedule.reminders.ReminderEngine
   :members:

.. autoclass:: schedule.reminders.Reminder
   :members:

.. autoclass:: schedule.reminders.ReminderItem
   :members:

.. autoclass:: schedule.reminders.PollResult
   :members:

.. autofunction:: schedule.reminders.install


Exceptions
----------

.. autoexception:: schedule.CancelJob