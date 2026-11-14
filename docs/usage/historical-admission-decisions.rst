Historical Admission Decisions
==============================

.. meta::
   :description lang=en:
      Decide whether a test format upload may count as a formal batch using
      time limited, source specific exemption clauses

A partner is occasionally allowed to upload a formal batch through the test
pipeline. Such an approval is not a standing rule: it is issued to one
partner, limited to a semantic version range, valid for a few business days
only, and may be revoked, sometimes retroactively. The
:mod:`semver.admission` module keeps those approvals as immutable records and
answers the same question from two historical points of view.

The building blocks are:

* :class:`~semver.admission.VersionRange` -- an AND-ed comparator range such
  as ``">=1.2.0 <2.0.0"``.
* :class:`~semver.admission.ReleaseExemption` -- one approval clause, bound to
  a source, a range, a validity window, and optional build, prerelease and
  revocation metadata.
* :class:`~semver.admission.ReleaseGate` -- a frozen set of clauses that
  evaluates an :class:`~semver.admission.Artifact`.
* :class:`~semver.admission.AdmissionDecision` -- the verdict plus a full
  :class:`~semver.admission.TraceEntry` for every clause considered.

.. currentmodule:: semver.admission

Define the range and the clause
-------------------------------

A prerelease is never a member of a range merely because its numeric core
falls inside it; prerelease admission must be enabled per clause:

.. code-block:: python

    >>> from datetime import datetime, timezone
    >>> from semver import Version
    >>> from semver.admission import VersionRange
    >>> rng = VersionRange(">=1.2.0 <2.0.0")
    >>> rng.contains(Version.parse("1.4.2"))
    True
    >>> rng.contains(Version.parse("1.4.2-rc.1"))
    False

A clause is issued to a single source and has an inclusive start and an
exclusive end to its validity window:

.. code-block:: python

    >>> from semver.admission import ReleaseExemption
    >>> utc = timezone.utc
    >>> clause = ReleaseExemption(
    ...     id="EX-2026-001",
    ...     source="partner-a",
    ...     version_range=">=1.2.0 <2.0.0",
    ...     effective_from=datetime(2026, 11, 1, tzinfo=utc),
    ...     effective_until=datetime(2026, 11, 6, tzinfo=utc),
    ... )

Build scoped and prerelease scoped approvals
--------------------------------------------

A clause may name a single build identifier. It then admits only artifacts
carrying that build; it never releases other artifacts of the same priority:

.. code-block:: python

    >>> build_clause = ReleaseExemption(
    ...     id="EX-2026-002",
    ...     source="partner-a",
    ...     version_range=">=1.2.0 <2.0.0",
    ...     effective_from=datetime(2026, 11, 1, tzinfo=utc),
    ...     build="build.42",
    ... )

Prereleases require an explicit flag. A clause that allows prereleases still
applies its range, so a prerelease outside the range stays denied:

.. code-block:: python

    >>> pre_clause = ReleaseExemption(
    ...     id="EX-2026-003",
    ...     source="partner-a",
    ...     version_range=">=1.2.0 <2.0.0",
    ...     effective_from=datetime(2026, 11, 1, tzinfo=utc),
    ...     prereleases_allowed=True,
    ... )

Decide with the information available at upload time
----------------------------------------------------

:meth:`ReleaseGate.reconstruct` evaluates the rule as of the upload moment
using only records that were knowable then. Clauses backfilled later and
revocations recorded later are invisible:

.. code-block:: python

    >>> from semver.admission import Artifact, ReleaseGate
    >>> gate = ReleaseGate((clause, build_clause, pre_clause))
    >>> artifact = Artifact("partner-a", Version.parse("1.4.2"))
    >>> upload = datetime(2026, 11, 2, 12, tzinfo=utc)
    >>> decision = gate.reconstruct(artifact, upload)
    >>> decision.admitted
    True
    >>> decision.clause_id
    'EX-2026-001'

Re-judge with records completed today
-------------------------------------

The decision moment and the knowledge cutoff are separate axes.
:meth:`ReleaseGate.reevaluate` keeps evaluating the rule as of the upload
moment but allows records entered into the system since then. A backdated
revocation therefore changes the modern verdict without rewriting the
historical one:

.. code-block:: python

    >>> revoked = clause.with_revocation(
    ...     revoked_at=datetime(2026, 11, 1, tzinfo=utc),
    ...     recorded_at=datetime(2026, 11, 10, tzinfo=utc),
    ... )
    >>> today_gate = ReleaseGate(
    ...     (revoked,),
    ...     now_factory=lambda: datetime(2026, 11, 14, 12, tzinfo=utc),
    ... )
    >>> today_gate.reconstruct(artifact, upload).admitted
    True
    >>> reviewed = today_gate.reevaluate(artifact, upload)
    >>> reviewed.admitted
    False

Every verdict is auditable
--------------------------

A denial lists a machine readable reason for each skipped clause, and an
admission names the single clause that was applied. Use
:attr:`~semver.admission.AdmissionDecision.trace` or
:meth:`~semver.admission.AdmissionDecision.explain` to inspect the exact
clause in force:

.. code-block:: python

    >>> reviewed.reasons
    ('revoked',)
    >>> other = Artifact("partner-b", Version.parse("1.4.2"))
    >>> gate.reconstruct(other, upload).reasons
    ('source_mismatch', 'source_mismatch', 'source_mismatch')
    >>> build_gate = ReleaseGate((build_clause,))
    >>> wrong_build = Artifact("partner-a", Version.parse("1.4.2+build.99"))
    >>> build_gate.reconstruct(wrong_build, upload).reasons
    ('build_mismatch',)
    >>> print(gate.reconstruct(other, upload).explain())  # doctest: +ELLIPSIS
    DENIED partner-b:1.4.2 decided at ... with records known up to ...
    - EX-2026-001: skipped (source_mismatch; ...)
    ...
