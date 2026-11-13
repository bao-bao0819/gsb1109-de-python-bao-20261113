"""
Python job scheduling for humans.

github.com/dbader/schedule

An in-process scheduler for periodic jobs that uses the builder pattern
for configuration. Schedule lets you run Python functions (or any other
callable) periodically at pre-determined intervals using a simple,
human-friendly syntax.

Inspired by Addam Wiggins' article "Rethinking Cron" [1] and the
"clockwork" Ruby module [2][3].

Features:
    - A simple to use API for scheduling jobs.
    - Very lightweight and no external dependencies.
    - Excellent test coverage.
    - Tested on Python 3.7, 3.8, 3.9, 3.10, 3.11 and 3.12

Usage:
    >>> import schedule
    >>> import time

    >>> def job(message='stuff'):
    >>>     print("I'm working on:", message)

    >>> schedule.every(10).minutes.do(job)
    >>> schedule.every(5).to(10).days.do(job)
    >>> schedule.every().hour.do(job, message='things')
    >>> schedule.every().day.at("10:30").do(job)

    >>> while True:
    >>>     schedule.run_pending()
    >>>     time.sleep(1)

[1] https://adam.herokuapp.com/past/2010/4/13/rethinking_cron/
[2] https://github.com/Rykian/clockwork
[3] https://adam.herokuapp.com/past/2010/6/30/replace_cron_with_clockwork/
"""

from collections.abc import Hashable
import datetime
import functools
import json
import logging
import os
import random
import re
import time
from typing import Any, Dict, Set, List, Optional, Callable, Union

logger = logging.getLogger("schedule")


class ScheduleError(Exception):
    """Base schedule exception"""

    pass


class ScheduleValueError(ScheduleError):
    """Base schedule value error"""

    pass


class IntervalError(ScheduleValueError):
    """An improper interval was used"""

    pass


class CancelJob:
    """
    Can be returned from a job to unschedule itself.
    """

    pass


class Slot:
    """
    A logical run (a shift) of a :class:`Job`.

    Slots are only recorded for jobs that have catch-up enabled with
    :meth:`Job.catch_up`.  A slot is identified by the instant it was
    supposed to run, not by the instant the work actually started.  This
    keeps the cadence stable when a process was stopped or a run was slow.

    The public ``state`` values are:

    ``scheduled``
        The slot is known but has not started.
    ``started``
        The job function was entered but no result was recorded yet.
    ``interrupted``
        A persisted ``started`` slot was found after a restart.
    ``completed``
        The job function returned and the result was recorded.
    ``failed``
        The job function raised a normal exception.
    ``skipped``
        The slot will not be run.  ``reason`` explains why.
    """

    def __init__(self, scheduled_at: datetime.datetime, state: str = "scheduled"):
        self.scheduled_at = scheduled_at
        self.state = state
        self.started_at: Optional[datetime.datetime] = None
        self.finished_at: Optional[datetime.datetime] = None
        self.skipped_at: Optional[datetime.datetime] = None
        self.attempts: int = 0
        self.caught_up: bool = False
        self.recovered: bool = False
        self.reason: Optional[str] = None
        self.error: Optional[str] = None

    @property
    def disposition(self) -> str:
        """A human readable outcome for this logical slot."""
        if self.state == "scheduled":
            return "pending"
        if self.state == "started":
            return "running"
        if self.state == "interrupted":
            return "interrupted"
        if self.state == "failed":
            return "failed"
        if self.state == "skipped":
            return "skipped"
        if self.state == "completed":
            if self.recovered:
                return "recovered"
            if self.caught_up:
                return "caught_up"
            return "delivered"
        return self.state

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scheduled_at": self.scheduled_at.isoformat(),
            "state": self.state,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "skipped_at": self.skipped_at.isoformat() if self.skipped_at else None,
            "attempts": self.attempts,
            "caught_up": self.caught_up,
            "recovered": self.recovered,
            "reason": self.reason,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Slot":
        slot = cls(datetime.datetime.fromisoformat(data["scheduled_at"]))
        slot.state = data.get("state", "scheduled")
        for name in ("started_at", "finished_at", "skipped_at"):
            value = data.get(name)
            if value:
                setattr(slot, name, datetime.datetime.fromisoformat(value))
        slot.attempts = int(data.get("attempts", 0))
        slot.caught_up = bool(data.get("caught_up", False))
        slot.recovered = bool(data.get("recovered", False))
        slot.reason = data.get("reason")
        slot.error = data.get("error")
        return slot

    def __repr__(self) -> str:
        return "Slot(scheduled_at={}, disposition={}{})".format(
            self.scheduled_at.isoformat(sep=" "),
            self.disposition,
            ", reason={}".format(self.reason) if self.reason else "",
        )


def now_without_microseconds() -> datetime.datetime:
    return datetime.datetime.now().replace(microsecond=0)


