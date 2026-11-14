"""Historical admission decisions for formal batches uploaded in test format.

Partners are occasionally allowed to upload a formal batch through the test
pipeline. Such an approval:

* is issued per partner (source),
* is limited to a semantic version range,
* stays valid for a few business days only,
* can be revoked, and such a revocation may be backdated,
* may be scoped to a single build identifier,
* does not turn a prerelease into a release by itself.

This module keeps those rules in immutable records and provides a single
decision entry point with two historical views:

* the view with the information available at upload time (records backfilled
  later are invisible, revocations issued later are not yet in force),
* the view with the records completed today (backfills and revocations are
  visible).

Every positive decision names the clause that was in force; every negative
decision carries the trace of every clause that was considered.
"""

import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Callable, Optional, Tuple, Union

from .version import Version

#: The artifact's source is not the source the clause was issued to.
REASON_SOURCE_MISMATCH = "source_mismatch"
#: The clause was not yet effective or had already expired at the view moment.
REASON_OUTSIDE_WINDOW = "outside_validity_window"
#: The clause had been revoked at or before the view moment.
REASON_REVOKED = "revoked"
#: The version number falls outside the clause's semantic version range.
REASON_OUT_OF_RANGE = "version_out_of_range"
#: A prerelease cannot be admitted by a clause that does not allow prereleases.
REASON_PRERELEASE_NOT_ALLOWED = "prerelease_not_allowed"
#: The clause is build scoped and the artifact carries a different build.
REASON_BUILD_MISMATCH = "build_mismatch"

#: Trace status marking the clause that actually admitted the artifact.
STATUS_APPLIED = "applied"
#: Trace status marking a clause that was considered but could not admit.
STATUS_SKIPPED = "skipped"

_COMPARATOR_RE = re.compile(r"^(>=|<=|==|!=|>|<)(.+)$")


def _require_aware(moment: datetime, name: str) -> datetime:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return moment


class VersionRange:
    """An inclusive semantic version range expressed as AND-ed comparators.

    The expression is whitespace separated comparators, for example
    ``">=1.2.0 <2.0.0"`` or ``"==3.0.0"``. A version is a member only when
    every comparator matches (logical AND).

    A prerelease version is never a member merely because its numeric parts
    fall inside the range. Membership for prereleases must be enabled
    explicitly through the ``allow_prerelease`` flag; the range itself does
    not promote prereleases to releases.

    :param expression: the comparator expression defining the range
    :raises ValueError: if the expression is empty or contains a comparator
        or version that cannot be parsed
    """

    __slots__ = ("_comparators", "_expression")

    def __init__(self, expression: str):
        if not isinstance(expression, str) or not expression.strip():
            raise ValueError("version range expression must be a non-empty string")
        comparators = tuple(token.strip() for token in expression.split())
        for comparator in comparators:
            match = _COMPARATOR_RE.match(comparator)
            if match is None:
                raise ValueError(
                    f"invalid comparator {comparator!r} in range {expression!r}"
                )
            Version.parse(match.group(2))
        self._expression = " ".join(comparators)
        self._comparators = comparators

    @property
    def expression(self) -> str:
        """The normalized comparator expression of the range."""
        return self._expression

    def contains(
        self, version: Version, allow_prerelease: bool = False
    ) -> bool:
        """Return whether the version is a member of the range.

        :param version: the version to test
        :param allow_prerelease: when false (the default), every prerelease
            version is rejected even when its numeric core is inside the
            range
        :return: true if the version is a member under the prerelease policy
        """
        matched, _ = self.membership(version, allow_prerelease)
        return matched

    def membership(
        self, version: Version, allow_prerelease: bool
    ) -> Tuple[bool, Optional[str]]:
        """Return the membership result together with a machine reason.

        :param version: the version to test
        :param allow_prerelease: whether prerelease versions may be members
        :return: a ``(matched, reason)`` pair; ``reason`` is ``None`` on a
            match and otherwise one of :data:`REASON_PRERELEASE_NOT_ALLOWED`
            or :data:`REASON_OUT_OF_RANGE`
        """
        if version.prerelease is not None and not allow_prerelease:
            return False, REASON_PRERELEASE_NOT_ALLOWED
        if not all(version.match(comparator) for comparator in self._comparators):
            return False, REASON_OUT_OF_RANGE
        return True, None

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, VersionRange)
            and other._expression == self._expression
        )

    def __hash__(self) -> int:
        return hash(self._expression)

    def __repr__(self) -> str:
        return f"VersionRange({self._expression!r})"


