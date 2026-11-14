import pytest

from semver import Version
from semver.promotion import (
    Artifact,
    Channel,
    FingerprintMismatchError,
    MissingApprovalError,
    PromotionError,
    PromotionRegistry,
    PromotionStatus,
    SkippedApprovalError,
    UnknownArtifactError,
    parse_release_tag,
)


def make_artifact(version, fingerprint):
    return Artifact(Version.parse(version), fingerprint)


@pytest.fixture
def registry():
    return PromotionRegistry()


@pytest.fixture
def candidate_a(registry):
    artifact = make_artifact("1.4.0-rc.1", "fp-a")
    registry.intake(artifact, "intake-a")
    return artifact


def promote_to_release(registry, artifact, tag):
    registry.approve(
        artifact,
        Channel.EXPERIMENTAL,
        Channel.CANDIDATE,
        approver="qa",
        approval_id="ap-exp-cand",
    )
    registry.promote(artifact, Channel.CANDIDATE, "promote-cand-" + tag)
    registry.approve(
        artifact,
        Channel.CANDIDATE,
        Channel.RELEASE,
        approver="release-manager",
        approval_id="ap-cand-rel",
    )
    return registry.promote(artifact, Channel.RELEASE, "promote-rel-" + tag)


def test_happy_path_walks_all_three_channels(registry, candidate_a):
    outcome = promote_to_release(registry, candidate_a, "a")

    assert outcome.status is PromotionStatus.PROMOTED
    assert registry.channel_of(candidate_a) is Channel.RELEASE
    assert registry.channel_path("v1.4.0") == [
        Channel.EXPERIMENTAL,
        Channel.CANDIDATE,
        Channel.RELEASE,
    ]


def test_history_lists_each_handoff_with_evidence(registry, candidate_a):
    promote_to_release(registry, candidate_a, "a")

    history = registry.history("1.4.0")

    assert [r.receipt_id for r in history] == [
        "intake-a",
        "promote-cand-a",
        "promote-rel-a",
    ]
    assert history[0].from_channel is None
    assert history[1].from_channel is Channel.EXPERIMENTAL
    assert history[2].from_channel is Channel.CANDIDATE
    # every promotion receipt carries the approval that authorized it
    assert history[1].approvals[0].approval_id == "ap-exp-cand"
    assert history[1].approvals[0].approver == "qa"
    assert history[2].approvals[0].approval_id == "ap-cand-rel"
    # evidence is ordered
    sequences = [r.sequence for r in history]
    assert sequences == sorted(sequences)


def test_candidate_line_with_several_builds_same_release_number(registry):
    build1 = make_artifact("1.4.0-rc.1", "fp-1")
    build2 = make_artifact("1.4.0-rc.2", "fp-2")

    registry.intake(build1, "intake-1")
    registry.intake(build2, "intake-2")

    # same release number, different content: tracked independently
    assert build1.release == build2.release == Version.parse("1.4.0")
    promote_to_release(registry, build1, "1")
    assert registry.channel_of(build1) is Channel.RELEASE
    assert registry.channel_of(build2) is Channel.EXPERIMENTAL

    # the release tag reports what actually happened, for both builds
    channels = [r.to_channel for r in registry.history("v1.4.0")]
    assert channels == [
        Channel.EXPERIMENTAL,
        Channel.EXPERIMENTAL,
        Channel.CANDIDATE,
        Channel.RELEASE,
    ]


def test_redelivered_receipt_is_idempotent(registry, candidate_a):
    registry.approve(
        candidate_a,
        Channel.EXPERIMENTAL,
        Channel.CANDIDATE,
        approver="qa",
        approval_id="ap-1",
    )
    first = registry.promote(candidate_a, Channel.CANDIDATE, "promote-1")
    second = registry.promote(candidate_a, Channel.CANDIDATE, "promote-1")

    assert first.status is PromotionStatus.PROMOTED
    assert second.status is PromotionStatus.DUPLICATE_RECEIPT
    assert second.receipt is first.receipt
    # no extra handoff was recorded
    assert [r.receipt_id for r in registry.history("1.4.0")] == [
        "intake-a",
        "promote-1",
    ]