class Scheduler:
    """
    Objects instantiated by the :class:`Scheduler <Scheduler>` are
    factories to create jobs, keep record of scheduled jobs and
    handle their execution.
    """

    def __init__(self) -> None:
        self.jobs: List[Job] = []

    def run_pending(self) -> None:
        """
        Run all jobs that are scheduled to run.

        Please note that it is *intended behavior that run_pending()
        does not run missed jobs*. For example, if you've registered a job
        that should run every minute and you only call run_pending()
        in one hour increments then your job won't be run 60 times in
        between but only once.
        """
        runnable_jobs = (job for job in self.jobs if job.should_run)
        for job in sorted(runnable_jobs):
            self._run_job(job)
            while (
                job in self.jobs
                and job.catch_up_max is not None
                and job.pending_catch_up
            ):
                self._run_job(job)

    def run_all(self, delay_seconds: int = 0) -> None:
        """
        Run all jobs regardless if they are scheduled to run or not.

        A delay of `delay` seconds is added between each job. This helps
        distribute system load generated by the jobs more evenly
        over time.

        :param delay_seconds: A delay added between every executed job
        """
        logger.debug(
            "Running *all* %i jobs with %is delay in between",
            len(self.jobs),
            delay_seconds,
        )
        for job in self.jobs[:]:
            self._run_job(job)
            time.sleep(delay_seconds)
            while (
                job in self.jobs
                and job.catch_up_max is not None
                and job.pending_catch_up
            ):
                self._run_job(job)
                time.sleep(delay_seconds)

    def get_jobs(self, tag: Optional[Hashable] = None) -> List["Job"]:
        """
        Gets scheduled jobs marked with the given tag, or all jobs
        if tag is omitted.

        :param tag: An identifier used to identify a subset of
                    jobs to retrieve
        """
        if tag is None:
            return self.jobs[:]
        else:
            return [job for job in self.jobs if tag in job.tags]

    def clear(self, tag: Optional[Hashable] = None) -> None:
        """
        Deletes scheduled jobs marked with the given tag, or all jobs
        if tag is omitted.

        :param tag: An identifier used to identify a subset of
                    jobs to delete
        """
        if tag is None:
            logger.debug("Deleting *all* jobs")
            del self.jobs[:]
        else:
            logger.debug('Deleting all jobs tagged "%s"', tag)
            self.jobs[:] = (job for job in self.jobs if tag not in job.tags)

    def cancel_job(self, job: "Job") -> None:
        """
        Delete a scheduled job.

        :param job: The job to be unscheduled
        """
        try:
            logger.debug('Cancelling job "%s"', str(job))
            self.jobs.remove(job)
        except ValueError:
            logger.debug('Cancelling not-scheduled job "%s"', str(job))

    def every(self, interval: int = 1) -> "Job":
        """
        Schedule a new periodic job.

        :param interval: A quantity of a certain time unit
        :return: An unconfigured :class:`Job <Job>`
        """
        job = Job(interval, self)
        return job

    def _run_job(self, job: "Job") -> None:
        ret = job.run()
        if isinstance(ret, CancelJob) or ret is CancelJob:
            self.cancel_job(job)

    def get_next_run(
        self, tag: Optional[Hashable] = None
    ) -> Optional[datetime.datetime]:
        """
        Datetime when the next job should run.

        :param tag: Filter the next run for the given tag parameter

        :return: A :class:`~datetime.datetime` object
                 or None if no jobs scheduled
        """
        if not self.jobs:
            return None
        jobs_filtered = self.get_jobs(tag)
        if not jobs_filtered:
            return None
        return min(jobs_filtered).next_run

    next_run = property(get_next_run)

    def reconcile(self, tag: Optional[Hashable] = None) -> None:
        """Refresh catch-up accounting for jobs without running them."""
        for job in self.get_jobs(tag):
            if job.catch_up_max is not None:
                job._reconcile_catch_up()

    @property
    def idle_seconds(self) -> Optional[float]:
        """
        :return: Number of seconds until
                 :meth:`next_run <Scheduler.next_run>`
                 or None if no jobs are scheduled
        """
        if not self.next_run:
            return None
        return (self.next_run - datetime.datetime.now()).total_seconds()