@dataclass(frozen=True, slots=True)
class ReleaseExemption:
    """A time limited, source specific exemption clause.

    The clause is an immutable record. Revoking an approval does not mutate
    history; use :meth:`with_revocation` to derive a new record carrying the
    revocation timestamp, so views reconstructed for earlier moments still
    see the clause as it was in force then.

    :param id: stable clause identifier, quoted back on every decision the
        clause drives
    :param source: the partner (upload source) the clause was issued to
    :param version_range: the admitted versions, as a :class:`VersionRange`
        or a comparator expression string
    :param effective_from: inclusive start of the validity window
    :param effective_until: exclusive end of the validity window, or ``None``
        for an open ended clause
    :param revoked_at: point in time the revocation takes effect (inclusive),
        or ``None``. The value may be backdated; whether the revocation was
        actually knowable at a historical view moment is controlled by
        ``revocation_recorded_at``
    :param revocation_recorded_at: when the revocation record entered the
        system; defaults to ``revoked_at``. A later timestamp models a
        backdated revocation: views reconstructed for earlier moments still
        see the clause as it was in force then
    :param build: when set, only artifacts carrying this exact build
        identifier are admitted; the clause never leaks to other artifacts of
        the same priority
    :param prereleases_allowed: whether prerelease artifacts may be admitted
        under this clause; defaults to false
    :param known_at: when the record became available in the system; defaults
        to ``effective_from``. Backfilled records carry a later timestamp so
        upload-time views cannot see information that did not exist yet
    :param note: optional human readable description of the clause
    """

    id: str
    source: str
    version_range: Union[VersionRange, str]
    effective_from: datetime
    effective_until: Optional[datetime] = None
    revoked_at: Optional[datetime] = None
    revocation_recorded_at: Optional[datetime] = None
    build: Optional[str] = None
    prereleases_allowed: bool = False
    known_at: Optional[datetime] = None
    note: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise ValueError("exemption id must be a non-empty string")
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("exemption source must be a non-empty string")
        if isinstance(self.version_range, str):
            object.__setattr__(self, "version_range", VersionRange(self.version_range))
        elif not isinstance(self.version_range, VersionRange):
            raise TypeError("version_range must be a VersionRange or a string")
        _require_aware(self.effective_from, "effective_from")
        if self.effective_until is not None:
            _require_aware(self.effective_until, "effective_until")
            if self.effective_until <= self.effective_from:
                raise ValueError("effective_until must be later than effective_from")
        if self.revoked_at is not None:
            _require_aware(self.revoked_at, "revoked_at")
            recorded_at = (
                self.revocation_recorded_at
                if self.revocation_recorded_at is not None
                else self.revoked_at
            )
            _require_aware(recorded_at, "revocation_recorded_at")
            object.__setattr__(self, "revocation_recorded_at", recorded_at)
        elif self.revocation_recorded_at is not None:
            raise ValueError(
                "revocation_recorded_at requires revoked_at to be set"
            )
        known_at = self.known_at if self.known_at is not None else self.effective_from
        _require_aware(known_at, "known_at")
        object.__setattr__(self, "known_at", known_at)

    @property
    def range(self) -> VersionRange:
        """The parsed semantic version range of the clause."""
        return self.version_range  # type: ignore[return-value]

    def is_active(self, moment: datetime) -> bool:
        """Return whether the validity window covers ``moment``."""
        if moment < self.effective_from:
            return False
        return self.effective_until is None or moment < self.effective_until

    def is_revoked(self, moment: datetime) -> bool:
        """Return whether the revocation is in force at ``moment``.

        This only looks at the effective time of the revocation. Whether the
        revocation record was actually knowable at a view moment is decided
        separately through ``revocation_recorded_at`` in :class:`ReleaseGate`.
        """
        return self.revoked_at is not None and self.revoked_at <= moment

    def with_revocation(
        self,
        revoked_at: datetime,
        recorded_at: Optional[datetime] = None,
    ) -> "ReleaseExemption":
        """Return a copy of the clause carrying a revocation timestamp.

        :param revoked_at: inclusive point in time the revocation takes effect
        :param recorded_at: when the revocation record entered the system;
            defaults to ``revoked_at``. Pass a later value for a backdated
            revocation recorded after the fact
        """
        _require_aware(revoked_at, "revoked_at")
        if recorded_at is not None:
            _require_aware(recorded_at, "recorded_at")
        return replace(
            self,
            revoked_at=revoked_at,
            revocation_recorded_at=recorded_at,
        )


