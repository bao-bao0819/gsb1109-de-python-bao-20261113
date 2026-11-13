"""
Backfill drill (补采演练) support built on top of :mod:`schedule`.

A drill re-collects data for one specific date in a fixed order:

1. the *catalog* (目录) is collected first;
2. a single *dictionary* (字典) is derived from the catalog and shared by
   every detail;
3. each *detail* (明细) runs against the catalog, the shared dictionary and
   the receipts that were supplied up front.

Every stage returns a :class:`Receipt` (or a plain value, which is wrapped
into one). A receipt carries a verifiable ``version``: when a detail needs
an input whose receipt is not available, the detail is not executed but is
reported as ``missing`` in this round instead. A failing stage blocks the
details that depend on it, while independent details still produce results.

A drill can be attached to a :class:`~schedule.Scheduler` as an ordinary
daily job. Triggering a drill manually never touches the routine schedule:
the enclosing job keeps recomputing its next trigger from the clock, so its
next run is neither advanced nor delayed.
"""

from dataclasses import dataclass
import datetime
import uuid
from typing import (
    Any,
    Callable,
    Dict,
    FrozenSet,
    Iterable,
    List,
    Mapping,
    Optional,
    Set,
)

from schedule import Job, Scheduler, ScheduleError, default_scheduler, logger

#: Stage name of the catalog step.
CATALOG_NAME = "catalog"
#: Stage name of the shared dictionary step.
DICTIONARY_NAME = "dictionary"
#: Receipt names reserved for the built-in stages.
RESERVED_NAMES = (CATALOG_NAME, DICTIONARY_NAME)

#: The stage ran successfully and produced a receipt.
STATUS_SUCCESS = "success"
#: The stage function raised an exception.
STATUS_FAILED = "failed"
#: An upstream stage the detail depends on did not succeed.
STATUS_BLOCKED = "blocked"
#: A required input receipt (verifiable version) was not available.
STATUS_MISSING = "missing"


class DrillError(ScheduleError):
    """Base class for backfill drill errors."""


class MissingInputError(DrillError):
    """Raised when a required verifiable receipt is not available."""

    def __init__(self, missing: Iterable[str]):
        self.missing = frozenset(missing)
        super().__init__(
            "Missing verifiable receipts: {}".format(", ".join(sorted(self.missing)))
        )