class Job:
    """
    A periodic job as used by :class:`Scheduler`.

    :param interval: A quantity of a certain time unit
    :param scheduler: The :class:`Scheduler <Scheduler>` instance that
                      this job will register itself with once it has
                      been fully configured in :meth:`Job.do()`.

    Every job runs at a given fixed time interval that is defined by:

    * a :meth:`time unit <Job.second>`
    * a quantity of `time units` defined by `interval`

    A job is usually created and returned by :meth:`Scheduler.every`
    method, which also defines its `interval`.
    """

    def __init__(self, interval: int, scheduler: Optional[Scheduler] = None):
        self.interval: int = interval  # pause interval * unit between runs
        self.latest: Optional[int] = None  # upper limit to the interval
        self.job_func: Optional[functools.partial] = None  # the job job_func to run

        # time units, e.g. 'minutes', 'hours', ...
        self.unit: Optional[str] = None

        # optional time at which this job runs
        self.at_time: Optional[datetime.time] = None

        # optional time zone of the self.at_time field. Only relevant when at_time is not None
        self.at_time_zone = None

        # datetime of the last run
        self.last_run: Optional[datetime.datetime] = None

        # datetime of the next run
        self.next_run: Optional[datetime.datetime] = None

        # Weekday to run the job at. Only relevant when unit is 'weeks'.
        # For example, when asking 'every week on tuesday' the start_day is 'tuesday'.
        self.start_day: Optional[str] = None

        # optional time of final run
        self.cancel_after: Optional[datetime.datetime] = None

        self.tags: Set[Hashable] = set()  # unique set of tags for the job
        self.scheduler: Optional[Scheduler] = scheduler  # scheduler to register with

        # Catch-up configuration. ``catch_up_max`` is None for jobs that keep
        # the historical "only run what is due now" behavior.
        self.catch_up_max: Optional[int] = None
        self.catch_up_state_path: Optional[str] = None
        self.catch_up_retry_interrupted: bool = False
        self.catch_up_ledger_limit: int = 1000

        # Logical run ledger, ordered by the instant each shift was due.
        self._slots: List[Slot] = []
        self._catch_up_queue: List[Slot] = []
        self._catch_up_last_seen: Optional[datetime.datetime] = None
        self._catch_up_anchor: Optional[datetime.datetime] = None
        self._catch_up_active: Optional[Slot] = None

        # A scheduler is expected to poll at least once a minute.  A longer
        # silence means the process was stopped or slept through a grid point.
        self.catch_up_poll_tolerance: datetime.timedelta = datetime.timedelta(
            seconds=60
        )

    def __lt__(self, other) -> bool:
        """
        PeriodicJobs are sortable based on the scheduled time they
        run next.
        """
        return self._effective_next_run < other._effective_next_run

    @property
    def _effective_next_run(self) -> Optional[datetime.datetime]:
        if self._catch_up_queue:
            return self._catch_up_queue[0].scheduled_at
        return self.next_run

    def __str__(self) -> str:
        if hasattr(self.job_func, "__name__"):
            job_func_name = self.job_func.__name__  # type: ignore
        else:
            job_func_name = repr(self.job_func)

        return ("Job(interval={}, unit={}, do={}, args={}, kwargs={})").format(
            self.interval,
            self.unit,
            job_func_name,
            "()" if self.job_func is None else self.job_func.args,
            "{}" if self.job_func is None else self.job_func.keywords,
        )

    def __repr__(self):
        def format_time(t):
            return t.strftime("%Y-%m-%d %H:%M:%S") if t else "[never]"

        def is_repr(j):
            return not isinstance(j, Job)

        timestats = "(last run: %s, next run: %s)" % (
            format_time(self.last_run),
            format_time(self.next_run),
        )

        if hasattr(self.job_func, "__name__"):
            job_func_name = self.job_func.__name__
        else:
            job_func_name = repr(self.job_func)

        if self.job_func is not None:
            args = [repr(x) if is_repr(x) else str(x) for x in self.job_func.args]
            kwargs = ["%s=%s" % (k, repr(v)) for k, v in self.job_func.keywords.items()]
            call_repr = job_func_name + "(" + ", ".join(args + kwargs) + ")"
        else:
            call_repr = "[None]"

        if self.at_time is not None:
            return "Every %s %s at %s do %s %s" % (
                self.interval,
                self.unit[:-1] if self.interval == 1 else self.unit,
                self.at_time,
                call_repr,
                timestats,
            )
        else:
            fmt = (
                "Every %(interval)s "
                + ("to %(latest)s " if self.latest is not None else "")
                + "%(unit)s do %(call_repr)s %(timestats)s"
            )

            return fmt % dict(
                interval=self.interval,
                latest=self.latest,
                unit=(self.unit[:-1] if self.interval == 1 else self.unit),
                call_repr=call_repr,
                timestats=timestats,
            )

    @property
    def second(self):
        if self.interval != 1:
            raise IntervalError("Use seconds instead of second")
        return self.seconds

    @property
    def seconds(self):
        self.unit = "seconds"
        return self

    @property
    def minute(self):
        if self.interval != 1:
            raise IntervalError("Use minutes instead of minute")
        return self.minutes

    @property
    def minutes(self):
        self.unit = "minutes"
        return self

    @property
    def hour(self):
        if self.interval != 1:
            raise IntervalError("Use hours instead of hour")
        return self.hours

    @property
    def hours(self):
        self.unit = "hours"
        return self

    @property
    def day(self):
        if self.interval != 1:
            raise IntervalError("Use days instead of day")
        return self.days

    @property
    def days(self):
        self.unit = "days"
        return self

    @property
    def week(self):
        if self.interval != 1:
            raise IntervalError("Use weeks instead of week")
        return self.weeks

    @property
    def weeks(self):
        self.unit = "weeks"
        return self

    @property
    def monday(self):
        if self.interval != 1:
            raise IntervalError(
                "Scheduling .monday() jobs is only allowed for weekly jobs. "
                "Using .monday() on a job scheduled to run every 2 or more weeks "
                "is not supported."
            )
        self.start_day = "monday"
        return self.weeks

    @property
    def tuesday(self):
        if self.interval != 1:
            raise IntervalError(
                "Scheduling .tuesday() jobs is only allowed for weekly jobs. "
                "Using .tuesday() on a job scheduled to run every 2 or more weeks "
                "is not supported."
            )
        self.start_day = "tuesday"
        return self.weeks

    @property
    def wednesday(self):
        if self.interval != 1:
            raise IntervalError(
                "Scheduling .wednesday() jobs is only allowed for weekly jobs. "
                "Using .wednesday() on a job scheduled to run every 2 or more weeks "
                "is not supported."
            )
        self.start_day = "wednesday"
        return self.weeks

    @property
    def thursday(self):
        if self.interval != 1:
            raise IntervalError(
                "Scheduling .thursday() jobs is only allowed for weekly jobs. "
                "Using .thursday() on a job scheduled to run every 2 or more weeks "
                "is not supported."
            )
        self.start_day = "thursday"
        return self.weeks

    @property
    def friday(self):
        if self.interval != 1:
            raise IntervalError(
                "Scheduling .friday() jobs is only allowed for weekly jobs. "
                "Using .friday() on a job scheduled to run every 2 or more weeks "
                "is not supported."
            )
        self.start_day = "friday"
        return self.weeks

    @property
    def saturday(self):
        if self.interval != 1:
            raise IntervalError(
                "Scheduling .saturday() jobs is only allowed for weekly jobs. "
                "Using .saturday() on a job scheduled to run every 2 or more weeks "
                "is not supported."
            )
        self.start_day = "saturday"
        return self.weeks

    @property
    def sunday(self):
        if self.interval != 1:
            raise IntervalError(
                "Scheduling .sunday() jobs is only allowed for weekly jobs. "
                "Using .sunday() on a job scheduled to run every 2 or more weeks "
                "is not supported."
            )
        self.start_day = "sunday"
        return self.weeks

    def tag(self, *tags: Hashable):
        """
        Tags the job with one or more unique identifiers.

        Tags must be hashable. Duplicate tags are discarded.

        :param tags: A unique list of ``Hashable`` tags.
        :return: The invoked job instance
        """
        if not all(isinstance(tag, Hashable) for tag in tags):
            raise TypeError("Tags must be hashable")
        self.tags.update(tags)
        return self

    def at(self, time_str: str, tz: Optional[str] = None):
        """
        Specify a particular time that the job should be run at.

        :param time_str: A string in one of the following formats:

            - For daily jobs -> `HH:MM:SS` or `HH:MM`
            - For hourly jobs -> `MM:SS` or `:MM`
            - For minute jobs -> `:SS`

            The format must make sense given how often the job is
            repeating; for example, a job that repeats every minute
            should not be given a string in the form `HH:MM:SS`. The
            difference between `:MM` and `:SS` is inferred from the
            selected time-unit (e.g. `every().hour.at(':30')` vs.
            `every().minute.at(':30')`).

        :param tz: The timezone that this timestamp refers to. Can be
            a string that can be parsed by pytz.timezone(), or a pytz.BaseTzInfo object

        :return: The invoked job instance
        """
        if self.unit not in ("days", "hours", "minutes") and not self.start_day:
            raise ScheduleValueError(
                "Invalid unit (valid units are `days`, `hours`, and `minutes`)"
            )

        if tz is not None:
            import pytz

            if isinstance(tz, str):
                self.at_time_zone = pytz.timezone(tz)  # type: ignore
            elif isinstance(tz, pytz.BaseTzInfo):
                self.at_time_zone = tz
            else:
                raise ScheduleValueError(
                    "Timezone must be string or pytz.timezone object"
                )

        if not isinstance(time_str, str):
            raise TypeError("at() should be passed a string")
        if self.unit == "days" or self.start_day:
            if not re.match(r"^[0-2]\d:[0-5]\d(:[0-5]\d)?$", time_str):
                raise ScheduleValueError(
                    "Invalid time format for a daily job (valid format is HH:MM(:SS)?)"
                )
        if self.unit == "hours":
            if not re.match(r"^([0-5]\d)?:[0-5]\d$", time_str):
                raise ScheduleValueError(
                    "Invalid time format for an hourly job (valid format is (MM)?:SS)"
                )

        if self.unit == "minutes":
            if not re.match(r"^:[0-5]\d$", time_str):
                raise ScheduleValueError(
                    "Invalid time format for a minutely job (valid format is :SS)"
                )
        time_values = time_str.split(":")
        hour: Union[str, int]
        minute: Union[str, int]
        second: Union[str, int]
        if len(time_values) == 3:
            hour, minute, second = time_values
        elif len(time_values) == 2 and self.unit == "minutes":
            hour = 0
            minute = 0
            _, second = time_values
        elif len(time_values) == 2 and self.unit == "hours" and len(time_values[0]):
            hour = 0
            minute, second = time_values
        else:
            hour, minute = time_values
            second = 0
        if self.unit == "days" or self.start_day:
            hour = int(hour)
            if not (0 <= hour <= 23):
                raise ScheduleValueError(
                    "Invalid number of hours ({} is not between 0 and 23)"
                )
        elif self.unit == "hours":
            hour = 0
        elif self.unit == "minutes":
            hour = 0
            minute = 0
        hour = int(hour)
        minute = int(minute)
        second = int(second)
        self.at_time = datetime.time(hour, minute, second)
        return self

    def to(self, latest: int):
        """
        Schedule the job to run at an irregular (randomized) interval.

        The job's interval will randomly vary from the value given
        to  `every` to `latest`. The range defined is inclusive on
        both ends. For example, `every(A).to(B).seconds` executes
        the job function every N seconds such that A <= N <= B.

        :param latest: Maximum interval between randomized job runs
        :return: The invoked job instance
        """
        self.latest = latest
        return self

    def until(
        self,
        until_time: Union[datetime.datetime, datetime.timedelta, datetime.time, str],
    ):
        """
        Schedule job to run until the specified moment.

        The job is canceled whenever the next run is calculated and it turns out the
        next run is after the until_time. The job is also canceled right before it runs,
        if the current time is after until_time. This latter case can happen when the
        the job was scheduled to run before until_time, but runs after until_time.

        If until_time is a moment in the past, ScheduleValueError is thrown.

        :param until_time: A moment in the future representing the latest time a job can
           be run. If only a time is supplied, the date is set to today.
           The following formats are accepted:

           - datetime.datetime
           - datetime.timedelta
           - datetime.time
           - String in one of the following formats: "%Y-%m-%d %H:%M:%S",
             "%Y-%m-%d %H:%M", "%Y-%m-%d", "%H:%M:%S", "%H:%M"
             as defined by strptime() behaviour. If an invalid string format is passed,
             ScheduleValueError is thrown.

        :return: The invoked job instance
        """

        if isinstance(until_time, datetime.datetime):
            self.cancel_after = until_time
        elif isinstance(until_time, datetime.timedelta):
            self.cancel_after = datetime.datetime.now() + until_time
        elif isinstance(until_time, datetime.time):
            self.cancel_after = datetime.datetime.combine(
                datetime.datetime.now(), until_time
            )
        elif isinstance(until_time, str):
            cancel_after = self._decode_datetimestr(
                until_time,
                [
                    "%Y-%m-%d %H:%M:%S",
                    "%Y-%m-%d %H:%M",
                    "%Y-%m-%d",
                    "%H:%M:%S",
                    "%H:%M",
                ],
            )
            if cancel_after is None:
                raise ScheduleValueError("Invalid string format for until()")
            if "-" not in until_time:
                # the until_time is a time-only format. Set the date to today
                now = datetime.datetime.now()
                cancel_after = cancel_after.replace(
                    year=now.year, month=now.month, day=now.day
                )
            self.cancel_after = cancel_after
        else:
            raise TypeError(
                "until() takes a string, datetime.datetime, datetime.timedelta, "
                "datetime.time parameter"
            )
        if self.cancel_after < datetime.datetime.now():
            raise ScheduleValueError(
                "Cannot schedule a job to run until a time in the past"
            )
        return self

    def do(self, job_func: Callable, *args, **kwargs):
        """
        Specifies the job_func that should be called every time the
        job runs.

        Any additional arguments are passed on to job_func when
        the job runs.

        :param job_func: The function to be scheduled
        :return: The invoked job instance
        """
        self.job_func = functools.partial(job_func, *args, **kwargs)
        functools.update_wrapper(self.job_func, job_func)
        self._schedule_next_run()
        if self.scheduler is None:
            raise ScheduleError(
                "Unable to a add job to schedule. "
                "Job is not associated with an scheduler"
            )
        self.scheduler.jobs.append(self)
        if self.catch_up_max is not None:
            self._activate_catch_up()
        return self

    def catch_up(
        self,
        max_slots: int = 0,
        state_path: Optional[str] = None,
        retry_interrupted: bool = False,
    ):
        """
        Enable bounded catch-up for this job and optionally persist its ledger.

        By default a restarted process only runs whatever is due at that
        instant and silently forgets the shifts it slept through.  With
        catch-up enabled the job keeps the logical schedule: the cadence is
        still derived from the original ``at`` grid and not from the time the
        process happened to come back.

        :param max_slots: How many of the most recent missed shifts may be run
            after a restart.  ``0`` replays nothing and only records the skips.
            Older missed shifts are recorded as skipped.
        :param state_path: Optional JSON file used to persist the ledger.  This
            is what lets a restarted process distinguish a shift that had
            already started from one that never started.
        :param retry_interrupted: When true, a shift that was ``started`` before
            the restart is retried.  By default it is marked ``interrupted`` and
            left alone so a half-finished result is never silently duplicated.
        :return: The invoked job instance
        """
        if not isinstance(max_slots, int) or isinstance(max_slots, bool):
            raise TypeError("max_slots must be an integer")
        if max_slots < 0:
            raise ScheduleValueError("max_slots must not be negative")
        if state_path is not None and not isinstance(state_path, str):
            raise TypeError("state_path must be a string or None")
        if self.latest is not None:
            raise ScheduleValueError(
                "Catch-up requires a fixed interval; .to() jobs cannot catch up"
            )

        self.catch_up_max = max_slots
        self.catch_up_state_path = state_path
        self.catch_up_retry_interrupted = bool(retry_interrupted)
        if self.job_func is not None:
            self._activate_catch_up()
        return self

    @property
    def slots(self) -> List[Slot]:
        """A copy of this job's logical run ledger, oldest first."""
        return list(self._slots)

    @property
    def skipped_slots(self) -> List[Slot]:
        """Logical shifts that will not be run, oldest first."""
        return [slot for slot in self._slots if slot.state == "skipped"]

    @property
    def interrupted_slots(self) -> List[Slot]:
        """Shifts that had started but had no recorded result, oldest first."""
        return [slot for slot in self._slots if slot.state == "interrupted"]

    @property
    def pending_catch_up(self) -> List[Slot]:
        """Caught-up shifts that are queued but not yet run, oldest first."""
        return list(self._catch_up_queue)

    def slot_at(self, scheduled_at: datetime.datetime) -> Optional[Slot]:
        """Return the ledger entry for a logical shift, if one exists."""
        for slot in self._slots:
            if slot.scheduled_at == scheduled_at:
                return slot
        return None

    @property
    def missed_slots(self) -> List[Slot]:
        """Logical shifts that were due but did not deliver a normal result."""
        return [
            slot
            for slot in self._slots
            if slot.state in ("skipped", "interrupted", "failed")
        ]

    def catch_up_report(self) -> List[Dict[str, Any]]:
        """Return a serializable accounting of every non-pending logical shift."""
        return [
            {
                "scheduled_at": slot.scheduled_at,
                "disposition": slot.disposition,
                "state": slot.state,
                "caught_up": slot.caught_up,
                "reason": slot.reason,
                "error": slot.error,
            }
            for slot in self._slots
            if slot.state != "scheduled"
        ]

    def _new_slot(self, scheduled_at: datetime.datetime) -> Slot:
        slot = Slot(scheduled_at)
        self._slots.append(slot)
        return slot

    def _slot_for(self, scheduled_at: datetime.datetime) -> Slot:
        slot = self.slot_at(scheduled_at)
        if slot is None:
            slot = self._new_slot(scheduled_at)
        return slot

    def _load_catch_up_payload(self) -> Optional[Dict[str, Any]]:
        path = self.catch_up_state_path
        if not path or not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError) as exc:
            logger.warning("Ignoring unreadable catch-up state %s: %s", path, exc)
            return None

    def _restore_catch_up_state(self) -> None:
        payload = self._load_catch_up_payload()
        if not payload:
            return
        key = self._catch_up_key()
        jobs = payload.get("jobs", {})
        entry = jobs.get(key)
        if entry is None:
            return
        if isinstance(entry, list):
            entry = {"slots": entry}
        for data in entry.get("slots", []):
            slot = Slot.from_dict(data)
            existing = self.slot_at(slot.scheduled_at)
            if existing is None:
                self._slots.append(slot)
            else:
                existing.__dict__.update(slot.__dict__)
        self._slots.sort(key=lambda item: item.scheduled_at)
        anchor = entry.get("anchor")
        if anchor:
            self._catch_up_anchor = datetime.datetime.fromisoformat(anchor)
        last_seen = entry.get("last_seen")
        if last_seen:
            self._catch_up_last_seen = datetime.datetime.fromisoformat(last_seen)
        next_run = entry.get("next_run")
        if next_run and self._slots:
            saved_next = datetime.datetime.fromisoformat(next_run)
            if saved_next > now_without_microseconds():
                self.next_run = saved_next

    def _catch_up_key(self) -> str:
        func = self.job_func
        name = getattr(func, "__name__", None)
        if name is None:
            return str(func)
        module = getattr(func, "__module__", "")
        qualname = getattr(func, "__qualname__", name)
        args = repr(getattr(func, "args", ()))
        kwargs = repr(sorted(getattr(func, "keywords", {}).items()))
        return "{}.{}{}{}".format(module, qualname, args, kwargs)

    def _save_catch_up_state(self) -> None:
        path = self.catch_up_state_path
        if not path:
            return
        payload: Dict[str, Any] = {"version": 1, "jobs": {}}
        current = self._load_catch_up_payload() or {}
        payload["jobs"].update(current.get("jobs", {}))
        payload["jobs"][self._catch_up_key()] = {
            "slots": [slot.to_dict() for slot in self._slots],
            "anchor": (
                self._catch_up_anchor.isoformat()
                if self._catch_up_anchor
                else None
            ),
            "last_seen": (
                self._catch_up_last_seen.isoformat()
                if self._catch_up_last_seen
                else None
            ),
            "next_run": self.next_run.isoformat() if self.next_run else None,
        }
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        temporary = path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)

    def _catch_up_period(self) -> Optional[datetime.timedelta]:
        if self.unit not in ("seconds", "minutes", "hours", "days", "weeks"):
            return None
        return datetime.timedelta(**{self.unit: self.interval})

    def _activate_catch_up(self) -> None:
        """Restore any ledger, then account for downtime since the last sighting."""
        period = self._catch_up_period()
        if period is None:
            raise ScheduleValueError("Catch-up requires a fixed time unit")

        self._slots = []
        self._catch_up_queue = []
        self._catch_up_last_seen = None
        self._catch_up_anchor = None
        self._catch_up_active = None
        self._restore_catch_up_state()
        now = now_without_microseconds()

        if not self._slots:
            if self.next_run is None:
                self._schedule_next_run()
            self._new_slot(self.next_run)
            self._catch_up_anchor = self.next_run
            self._catch_up_last_seen = now
            self._save_catch_up_state()
            return

        for slot in self._slots:
            if slot.state == "started":
                if self.catch_up_retry_interrupted:
                    slot.state = "skipped"
                    slot.skipped_at = now
                    slot.reason = "interrupted before restart"
                else:
                    slot.state = "interrupted"
                    slot.reason = "started before restart; result not delivered"

        self._catch_up_queue = []

        if self._catch_up_anchor is None:
            self._catch_up_anchor = min(slot.scheduled_at for slot in self._slots)

        self._materialize_grid(now)
        self._classify_downtime(now)
        self._queue_retryable_interruptions(now)
        self._catch_up_last_seen = now
        self._advance_catch_up_next_run(now, period)
        self._save_catch_up_state()

    def _queue_retryable_interruptions(self, now: datetime.datetime) -> None:
        if not self.catch_up_retry_interrupted:
            return
        retryable = [
            slot
            for slot in self._slots
            if slot.state == "skipped" and slot.reason == "interrupted before restart"
        ]
        if not retryable:
            return
        for slot in retryable:
            slot.state = "scheduled"
            slot.skipped_at = None
            slot.reason = "retry after interruption"
            slot.caught_up = True
            self._catch_up_queue.append(slot)
        self._catch_up_queue.sort(key=lambda item: item.scheduled_at)

    def _materialize_grid(self, now: datetime.datetime) -> None:
        """Create ledger entries for every grid point up to ``now``."""
        period = self._catch_up_period()
        if period is None:
            return
        if self._catch_up_anchor is None:
            return
        threshold = now - period * self.catch_up_ledger_limit
        if self._catch_up_last_seen is not None:
            threshold = max(threshold, self._catch_up_last_seen - period)
        cursor = self._catch_up_anchor
        if cursor < threshold:
            steps = (threshold - cursor) // period + 1
            cursor = cursor + period * steps
        while cursor <= now:
            if self.slot_at(cursor) is None:
                self._new_slot(cursor)
            cursor += period

    def _classify_downtime(self, now: datetime.datetime) -> None:
        """Skip or replay shifts that were already due when the process woke up."""
        if not self._catch_up_last_seen:
            return
        missed = [
            slot
            for slot in self._slots
            if slot.state == "scheduled"
            and not slot.caught_up
            and self._catch_up_last_seen < slot.scheduled_at < now
        ]
        if not missed:
            return
        for slot in missed:
            slot.state = "skipped"
            slot.skipped_at = now
            slot.reason = "missed while process was not running"
        self._queue_catch_up(now, missed)

    def _classify_overrun(self, now: datetime.datetime) -> None:
        """Skip shifts that became due while the current execution was running."""
        missed = [
            slot
            for slot in self._slots
            if slot.state == "scheduled"
            and not slot.caught_up
            and slot.scheduled_at < now
        ]
        if not missed:
            return
        for slot in missed:
            slot.state = "skipped"
            slot.skipped_at = now
            slot.reason = "shift was displaced by a slow run"

    def _queue_catch_up(
        self, now: datetime.datetime, missed: List[Slot]
    ) -> None:
        budget = min(self.catch_up_max or 0, len(missed))
        if budget:
            for slot in missed[len(missed) - budget :]:
                slot.state = "scheduled"
                slot.skipped_at = None
                slot.reason = None
                slot.caught_up = True
                self._catch_up_queue.append(slot)
        self._catch_up_queue.sort(key=lambda item: item.scheduled_at)

    def _advance_catch_up_next_run(
        self, now: datetime.datetime, period: datetime.timedelta
    ) -> None:
        if self._catch_up_queue:
            self.next_run = self._catch_up_queue[0].scheduled_at
            return
        if (
            self.next_run is not None
            and self.next_run >= now
            and self._is_on_grid(self.next_run)
        ):
            if self.slot_at(self.next_run) is None:
                self._new_slot(self.next_run)
            return
        next_run = self._next_grid_after(now)
        self.next_run = next_run
        if next_run is not None and self.slot_at(next_run) is None:
            self._new_slot(next_run)

    def _is_on_grid(self, moment: datetime.datetime) -> bool:
        period = self._catch_up_period()
        if period is None or self._catch_up_anchor is None:
            return False
        return (moment - self._catch_up_anchor) % period == datetime.timedelta(0)

    def _next_grid_after(self, moment: datetime.datetime) -> Optional[datetime.datetime]:
        period = self._catch_up_period()
        if period is None or self._catch_up_anchor is None:
            return self.next_run
        candidate = self._catch_up_anchor
        if candidate > moment:
            return candidate
        while candidate <= moment:
            candidate += period
        return candidate

    def _reconcile_catch_up(self) -> None:
        if self.catch_up_max is None:
            return
        now = now_without_microseconds()
        period = self._catch_up_period()
        if period is None:
            return

        last = max((slot.scheduled_at for slot in self._slots), default=None)
        if last is None:
            self._activate_catch_up()
            return

        if self._catch_up_active is not None:
            self._advance_catch_up_next_run(now, period)
            return

        previous_last_seen = self._catch_up_last_seen
        polling_gap = (
            previous_last_seen is not None
            and now - previous_last_seen > self.catch_up_poll_tolerance
        )
        if polling_gap:
            self._materialize_grid(now)
            self._classify_downtime(now)
        self._catch_up_last_seen = now
        self._advance_catch_up_next_run(now, period)

    def _run_with_catch_up(self):
        now = now_without_microseconds()
        if self._is_overdue(now):
            self._close_catch_up("job deadline passed")
            logger.debug("Cancelling job %s", self)
            return CancelJob

        if self._catch_up_queue:
            slot = self._catch_up_queue.pop(0)
            slot.caught_up = True
        else:
            if self.next_run is None:
                self._schedule_next_run()
            slot = self._slot_for(self.next_run)
            slot.caught_up = False

        self._catch_up_active = slot
        slot.state = "started"
        slot.started_at = now
        slot.attempts += 1
        slot.error = None
        self.last_run = now
        self._catch_up_last_seen = now
        if self._catch_up_queue:
            self.next_run = self._catch_up_queue[0].scheduled_at
        else:
            next_run = self._next_grid_after(slot.scheduled_at)
            self.next_run = next_run
            if next_run is not None and self.slot_at(next_run) is None:
                self._new_slot(next_run)
        self._save_catch_up_state()

        logger.debug("Running job %s", self)
        try:
            ret = self.job_func()
        except Exception as exc:
            slot.state = "failed"
            slot.finished_at = now_without_microseconds()
            slot.error = "{}: {}".format(type(exc).__name__, exc)
            self._catch_up_active = None
            self._save_catch_up_state()
            raise

        slot.state = "completed"
        slot.finished_at = now_without_microseconds()
        slot.recovered = bool(
            slot.attempts > 1 or slot.reason == "retry after interruption"
        )
        self._catch_up_active = None
        self._materialize_grid(slot.finished_at)
        self._classify_overrun(slot.finished_at)
        self._catch_up_last_seen = slot.finished_at
        if self._catch_up_queue:
            self.next_run = self._catch_up_queue[0].scheduled_at
        else:
            next_run = self._next_grid_after(slot.finished_at)
            self.next_run = next_run
            if next_run is not None and self.slot_at(next_run) is None:
                self._new_slot(next_run)
        self._save_catch_up_state()

        if self._is_overdue(self.next_run):
            self._close_catch_up("job deadline passed")
            logger.debug("Cancelling job %s", self)
            return CancelJob
        return ret

    def _close_catch_up(self, reason: str) -> None:
        now = now_without_microseconds()
        for slot in self._slots:
            if slot.state == "scheduled":
                slot.state = "skipped"
                slot.skipped_at = now
                slot.reason = reason
        self._catch_up_queue = []
        self._catch_up_active = None
        self._save_catch_up_state()

    @property
    def should_run(self) -> bool:
        """
        :return: ``True`` if the job should be run now.
        """
        if self.catch_up_max is not None:
            self._reconcile_catch_up()
        assert self.next_run is not None, "must run _schedule_next_run before"
        return datetime.datetime.now() >= self.next_run

    def run(self):
        """
        Run the job and immediately reschedule it.
        If the job's deadline is reached (configured using .until()), the job is not
        run and CancelJob is returned immediately. If the next scheduled run exceeds
        the job's deadline, CancelJob is returned after the execution. In this latter
        case CancelJob takes priority over any other returned value.

        :return: The return value returned by the `job_func`, or CancelJob if the job's
                 deadline is reached.

        """
        if self.catch_up_max is not None:
            self._reconcile_catch_up()
            return self._run_with_catch_up()

        if self._is_overdue(datetime.datetime.now()):
            logger.debug("Cancelling job %s", self)
            return CancelJob

        logger.debug("Running job %s", self)
        ret = self.job_func()
        self.last_run = datetime.datetime.now()
        self._schedule_next_run()

        if self._is_overdue(self.next_run):
            logger.debug("Cancelling job %s", self)
            return CancelJob
        return ret

    def _schedule_next_run(self) -> None:
        """
        Compute the instant when this job should run next.
        """
        if self.unit not in ("seconds", "minutes", "hours", "days", "weeks"):
            raise ScheduleValueError(
                "Invalid unit (valid units are `seconds`, `minutes`, `hours`, "
                "`days`, and `weeks`)"
            )
        if self.latest is not None:
            if not (self.latest >= self.interval):
                raise ScheduleError("`latest` is greater than `interval`")
            interval = random.randint(self.interval, self.latest)
        else:
            interval = self.interval

        # Do all computation in the context of the requested timezone
        now = datetime.datetime.now(self.at_time_zone)

        next_run = now

        if self.start_day is not None:
            if self.unit != "weeks":
                raise ScheduleValueError("`unit` should be 'weeks'")
            next_run = _move_to_next_weekday(next_run, self.start_day)

        if self.at_time is not None:
            next_run = self._move_to_at_time(next_run)

        period = datetime.timedelta(**{self.unit: interval})
        if interval != 1:
            next_run += period

        while next_run <= now:
            next_run += period

        next_run = self._correct_utc_offset(
            next_run, fixate_time=(self.at_time is not None)
        )

        # To keep the api consistent with older versions, we have to set the 'next_run' to a naive timestamp in the local timezone.
        # Because we want to stay backwards compatible with older versions.
        if self.at_time_zone is not None:
            # Convert back to the local timezone
            next_run = next_run.astimezone()

            next_run = next_run.replace(tzinfo=None)

        self.next_run = next_run
        if self.catch_up_max is not None and not self._slots:
            self._new_slot(next_run)

    def _move_to_at_time(self, moment: datetime.datetime) -> datetime.datetime:
        """
        Takes a datetime and moves the time-component to the job's at_time.
        """
        if self.at_time is None:
            return moment

        kwargs = {"second": self.at_time.second, "microsecond": 0}

        if self.unit == "days" or self.start_day is not None:
            kwargs["hour"] = self.at_time.hour

        if self.unit in ["days", "hours"] or self.start_day is not None:
            kwargs["minute"] = self.at_time.minute

        moment = moment.replace(**kwargs)  # type: ignore

        # When we set the time elements, we might end up in a different UTC-offset than the current offset.
        # This happens when we cross into or out of daylight saving time.
        moment = self._correct_utc_offset(moment, fixate_time=True)

        return moment

    def _correct_utc_offset(
        self, moment: datetime.datetime, fixate_time: bool
    ) -> datetime.datetime:
        """
        Given a datetime, corrects any mistakes in the utc offset.
        This is similar to pytz' normalize, but adds the ability to attempt
        keeping the time-component at the same hour/minute/second.
        """
        if self.at_time_zone is None:
            return moment
        # Normalize corrects the utc-offset to match the timezone
        # For example: When a date&time&offset does not exist within a timezone,
        # the normalization will change the utc-offset to where it is valid.
        # It does this while keeping the moment in time the same, by moving the
        # time component opposite of the utc-change.
        offset_before_normalize = moment.utcoffset()
        moment = self.at_time_zone.normalize(moment)
        offset_after_normalize = moment.utcoffset()

        if offset_before_normalize == offset_after_normalize:
            # There was no change in the utc-offset, datetime didn't change.
            return moment

        # The utc-offset and time-component has changed

        if not fixate_time:
            # No need to fixate the time.
            return moment

        offset_diff = offset_after_normalize - offset_before_normalize

        # Adjust the time to reset the date-time to have the same HH:mm components
        moment -= offset_diff

        # Check if moving the timestamp back by the utc-offset-difference made it end up
        # in a moment that does not exist within the current timezone/utc-offset
        re_normalized_offset = self.at_time_zone.normalize(moment).utcoffset()
        if re_normalized_offset != offset_after_normalize:
            # We ended up in a DST Gap. The requested 'at' time does not exist
            # within the current timezone/utc-offset. As a best effort, we will
            # schedule the job 1 offset later than possible.
            # For example, if 02:23 does not exist (because DST moves from 02:00
            # to 03:00), this will schedule the job at 03:23.
            moment += offset_diff
        return moment

    def _is_overdue(self, when: datetime.datetime):
        return self.cancel_after is not None and when > self.cancel_after

    def _decode_datetimestr(
        self, datetime_str: str, formats: List[str]
    ) -> Optional[datetime.datetime]:
        for f in formats:
            try:
                return datetime.datetime.strptime(datetime_str, f)
            except ValueError:
                pass
        return None