@dataclass(frozen=True, slots=True)
class Artifact:
    """An uploaded artifact asking for admission.

    :param source: the upload source (partner identifier)
    :param version: the parsed semantic version of the artifact
    """

    source: str
    version: Version

    def __post_init__(self) -> None:
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("artifact source must be a non-empty string")
        if not isinstance(self.version, Version):
            raise TypeError("artifact version must be a semver.Version")


@dataclass(frozen=True, slots=True)
class TraceEntry:
    """One evaluated clause, fully self describing for audits."""

    clause_id: str
    source: str
    status: str
    reason: Optional[str]
    detail: str
    version_range: str
    effective_from: datetime
    effective_until: Optional[datetime]
    revoked_at: Optional[datetime]
    revocation_recorded_at: Optional[datetime]
    build: Optional[str]
    prereleases_allowed: bool


@dataclass(frozen=True, slots=True)
class AdmissionDecision:
    """The result of an admission evaluation with its full clause trace."""

    admitted: bool
    artifact_source: str
    artifact_version: str
    decision_at: datetime
    knowledge_at: datetime
    clause_id: Optional[str]
    trace: Tuple[TraceEntry, ...] = field(default_factory=tuple)

    @property
    def reasons(self) -> Tuple[str, ...]:
        """Machine readable reasons of every skipped clause."""
        return tuple(
            entry.reason for entry in self.trace if entry.reason is not None
        )

    @property
    def applied_entry(self) -> Optional[TraceEntry]:
        """The trace entry of the clause that admitted the artifact, if any."""
        for entry in self.trace:
            if entry.status == STATUS_APPLIED:
                return entry
        return None

    def explain(self) -> str:
        """Return a human readable rendering of the decision and its trace."""
        verdict = "ADMITTED" if self.admitted else "DENIED"
        lines = [
            f"{verdict} {self.artifact_source}:{self.artifact_version} "
            f"decided at {self.decision_at.isoformat()} with records known "
            f"up to {self.knowledge_at.isoformat()}"
        ]
        if self.clause_id is not None:
            lines.append(f"applied clause: {self.clause_id}")
        for entry in self.trace:
            if entry.status == STATUS_APPLIED:
                lines.append(f"- {entry.clause_id}: applied ({entry.detail})")
            else:
                lines.append(
                    f"- {entry.clause_id}: skipped ({entry.reason}; {entry.detail})"
                )
        return "\n".join(lines)


