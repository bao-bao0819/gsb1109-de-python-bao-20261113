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
.. autofunction:: get_jobs_at
.. autofunction:: diff_jobs
.. autofunction:: correct_job
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

.. autoclass:: schedule.JobEvent
   :members:
   :undoc-members:

.. autoclass:: schedule.JobsDiff
   :members:
   :undoc-members:


Exceptions
----------

.. autoexception:: schedule.CancelJob