@dataclass(frozen=True)
class Receipt:
    """
    Proof that a stage (catalog, dictionary or detail) completed.

    :param name: Unique input name under which the receipt can be requested.
    :param version: Verifiable version of the produced data.
    :param produced_at: Moment at which the data was produced.
    :param payload: Optional data carried alongside the receipt.
    """

    name: str
    version: str
    produced_at: datetime.datetime
    payload: Mapping[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise DrillError("Receipt name must be a non-empty string")
        if not isinstance(self.version, str) or not self.version:
            raise DrillError("Receipt version must be a non-empty string")
        if not isinstance(self.produced_at, datetime.datetime):
            raise DrillError("Receipt.produced_at must be a datetime.datetime")
        if self.payload is None:
            object.__setattr__(self, "payload", {})


@dataclass(frozen=True)
class DrillContext:
    """
    Input handed to every stage function.

    :param date: The date for which the drill is running.
    :param receipts: All receipts currently available, keyed by their name.
    """

    date: datetime.date
    receipts: Mapping[str, Receipt]

    @property
    def dictionary(self) -> Optional[Receipt]:
        """The shared dictionary receipt, or the catalog receipt when the
        drill does not define a separate dictionary stage."""
        return self.receipts.get(DICTIONARY_NAME) or self.receipts.get(CATALOG_NAME)

    def receipt(self, name: str) -> Receipt:
        """Return the receipt registered under ``name``."""
        try:
            return self.receipts[name]
        except KeyError:
            raise MissingInputError((name,)) from None

    def payload(self, name: str) -> Any:
        """Return the payload of the receipt registered under ``name``."""
        return self.receipt(name).payload


#: Signature of a catalog, dictionary or detail function.
StageFunc = Callable[[DrillContext], Any]


class DetailSpec:
    """
    Declaration of one detail (明细) within a drill.

    :param name: Unique detail name. ``catalog`` and ``dictionary`` are
        reserved.
    :param func: Callable that performs the detail. It receives a
        :class:`DrillContext` and returns a :class:`Receipt` or any plain
        value (which is wrapped into a receipt named after the detail).
    :param inputs: Receipt names the detail needs before it may run. When one
        of them is not available, the detail is reported as ``missing``
        instead of being executed.
    :param blocked_by: Names of other details that must succeed. When one of
        them does not succeed, this detail is reported as ``blocked``.
    """

    def __init__(
        self,
        name: str,
        func: StageFunc,
        inputs: Iterable[str] = (),
        blocked_by: Iterable[str] = (),
    ) -> None:
        if not isinstance(name, str) or not name:
            raise DrillError("Detail name must be a non-empty string")
        if name in RESERVED_NAMES:
            raise DrillError(
                "Detail name {!r} is reserved for a built-in stage".format(name)
            )
        if not callable(func):
            raise DrillError("Detail func must be callable")
        self.name = name
        self.func = func
        self.inputs = frozenset(inputs)
        self.blocked_by = frozenset(blocked_by)

    def missing_inputs(self, available: Iterable[str]) -> frozenset:
        """Return the required input names that are not available."""
        return self.inputs - frozenset(available)


@dataclass(frozen=True)
class DetailResult:
    """Outcome of a single stage in a drill run."""

    name: str
    status: str
    receipt: Optional[Receipt] = None
    error: Optional[BaseException] = None
    blocked_by: FrozenSet[str] = frozenset()

    @property
    def ok(self) -> bool:
        return self.status == STATUS_SUCCESS


@dataclass(frozen=True)
class DrillReport:
    """
    Result of one drill run.

    :param date: The date for which the drill ran.
    :param results: Outcomes of every stage, keyed by stage name.
    :param missing_inputs: Missing receipt names per detail that could not run.
    """

    date: datetime.date
    results: Mapping[str, DetailResult]
    missing_inputs: Mapping[str, frozenset]

    @property
    def ok(self) -> bool:
        """``True`` when every stage succeeded."""
        return all(result.ok for result in self.results.values())

    @property
    def failed(self) -> tuple:
        """Names of the stages that raised an exception."""
        return tuple(
            name
            for name, result in self.results.items()
            if result.status == STATUS_FAILED
        )

    @property
    def blocked(self) -> tuple:
        """Names of the details blocked by an unsuccessful upstream stage."""
        return tuple(
            name
            for name, result in self.results.items()
            if result.status == STATUS_BLOCKED
        )

    @property
    def missing(self) -> tuple:
        """Names of the details skipped because an input receipt was absent."""
        return tuple(
            name
            for name, result in self.results.items()
            if result.status == STATUS_MISSING
        )

    def get(self, name: str) -> DetailResult:
        """Return the :class:`DetailResult` recorded under ``name``."""
        return self.results[name]


def _coerce_date(value: Any) -> datetime.date:
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    if isinstance(value, str):
        try:
            return datetime.datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError as exc:
            raise DrillError(
                "Invalid drill date {!r}, expected YYYY-MM-DD".format(value)
            ) from exc
    raise DrillError(
        "Drill date must be a datetime.date or a YYYY-MM-DD string, got {}".format(
            type(value).__name__
        )
    )


def _coerce_receipt(value: Any, stage_name: str) -> Receipt:
    if isinstance(value, Receipt):
        return value
    produced_at = datetime.datetime.now()
    if isinstance(value, Mapping):
        version = value.get("version")
        if not version:
            raise DrillError(
                "Receipt mapping for stage {!r} must contain a 'version'".format(
                    stage_name
                )
            )
        name = value.get("name", stage_name)
        payload = {
            key: item
            for key, item in value.items()
            if key not in ("name", "version", "produced_at")
        }
        return Receipt(
            name=str(name),
            version=str(version),
            produced_at=produced_at,
            payload=payload,
        )
    return Receipt(
        name=stage_name,
        version=uuid.uuid4().hex,
        produced_at=produced_at,
        payload={"value": value},
    )


class BackfillDrill:
    """
    Entry point for a backfill drill on a specific date.

    :param name: Human readable drill name.
    :param catalog_func: Stage collecting the catalog (目录). Runs first.
    :param dictionary_func: Optional stage building the dictionary (字典)
        from the catalog. It runs once and its receipt is shared by every
        detail. When omitted, the catalog receipt acts as the dictionary.
    :param details: Optional initial :class:`DetailSpec` instances.
    """

    def __init__(
        self,
        name: str,
        catalog_func: StageFunc,
        dictionary_func: Optional[StageFunc] = None,
        details: Iterable[DetailSpec] = (),
    ) -> None:
        if not isinstance(name, str) or not name:
            raise DrillError("Drill name must be a non-empty string")
        if not callable(catalog_func):
            raise DrillError("catalog_func must be callable")
        if dictionary_func is not None and not callable(dictionary_func):
            raise DrillError("dictionary_func must be callable")
        self.name = name
        self._catalog_func = catalog_func
        self._dictionary_func = dictionary_func
        self._details: List[DetailSpec] = []
        for detail in details:
            self.add_detail(detail)
        self._scheduler: Optional[Scheduler] = None
        self._job: Optional[Job] = None
        self._receipts_provider: Optional[Callable[[], Any]] = None
        #: Reports produced by every :meth:`run` or scheduled trigger.
        self.history: List[DrillReport] = []

    @property
    def details(self) -> tuple:
        """Registered details in execution order."""
        return tuple(self._details)

    @property
    def job(self) -> Optional[Job]:
        """The routine job created by :meth:`schedule`, if any."""
        return self._job

    def add_detail(
        self,
        detail: Any,
        func: Optional[StageFunc] = None,
        inputs: Iterable[str] = (),
        blocked_by: Iterable[str] = (),
    ) -> DetailSpec:
        """Register a detail, either a ready :class:`DetailSpec` or by
        providing its name and function directly."""
        if isinstance(detail, DetailSpec):
            spec = detail
        else:
            if func is None:
                raise DrillError("add_detail() requires a callable func")
            spec = DetailSpec(detail, func, inputs, blocked_by)
        if any(existing.name == spec.name for existing in self._details):
            raise DrillError("Duplicate detail name {!r}".format(spec.name))
        self._details.append(spec)
        return spec

    def run(self, date: Any, receipts: Any = ()) -> DrillReport:
        """
        Execute the drill for ``date``.

        :param date: A :class:`datetime.date`, :class:`datetime.datetime`
            or ``YYYY-MM-DD`` string.
        :param receipts: Existing successful receipts. Either an iterable of
            :class:`Receipt` instances / receipt mappings, or a mapping of
            receipt name to receipt. They satisfy the details' ``inputs``.
        :return: A :class:`DrillReport`. Stage failures never escape; they
            are captured in the report.
        """
        run_date = _coerce_date(date)
        available = self._index_receipts(receipts)
        self._validate_references()

        results: Dict[str, DetailResult] = {}
        missing: Dict[str, frozenset] = {}

        catalog_receipt = self._run_stage(
            CATALOG_NAME, self._catalog_func, run_date, available, results
        )
        if catalog_receipt is not None:
            available[CATALOG_NAME] = catalog_receipt
            if self._dictionary_func is None:
                # The catalog doubles as the shared dictionary.
                available[DICTIONARY_NAME] = catalog_receipt
            else:
                dictionary_receipt = self._run_stage(
                    DICTIONARY_NAME,
                    self._dictionary_func,
                    run_date,
                    available,
                    results,
                )
                if dictionary_receipt is not None:
                    available[DICTIONARY_NAME] = dictionary_receipt
        elif self._dictionary_func is not None:
            # The dictionary cannot be built without a catalog.
            results[DICTIONARY_NAME] = DetailResult(
                DICTIONARY_NAME,
                STATUS_BLOCKED,
                blocked_by=frozenset({CATALOG_NAME}),
            )

        for spec in self._details:
            self._run_detail(spec, run_date, available, results, missing)

        report = DrillReport(run_date, dict(results), dict(missing))
        self.history.append(report)
        return report

    def schedule(
        self,
        scheduler: Optional[Scheduler] = None,
        at: Optional[str] = None,
        receipts_provider: Optional[Callable[[], Any]] = None,
    ) -> Job:
        """
        Attach the drill to a scheduler as an ordinary daily job.

        :param scheduler: Scheduler to register with. Defaults to the module
            level default scheduler.
        :param at: Optional ``HH:MM[:SS]`` time of day, following
            :meth:`schedule.Job.at`.
        :param receipts_provider: Optional callable returning the receipts
            available at every routine trigger.
        :return: The registered :class:`schedule.Job`.

        The drill only observes job triggers; it never changes the job's
        ``next_run``, so running a drill manually leaves the routine timing
        untouched.
        """
        if self._job is not None:
            raise DrillError("Drill {!r} is already scheduled".format(self.name))
        self._scheduler = scheduler or default_scheduler
        self._receipts_provider = receipts_provider
        builder = self._scheduler.every().day
        if at is not None:
            builder = builder.at(at)
        self._job = builder.do(self._run_from_schedule)
        return self._job

    def unschedule(self) -> None:
        """Remove the routine job created by :meth:`schedule`."""
        if self._job is None or self._scheduler is None:
            return
        self._scheduler.cancel_job(self._job)
        self._job = None
        self._scheduler = None

    def _run_from_schedule(self) -> Optional[DrillReport]:
        provider = self._receipts_provider
        try:
            receipts = provider() if provider is not None else ()
        except Exception:
            logger.exception("Drill %r receipts provider failed", self.name)
            return None
        try:
            return self.run(datetime.datetime.now().date(), receipts)
        except Exception:
            logger.exception("Drill %r failed unexpectedly", self.name)
            return None

    def _validate_references(self) -> None:
        seen: Set[str] = set()
        for spec in self._details:
            unknown = spec.blocked_by - {item.name for item in self._details}
            if unknown:
                raise DrillError(
                    "Detail {!r} blocks on unknown details: {}".format(
                        spec.name, ", ".join(sorted(unknown))
                    )
                )
            forward = spec.blocked_by - seen
            if forward:
                raise DrillError(
                    "Detail {!r} blocks on details registered after it: "
                    "{}".format(spec.name, ", ".join(sorted(forward)))
                )
            seen.add(spec.name)

    def _index_receipts(self, receipts: Any) -> Dict[str, Receipt]:
        if isinstance(receipts, Mapping):
            items = list(receipts.values())
        else:
            items = list(receipts)
        indexed: Dict[str, Receipt] = {}
        for item in items:
            if isinstance(item, Receipt):
                receipt = item
            elif isinstance(item, Mapping) and item.get("name") and item.get("version"):
                receipt = _coerce_receipt(item, str(item["name"]))
            else:
                raise DrillError(
                    "External receipts must be Receipt objects or mappings "
                    "with 'name' and 'version', got {}".format(type(item).__name__)
                )
            if receipt.name in RESERVED_NAMES:
                raise DrillError(
                    "Receipt name {!r} is reserved for a built-in stage".format(
                        receipt.name
                    )
                )
            if receipt.name in indexed:
                raise DrillError(
                    "Duplicate receipt name {!r}".format(receipt.name)
                )
            indexed[receipt.name] = receipt
        return indexed

    def _run_stage(
        self,
        name: str,
        func: StageFunc,
        run_date: datetime.date,
        available: Mapping[str, Receipt],
        results: Dict[str, DetailResult],
    ) -> Optional[Receipt]:
        context = DrillContext(run_date, dict(available))
        try:
            receipt = _coerce_receipt(func(context), name)
        except Exception as exc:
            results[name] = DetailResult(name, STATUS_FAILED, error=exc)
            return None
        results[name] = DetailResult(name, STATUS_SUCCESS, receipt=receipt)
        return receipt

    def _run_detail(
        self,
        spec: DetailSpec,
        run_date: datetime.date,
        available: Dict[str, Receipt],
        results: Dict[str, DetailResult],
        missing: Dict[str, frozenset],
    ) -> None:
        if not results[CATALOG_NAME].ok:
            results[spec.name] = DetailResult(
                spec.name, STATUS_BLOCKED, blocked_by=frozenset({CATALOG_NAME})
            )
            return
        if (
            self._dictionary_func is not None
            and not results[DICTIONARY_NAME].ok
        ):
            results[spec.name] = DetailResult(
                spec.name,
                STATUS_BLOCKED,
                blocked_by=frozenset({DICTIONARY_NAME}),
            )
            return

        blockers = spec.blocked_by.intersection(
            name for name, result in results.items() if not result.ok
        )
        if blockers:
            results[spec.name] = DetailResult(
                spec.name, STATUS_BLOCKED, blocked_by=frozenset(blockers)
            )
            return

        absent = spec.missing_inputs(available)
        if absent:
            missing[spec.name] = frozenset(absent)
            results[spec.name] = DetailResult(
                spec.name, STATUS_MISSING, blocked_by=frozenset(absent)
            )
            return

        context = DrillContext(run_date, dict(available))
        try:
            receipt = _coerce_receipt(spec.func(context), spec.name)
        except Exception as exc:
            results[spec.name] = DetailResult(spec.name, STATUS_FAILED, error=exc)
            return
        available[spec.name] = receipt
        results[spec.name] = DetailResult(
            spec.name, STATUS_SUCCESS, receipt=receipt
        )


def run_drill(
    date: Any,
    catalog_func: StageFunc,
    details: Iterable[DetailSpec],
    dictionary_func: Optional[StageFunc] = None,
    receipts: Any = (),
    name: str = "backfill-drill",
) -> DrillReport:
    """Convenience wrapper that builds a :class:`BackfillDrill` and runs it
    once for ``date``."""
    drill = BackfillDrill(name, catalog_func, dictionary_func, details)
    return drill.run(date, receipts)