# The following methods are shortcuts for not having to
# create a Scheduler instance:

#: Default :class:`Scheduler <Scheduler>` object
default_scheduler = Scheduler()

#: Default :class:`Jobs <Job>` list
jobs = default_scheduler.jobs  # todo: should this be a copy, e.g. jobs()?


def every(interval: int = 1) -> Job:
    """Calls :meth:`every <Scheduler.every>` on the
    :data:`default scheduler instance <default_scheduler>`.
    """
    return default_scheduler.every(interval)


def run_pending() -> None:
    """Calls :meth:`run_pending <Scheduler.run_pending>` on the
    :data:`default scheduler instance <default_scheduler>`.
    """
    default_scheduler.run_pending()


def run_all(delay_seconds: int = 0) -> None:
    """Calls :meth:`run_all <Scheduler.run_all>` on the
    :data:`default scheduler instance <default_scheduler>`.
    """
    default_scheduler.run_all(delay_seconds=delay_seconds)


def reconcile(tag: Optional[Hashable] = None) -> None:
    """Calls :meth:`reconcile <Scheduler.reconcile>` on the
    :data:`default scheduler instance <default_scheduler>`.
    """
    default_scheduler.reconcile(tag)


def get_jobs(tag: Optional[Hashable] = None) -> List[Job]:
    """Calls :meth:`get_jobs <Scheduler.get_jobs>` on the
    :data:`default scheduler instance <default_scheduler>`.
    """
    return default_scheduler.get_jobs(tag)


