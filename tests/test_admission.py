"""Tests for the historical admission gate and its clause records."""

from datetime import datetime, timedelta, timezone

import pytest

from semver import (
    AdmissionDecision,
    Artifact,
    ReleaseExemption,
    ReleaseGate,
    Version,
    VersionRange,
)
from semver.admission import (
    REASON_BUILD_MISMATCH,
    REASON_OUTSIDE_WINDOW,
    REASON_OUT_OF_RANGE,
    REASON_PRERELEASE_NOT_ALLOWED,
    REASON_REVOKED,
    REASON_SOURCE_MISMATCH,
    STATUS_APPLIED,
    STATUS_SKIPPED,
)

UTC = timezone.utc


def dt(year, month, day, hour=12):
    return datetime(year, month, day, hour, tzinfo=UTC)


UPLOAD = dt(2026, 11, 2)
TODAY = dt(2026, 11, 14)


def make_clause(**overrides):
    defaults = {
        "id": "EX-1",
        "source": "partner-a",
        "version_range": ">=1.2.0 <2.0.0",
        "effective_from": dt(2026, 11, 1),
        "effective_until": dt(2026, 11, 6),
    }
    defaults.update(overrides)
    return ReleaseExemption(**defaults)


@pytest.fixture
def stable_artifact():
    return Artifact(source="partner-a", version=Version.parse("1.4.2"))


class TestVersionRange:
    def test_membership_inside_and_outside(self):
        rng = VersionRange(">=1.2.0 <2.0.0")
        assert rng.contains(Version.parse("1.4.2")) is True
        assert rng.contains(Version.parse("2.0.0")) is False
        assert rng.contains(Version.parse("1.1.9")) is False

    def test_all_comparators_must_match(self):
        rng = VersionRange(">=1.0.0 <2.0.0 !=1.5.0")
        assert rng.contains(Version.parse("1.4.0")) is True
        assert rng.contains(Version.parse("1.5.0")) is False

    def test_prerelease_is_not_promoted_by_numeric_membership(self):
        rng = VersionRange(">=1.0.0 <2.0.0")
        prerelease = Version.parse("1.4.2-rc.1")
        matched, reason = rng.membership(prerelease, allow_prerelease=False)
        assert matched is False
        assert reason == REASON_PRERELEASE_NOT_ALLOWED
        assert rng.contains(prerelease) is False

    def test_prerelease_admitted_only_when_explicitly_allowed(self):
        rng = VersionRange(">=1.0.0 <2.0.0")
        prerelease = Version.parse("1.4.2-rc.1")
        assert rng.contains(prerelease, allow_prerelease=True) is True
        # A prerelease of the boundary release is still below that release,
        # hence inside the range; only a genuinely higher core is outside.
        outside = Version.parse("3.0.0-rc.1")
        assert rng.contains(outside, allow_prerelease=True) is False

    def test_prerelease_numeric_core_inside_range_still_denied(self):
        rng = VersionRange(">=1.2.0 <2.0.0")
        decision_version = Version.parse("1.9.9-beta.7")
        assert rng.contains(decision_version) is False

    @pytest.mark.parametrize("expression", ["", "   ", "1.2.0", "~1.2.0", ">=nope"])
    def test_invalid_expression_raises(self, expression):
        with pytest.raises(ValueError):
            VersionRange(expression)

    def test_equality_and_repr(self):
        assert VersionRange(">=1.0.0 <2.0.0") == VersionRange(">=1.0.0 <2.0.0")
        assert ">=1.0.0" in repr(VersionRange(">=1.0.0 <2.0.0"))


class TestReleaseExemption:
    def test_string_range_is_parsed(self):
        clause = make_clause()
        assert isinstance(clause.range, VersionRange)
        assert clause.range.expression == ">=1.2.0 <2.0.0"

    def test_naive_datetime_rejected(self):
        naive = datetime(2026, 11, 1)
        with pytest.raises(ValueError):
            make_clause(effective_from=naive)
        with pytest.raises(ValueError):
            make_clause(effective_until=naive)
        with pytest.raises(ValueError):
            make_clause(revoked_at=naive)

    def test_window_end_must_be_after_start(self):
        with pytest.raises(ValueError):
            make_clause(effective_until=dt(2026, 10, 31))

    def test_known_at_defaults_to_effective_from(self):
        clause = make_clause()
        assert clause.known_at == clause.effective_from

    def test_validity_window_boundaries(self):
        clause = make_clause()
        assert clause.is_active(dt(2026, 11, 1)) is True
        assert clause.is_active(dt(2026, 11, 5, 23)) is True
        # effective_until is exclusive
        assert clause.is_active(dt(2026, 11, 6)) is False
        assert clause.is_active(dt(2026, 10, 31)) is False

    def test_revocation_is_inclusive_and_derived_not_mutated(self):
        clause = make_clause()
        assert clause.is_revoked(UPLOAD) is False
        revoked = clause.with_revocation(dt(2026, 11, 3))
        assert clause.revoked_at is None
        assert revoked.is_revoked(dt(2026, 11, 3)) is True
        assert revoked.is_revoked(dt(2026, 11, 2, 23)) is False

    def test_records_are_frozen(self):
        clause = make_clause()
        with pytest.raises(AttributeError):
            clause.source = "partner-b"  # type: ignore[misc]