class ReleaseGate:
    """An immutable policy snapshot evaluated against artifacts.

    The gate holds a frozen set of :class:`ReleaseExemption` records. It never
    gains or loses clauses after construction; historical views are produced
    by filtering those fixed records according to what was knowable at the
    view moment.

    :param exemptions: the clause records making up the policy
    :param now_factory: callable returning the current timezone aware
        moment, used when an evaluation does not pin ``knowledge_at``
    """

    def __init__(
        self,
        exemptions: Tuple[ReleaseExemption, ...],
        now_factory: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        clauses = tuple(exemptions)
        for clause in clauses:
            if not isinstance(clause, ReleaseExemption):
                raise TypeError("all gate records must be ReleaseExemption instances")
        ids = [clause.id for clause in clauses]
        if len(ids) != len(set(ids)):
            raise ValueError("exemption ids must be unique within a gate")
        self._exemptions: Tuple[ReleaseExemption, ...] = tuple(
            sorted(clauses, key=lambda clause: (clause.effective_from, clause.id))
        )
        self._now_factory = now_factory

    @classmethod
    def from_mappings(
        cls,
        records: Tuple[dict, ...],
        now_factory: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> "ReleaseGate":
        """Build a gate from JSON-like mapping records.

        Datetime fields may be given as :class:`~datetime.datetime` values or
        ISO 8601 strings; naive strings are rejected so a record cannot
        silently pick up the host timezone.

        :param records: mappings accepted by :class:`ReleaseExemption`
        :param now_factory: optional clock used for unpinned evaluations
        """
        date_fields = (
            "effective_from",
            "effective_until",
            "revoked_at",
            "revocation_recorded_at",
            "known_at",
        )
        exemptions = []
        for record in records:
            parsed = dict(record)
            for field_name in date_fields:
                value = parsed.get(field_name)
                if isinstance(value, str):
                    moment = datetime.fromisoformat(value)
                    _require_aware(moment, field_name)
                    parsed[field_name] = moment
            exemptions.append(ReleaseExemption(**parsed))
        return cls(tuple(exemptions), now_factory=now_factory)

    @property
    def exemptions(self) -> Tuple[ReleaseExemption, ...]:
        """The frozen clause records of the gate."""
        return self._exemptions

    def evaluate(
        self,
        artifact: Artifact,
        decision_at: Optional[datetime] = None,
        knowledge_at: Optional[datetime] = None,
    ) -> AdmissionDecision:
        """Evaluate an artifact against the policy snapshot.

        The two timestamps are independent axes of a historical view:

        * ``decision_at`` is the moment the rule is evaluated at, typically
          the upload moment. Validity windows and revocation effective times
          are tested against it.
        * ``knowledge_at`` is the cutoff for what records may be used. A
          clause backfilled after the cutoff, and a revocation record entered
          after the cutoff, are invisible even when they are backdated.

        Both default to the gate clock, which answers the question with the
        records completed today. To decide an upload using only what was
        knowable then, pass the upload moment for both timestamps (or use
        :meth:`reconstruct`).

        :param artifact: the artifact requesting admission
        :param decision_at: the moment the rule is evaluated at; defaults to
            the gate clock
        :param knowledge_at: the record visibility cutoff; defaults to
            ``decision_at``
        :return: an :class:`AdmissionDecision` whose trace names the clause in
            force for an admission or the reason for every skipped clause
        """
        if not isinstance(artifact, Artifact):
            raise TypeError("artifact must be an Artifact instance")
        now = self._now_factory()
        if decision_at is None:
            decision_at = now
        _require_aware(decision_at, "decision_at")
        if knowledge_at is None:
            knowledge_at = decision_at
        _require_aware(knowledge_at, "knowledge_at")

        trace = []
        clause_id = None
        for clause in self._exemptions:
            clause_known_at = clause.known_at
            # __post_init__ normalizes known_at to a non-None value.
            assert clause_known_at is not None
            if clause_known_at > knowledge_at:
                # The clause itself was backfilled later than the view's
                # knowledge cutoff and did not exist for this decision.
                continue
            entry = self._evaluate_clause(
                clause, artifact, decision_at, knowledge_at
            )
            trace.append(entry)
            if entry.status == STATUS_APPLIED:
                clause_id = clause.id
                break

        return AdmissionDecision(
            admitted=clause_id is not None,
            artifact_source=artifact.source,
            artifact_version=str(artifact.version),
            decision_at=decision_at,
            knowledge_at=knowledge_at,
            clause_id=clause_id,
            trace=tuple(trace),
        )

    def reconstruct(
        self, artifact: Artifact, uploaded_at: datetime
    ) -> AdmissionDecision:
        """Decide an upload using only information knowable at that moment."""
        return self.evaluate(
            artifact, decision_at=uploaded_at, knowledge_at=uploaded_at
        )

    def reevaluate(
        self,
        artifact: Artifact,
        uploaded_at: datetime,
        knowledge_at: Optional[datetime] = None,
    ) -> AdmissionDecision:
        """Re-judge a past upload against records completed since then.

        :param artifact: the originally uploaded artifact
        :param uploaded_at: the moment the upload happened; rules are still
            evaluated as of this moment
        :param knowledge_at: the record visibility cutoff, defaulting to the
            gate clock (today's completed records)
        """
        return self.evaluate(
            artifact,
            decision_at=uploaded_at,
            knowledge_at=knowledge_at
            if knowledge_at is not None
            else self._now_factory(),
        )

    def evaluate_today(self, artifact: Artifact) -> AdmissionDecision:
        """Decide with every record completed up to the gate clock."""
        return self.evaluate(artifact, decision_at=None, knowledge_at=None)

    def _evaluate_clause(
        self,
        clause: ReleaseExemption,
        artifact: Artifact,
        decision_at: datetime,
        knowledge_at: datetime,
    ) -> TraceEntry:
        # A backdated revocation whose record only entered the system after
        # the knowledge cutoff is not knowable in this view; project the
        # clause as it was in force then.
        recorded_at = clause.revocation_recorded_at
        revocation_visible = (
            clause.revoked_at is not None
            and recorded_at is not None
            and recorded_at <= knowledge_at
        )
        visible_revoked_at = clause.revoked_at if revocation_visible else None

        def make_entry(
            status: str, reason: Optional[str], detail: str
        ) -> TraceEntry:
            return TraceEntry(
                clause_id=clause.id,
                source=clause.source,
                status=status,
                reason=reason,
                detail=detail,
                version_range=clause.range.expression,
                effective_from=clause.effective_from,
                effective_until=clause.effective_until,
                revoked_at=visible_revoked_at,
                revocation_recorded_at=recorded_at,
                build=clause.build,
                prereleases_allowed=clause.prereleases_allowed,
            )

        if clause.source != artifact.source:
            return make_entry(
                STATUS_SKIPPED,
                REASON_SOURCE_MISMATCH,
                f"clause issued to {clause.source!r}, artifact source "
                f"{artifact.source!r}",
            )
        if revocation_visible:
            revoked_effective = clause.revoked_at
            assert revoked_effective is not None
            if revoked_effective <= decision_at:
                return make_entry(
                    STATUS_SKIPPED,
                    REASON_REVOKED,
                    f"revoked at {revoked_effective.isoformat()}",
                )
        if not clause.is_active(decision_at):
            if decision_at < clause.effective_from:
                detail = (
                    "not effective until "
                    f"{clause.effective_from.isoformat()}"
                )
            else:
                expired_at = clause.effective_until
                assert expired_at is not None
                detail = f"expired at {expired_at.isoformat()}"
            return make_entry(STATUS_SKIPPED, REASON_OUTSIDE_WINDOW, detail)

        matched, reason = clause.range.membership(
            artifact.version, clause.prereleases_allowed
        )
        if not matched:
            return make_entry(
                STATUS_SKIPPED,
                reason,
                f"version {artifact.version} not admitted by range "
                f"{clause.range.expression!r}",
            )
        if clause.build is not None and artifact.version.build != clause.build:
            return make_entry(
                STATUS_SKIPPED,
                REASON_BUILD_MISMATCH,
                f"clause limited to build {clause.build!r}, artifact build "
                f"{artifact.version.build!r}",
            )

        window_end = (
            clause.effective_until.isoformat()
            if clause.effective_until is not None
            else "open"
        )
        return make_entry(
            STATUS_APPLIED,
            None,
            f"range {clause.range.expression!r} matches and validity window "
            f"{clause.effective_from.isoformat()}..{window_end} covers the "
            "decision moment",
        )