def clear(tag: Optional[Hashable] = None) -> None:
    """Calls :meth:`clear <Scheduler.clear>` on the
    :data:`default scheduler instance <default_scheduler>`.
    """
    default_scheduler.clear(tag)


def cancel_job(job: Job) -> None:
    """Calls :meth:`cancel_job <Scheduler.cancel_job>` on the
    :data:`default scheduler instance <default_scheduler>`.
    """
    default_scheduler.cancel_job(job)


def next_run(tag: Optional[Hashable] = None) -> Optional[datetime.datetime]:
    """Calls :meth:`next_run <Scheduler.next_run>` on the
    :data:`default scheduler instance <default_scheduler>`.
    """
    return default_scheduler.get_next_run(tag)


def idle_seconds() -> Optional[float]:
    """Calls :meth:`idle_seconds <Scheduler.idle_seconds>` on the
    :data:`default scheduler instance <default_scheduler>`.
    """
    return default_scheduler.idle_seconds


def repeat(job, *args, **kwargs):
    """
    Decorator to schedule a new periodic job.

    Any additional arguments are passed on to the decorated function
    when the job runs.

    :param job: a :class:`Jobs <Job>`
    """

    def _schedule_decorator(decorated_function):
        job.do(decorated_function, *args, **kwargs)
        return decorated_function

    return _schedule_decorator


def _move_to_next_weekday(moment: datetime.datetime, weekday: str):
    """
    Move the given timestamp to the nearest given weekday. May be this week
    or next week. If the timestamp is already at the given weekday, it is not
    moved.
    """
    weekday_index = _weekday_index(weekday)

    days_ahead = weekday_index - moment.weekday()
    if days_ahead < 0:
        # Target day already happened this week, move to next week
        days_ahead += 7
    return moment + datetime.timedelta(days=days_ahead)


def _weekday_index(day: str) -> int:
    weekdays = (
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
    )
    if day not in weekdays:
        raise ScheduleValueError(
            "Invalid start day (valid start days are {})".format(weekdays)
        )
    return weekdays.index(day)