class TestReleaseGate:
    def test_admission_names_the_clause_in_force(self, stable_artifact):
        gate = ReleaseGate((make_clause(),))
        decision = gate.reconstruct(stable_artifact, UPLOAD)
        assert isinstance(decision, AdmissionDecision)
        assert decision.admitted is True
        assert decision.clause_id == "EX-1"
        applied = decision.applied_entry
        assert applied is not None
        assert applied.status == STATUS_APPLIED
        assert applied.clause_id == "EX-1"

    def test_denied_after_validity_window(self, stable_artifact):
        gate = ReleaseGate((make_clause(),))
        decision = gate.reconstruct(stable_artifact, dt(2026, 11, 9))
        assert decision.admitted is False
        assert decision.clause_id is None
        assert decision.applied_entry is None
        assert decision.trace[0].reason == REASON_OUTSIDE_WINDOW

    def test_approval_lasting_a_few_business_days_uses_exclusive_end(self):
        clause = make_clause(
            effective_from=dt(2026, 11, 2),
            effective_until=dt(2026, 11, 5),
        )
        gate = ReleaseGate((clause,))
        artifact = Artifact("partner-a", Version.parse("1.4.2"))
        assert gate.reconstruct(artifact, dt(2026, 11, 4)).admitted is True
        assert gate.reconstruct(artifact, dt(2026, 11, 5)).admitted is False

    def test_clause_is_source_specific(self):
        gate = ReleaseGate((make_clause(),))
        other = Artifact(source="partner-b", version=Version.parse("1.4.2"))
        decision = gate.reconstruct(other, UPLOAD)
        assert decision.admitted is False
        assert decision.reasons == (REASON_SOURCE_MISMATCH,)
        assert "partner-a" in decision.trace[0].detail

    def test_version_outside_range_is_denied(self):
        gate = ReleaseGate((make_clause(),))
        artifact = Artifact("partner-a", Version.parse("2.1.0"))
        decision = gate.reconstruct(artifact, UPLOAD)
        assert decision.admitted is False
        assert decision.reasons == (REASON_OUT_OF_RANGE,)

    def test_prerelease_not_admitted_just_because_number_is_in_range(self):
        gate = ReleaseGate((make_clause(),))
        artifact = Artifact("partner-a", Version.parse("1.4.2-rc.3"))
        decision = gate.reconstruct(artifact, UPLOAD)
        assert decision.admitted is False
        assert decision.reasons == (REASON_PRERELEASE_NOT_ALLOWED,)

    def test_prerelease_requires_dedicated_permission(self):
        gate = ReleaseGate((make_clause(prereleases_allowed=True),))
        artifact = Artifact("partner-a", Version.parse("1.4.2-rc.3"))
        decision = gate.reconstruct(artifact, UPLOAD)
        assert decision.admitted is True
        assert decision.clause_id == "EX-1"

    def test_build_scoped_clause_does_not_admit_other_artifacts(self):
        clause = make_clause(build="build.42")
        gate = ReleaseGate((clause,))
        same_priority = Artifact("partner-a", Version.parse("1.4.2+build.99"))
        decision = gate.reconstruct(same_priority, UPLOAD)
        assert decision.admitted is False
        assert decision.reasons == (REASON_BUILD_MISMATCH,)

        no_build = Artifact("partner-a", Version.parse("1.4.2"))
        assert gate.reconstruct(no_build, UPLOAD).reasons == (REASON_BUILD_MISMATCH,)

        matching = Artifact("partner-a", Version.parse("1.4.2+build.42"))
        admitted = gate.reconstruct(matching, UPLOAD)
        assert admitted.admitted is True
        assert admitted.clause_id == "EX-1"

    def test_backdated_revocation_differs_between_the_two_views(self):
        # The revocation takes effect on 2026-11-01 (before the upload) but
        # its record only enters the system on 2026-11-10: it is invisible in
        # the upload-time view yet decisive in today's completed view.
        clause = make_clause(
            revoked_at=dt(2026, 11, 1),
            revocation_recorded_at=dt(2026, 11, 10),
        )
        gate = ReleaseGate((clause,), now_factory=lambda: TODAY)
        artifact = Artifact("partner-a", Version.parse("1.4.2"))

        at_upload = gate.reconstruct(artifact, UPLOAD)
        assert at_upload.admitted is True
        assert at_upload.clause_id == "EX-1"
        applied = at_upload.applied_entry
        assert applied is not None
        assert applied.revoked_at is None
        assert applied.revocation_recorded_at == dt(2026, 11, 10)

        today = gate.evaluate_today(artifact)
        assert today.admitted is False
        assert today.reasons == (REASON_REVOKED,)
        assert today.trace[0].revoked_at == dt(2026, 11, 1)
        assert today.trace[0].revocation_recorded_at == dt(2026, 11, 10)

    def test_recorded_at_without_revoked_at_is_rejected(self):
        with pytest.raises(ValueError):
            make_clause(revocation_recorded_at=dt(2026, 11, 10))

    def test_reevaluate_with_completed_records_flips_the_verdict(self):
        clause = make_clause(
            revoked_at=dt(2026, 11, 1),
            revocation_recorded_at=dt(2026, 11, 10),
        )
        gate = ReleaseGate((clause,), now_factory=lambda: TODAY)
        artifact = Artifact("partner-a", Version.parse("1.4.2"))

        at_upload = gate.reconstruct(artifact, UPLOAD)
        assert at_upload.admitted is True

        reviewed = gate.reevaluate(artifact, UPLOAD)
        assert reviewed.decision_at == UPLOAD
        assert reviewed.knowledge_at == TODAY
        assert reviewed.admitted is False
        assert reviewed.reasons == (REASON_REVOKED,)

        # Pinning the knowledge cutoff before the revocation was recorded
        # reproduces the original verdict even when re-judging later.
        cutoff = gate.reevaluate(
            artifact, UPLOAD, knowledge_at=dt(2026, 11, 5)
        )
        assert cutoff.admitted is True

    def test_revocation_recorded_after_upload_blocks_only_today_view(self):
        original = make_clause()
        revoked = original.with_revocation(dt(2026, 11, 9))
        gate = ReleaseGate((revoked,), now_factory=lambda: TODAY)
        artifact = Artifact("partner-a", Version.parse("1.4.2"))

        assert gate.reconstruct(artifact, UPLOAD).admitted is True
        assert gate.evaluate_today(artifact).admitted is False

    def test_backfilled_clause_invisible_at_upload_visible_today(self):
        # Record created on 2026-11-10 covering an upload that happened on
        # 2026-11-02; the upload-time view must not use later knowledge.
        clause = make_clause(known_at=dt(2026, 11, 10))
        gate = ReleaseGate((clause,), now_factory=lambda: TODAY)
        artifact = Artifact("partner-a", Version.parse("1.4.2"))

        at_upload = gate.reconstruct(artifact, UPLOAD)
        assert at_upload.admitted is False
        assert at_upload.trace == ()

        # Today's records re-judge the same historical upload: the rule is
        # still evaluated as of the upload moment, but the backfilled clause
        # is now visible.
        today = gate.reevaluate(artifact, UPLOAD)
        assert today.decision_at == UPLOAD
        assert today.knowledge_at == TODAY
        assert today.admitted is True
        assert today.applied_entry is not None

    def test_trace_covers_every_visible_skipped_clause(self):
        clauses = (
            make_clause(id="EX-wrong-source", source="partner-b"),
            make_clause(id="EX-expired", effective_until=dt(2026, 11, 2)),
            make_clause(id="EX-good", effective_from=dt(2026, 11, 2)),
        )
        gate = ReleaseGate(clauses)
        artifact = Artifact("partner-a", Version.parse("1.4.2"))
        decision = gate.reconstruct(artifact, UPLOAD)
        assert decision.admitted is True
        assert decision.clause_id == "EX-good"
        assert [entry.clause_id for entry in decision.trace] == [
            "EX-expired",
            "EX-wrong-source",
            "EX-good",
        ]
        assert all(entry.status == STATUS_SKIPPED for entry in decision.trace[:2])
        assert decision.trace[-1].status == STATUS_APPLIED
        assert decision.trace[0].version_range == ">=1.2.0 <2.0.0"
        expired = next(
            entry for entry in decision.trace if entry.clause_id == "EX-expired"
        )
        assert expired.reason == REASON_OUTSIDE_WINDOW
        wrong_source = next(
            entry for entry in decision.trace if entry.clause_id == "EX-wrong-source"
        )
        assert wrong_source.reason == REASON_SOURCE_MISMATCH

    def test_first_matching_clause_short_circuits_remaining_trace(self):
        clauses = (
            make_clause(id="EX-first"),
            make_clause(id="EX-second"),
        )
        gate = ReleaseGate(clauses)
        decision = gate.reconstruct(
            Artifact("partner-a", Version.parse("1.4.2")), UPLOAD
        )
        assert decision.clause_id == "EX-first"
        assert [entry.clause_id for entry in decision.trace] == ["EX-first"]

    def test_duplicate_clause_ids_rejected(self):
        with pytest.raises(ValueError):
            ReleaseGate((make_clause(), make_clause()))

    def test_gate_rejects_non_exemption_records(self):
        with pytest.raises(TypeError):
            ReleaseGate(("not-a-clause",))  # type: ignore[arg-type]

    def test_evaluate_requires_aware_moment(self, stable_artifact):
        gate = ReleaseGate((make_clause(),))
        with pytest.raises(ValueError):
            gate.evaluate(stable_artifact, datetime(2026, 11, 2))

    def test_from_mappings_builds_gate(self):
        records = (
            {
                "id": "EX-json",
                "source": "partner-a",
                "version_range": ">=1.0.0",
                "effective_from": dt(2026, 11, 1),
            },
        )
        gate = ReleaseGate.from_mappings(records)
        decision = gate.reconstruct(
            Artifact("partner-a", Version.parse("9.9.9")), UPLOAD
        )
        assert decision.admitted is True
        assert decision.clause_id == "EX-json"

    def test_from_mappings_accepts_iso8601_and_rejects_naive_strings(self):
        gate = ReleaseGate.from_mappings(
            (
                {
                    "id": "EX-iso",
                    "source": "partner-a",
                    "version_range": ">=1.0.0 <2.0.0",
                    "effective_from": "2026-11-01T00:00:00+00:00",
                    "effective_until": "2026-11-06T00:00:00+00:00",
                },
            )
        )
        decision = gate.reconstruct(
            Artifact("partner-a", Version.parse("1.4.2")), UPLOAD
        )
        assert decision.admitted is True

        with pytest.raises(ValueError):
            ReleaseGate.from_mappings(
                (
                    {
                        "id": "EX-naive",
                        "source": "partner-a",
                        "version_range": ">=1.0.0",
                        "effective_from": "2026-11-01T00:00:00",
                    },
                )
            )

    def test_explain_is_auditable(self, stable_artifact):
        gate = ReleaseGate((make_clause(),))
        text = gate.reconstruct(stable_artifact, UPLOAD).explain()
        assert "ADMITTED" in text
        assert "EX-1" in text
        denied = gate.reconstruct(
            Artifact("partner-b", Version.parse("1.4.2")), UPLOAD
        ).explain()
        assert "DENIED" in denied
        assert REASON_SOURCE_MISMATCH in denied

    def test_open_ended_window_stays_active(self):
        clause = make_clause(effective_until=None)
        gate = ReleaseGate((clause,))
        decision = gate.reconstruct(
            Artifact("partner-a", Version.parse("1.4.2")),
            dt(2030, 1, 1),
        )
        assert decision.admitted is True

    def test_subclassed_version_remains_comparable(self):
        gate = ReleaseGate((make_clause(),))
        version = Version.parse("1.4.2+sha.abc")
        artifact = Artifact("partner-a", version)
        decision = gate.reconstruct(artifact, UPLOAD)
        assert decision.artifact_version == "1.4.2+sha.abc"
        assert decision.admitted is True

    def test_timedelta_based_business_day_window(self):
        start = dt(2026, 11, 2)
        clause = make_clause(
            effective_from=start,
            effective_until=start + timedelta(days=3),
        )
        gate = ReleaseGate((clause,))
        artifact = Artifact("partner-a", Version.parse("1.2.0"))
        assert gate.reconstruct(artifact, start + timedelta(days=2)).admitted is True
        assert gate.reconstruct(artifact, start + timedelta(days=3)).admitted is False