def test_swapped_resigned_package_is_rejected(registry, candidate_a):
    swapped = make_artifact("1.4.0-rc.1", "fp-b")
    registry.approve(
        candidate_a,
        Channel.EXPERIMENTAL,
        Channel.CANDIDATE,
        approver="qa",
        approval_id="ap-1",
    )

    with pytest.raises(FingerprintMismatchError):
        registry.promote(swapped, Channel.CANDIDATE, "promote-swapped")


def test_swapped_package_needs_own_intake_and_approvals(registry, candidate_a):
    registry.approve(
        candidate_a,
        Channel.EXPERIMENTAL,
        Channel.CANDIDATE,
        approver="qa",
        approval_id="ap-old",
    )
    swapped = make_artifact("1.4.0-rc.1", "fp-b")
    registry.intake(swapped, "intake-b")

    # the approval for the old fingerprint does not cover the new content
    with pytest.raises(MissingApprovalError):
        registry.promote(swapped, Channel.CANDIDATE, "promote-b")

    registry.approve(
        swapped,
        Channel.EXPERIMENTAL,
        Channel.CANDIDATE,
        approver="qa",
        approval_id="ap-new",
    )
    outcome = registry.promote(swapped, Channel.CANDIDATE, "promote-b")
    assert outcome.status is PromotionStatus.PROMOTED
    assert outcome.receipt.approvals[0].approval_id == "ap-new"


def test_skipping_intermediate_channel_is_rejected(registry, candidate_a):
    with pytest.raises(SkippedApprovalError):
        registry.promote(candidate_a, Channel.RELEASE, "promote-skip")
    assert registry.channel_of(candidate_a) is Channel.EXPERIMENTAL


def test_cannot_approve_a_skipped_transition(registry, candidate_a):
    with pytest.raises(SkippedApprovalError):
        registry.approve(
            candidate_a,
            Channel.EXPERIMENTAL,
            Channel.RELEASE,
            approver="qa",
            approval_id="ap-skip",
        )


def test_promotion_without_approval_is_rejected(registry, candidate_a):
    with pytest.raises(MissingApprovalError):
        registry.promote(candidate_a, Channel.CANDIDATE, "promote-1")


def test_late_message_from_old_channel_does_not_demote(registry, candidate_a):
    promote_to_release(registry, candidate_a, "a")

    # a late intake echo from the experimental channel
    late_intake = registry.intake(candidate_a, "intake-late")
    assert late_intake.status is PromotionStatus.STALE_MESSAGE

    # a late promotion message that only reaches the candidate channel
    late_promote = registry.promote(candidate_a, Channel.CANDIDATE, "promote-late")
    assert late_promote.status is PromotionStatus.STALE_MESSAGE

    assert registry.channel_of(candidate_a) is Channel.RELEASE
    assert registry.channel_path("v1.4.0") == [
        Channel.EXPERIMENTAL,
        Channel.CANDIDATE,
        Channel.RELEASE,
    ]


def test_unknown_artifact_cannot_be_promoted(registry):
    ghost = make_artifact("9.9.9", "fp-ghost")
    with pytest.raises(UnknownArtifactError):
        registry.promote(ghost, Channel.CANDIDATE, "promote-ghost")


def test_redelivered_intake_receipt_is_idempotent(registry):
    artifact = make_artifact("1.4.0-rc.1", "fp-a")
    first = registry.intake(artifact, "intake-a")
    second = registry.intake(artifact, "intake-a")

    assert first.status is PromotionStatus.PROMOTED
    assert second.status is PromotionStatus.DUPLICATE_RECEIPT
    assert second.receipt is first.receipt
    assert len(registry.history("1.4.0")) == 1


def test_registered_artifact_moves_only_via_promote(registry, candidate_a):
    with pytest.raises(PromotionError, match="use promote"):
        registry.intake(candidate_a, "intake-again", channel=Channel.CANDIDATE)


def test_last_channel_has_no_successor():
    with pytest.raises(ValueError, match="no successor"):
        Channel.RELEASE.next()


def test_history_of_unknown_release_is_empty(registry):
    assert registry.history("v0.0.1") == []
    assert registry.channel_path("0.0.1") == []


@pytest.mark.parametrize(
    "tag,expected",
    [
        ("1.4.0", "1.4.0"),
        ("v1.4.0", "1.4.0"),
        ("V1.4.0-rc.2", "1.4.0"),
        ("1.4.0-rc.2+build.5", "1.4.0"),
    ],
)
def test_parse_release_tag(tag, expected):
    assert parse_release_tag(tag) == Version.parse(expected)
