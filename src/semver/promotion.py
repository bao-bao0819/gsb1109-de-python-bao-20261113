"""
Promotion registry for moving artifacts through release channels.

Artifacts of one and the same format travel through the ordered channels
``experimental`` -> ``candidate`` -> ``release``.  A candidate line may
consist of several builds: they share the same release number (the version
without its prerelease segment, see :meth:`Version.finalize_version`) but
their content, and therefore their fingerprint, may differ.

The registry records every handoff between channels and enforces two
invariants:

* the fingerprint of the artifact presented for promotion must match the
  fingerprint registered in the source channel;
* every single-step transition must be covered by an approval that was
  issued for exactly that artifact (release number *and* fingerprint) and
  exactly that transition.

Three recurring situations are told apart explicitly:

* redelivery of an already recorded receipt is idempotent and reported as
  :attr:`PromotionStatus.DUPLICATE_RECEIPT`;
* a swapped, re-signed package (same release number, new fingerprint)
  invalidates the approvals of its predecessor and is rejected with
  :class:`FingerprintMismatchError` until it is registered and approved
  on its own;
* jumping over an intermediate channel without its approval is rejected
  with :class:`SkippedApprovalError`.

Messages that arrive late from a channel the artifact has already left
never move it back: they are acknowledged as
:attr:`PromotionStatus.STALE_MESSAGE` and the recorded state stays
untouched.
"""

from dataclasses import dataclass
from enum import Enum, IntEnum
from typing import Dict, List, Optional, Tuple

from .version import Version


class Channel(IntEnum):
    """The ordered channels an artifact travels through."""

    EXPERIMENTAL = 1
    CANDIDATE = 2
    RELEASE = 3

    def next(self) -> "Channel":
        """
        Return the channel that directly follows this one.

        :raises ValueError: if this is already the last channel
        """
        try:
            return Channel(self + 1)
        except ValueError:
            raise ValueError("%s has no successor channel" % self.name)


class PromotionStatus(Enum):
    """How a promotion request was settled."""

    PROMOTED = "promoted"
    DUPLICATE_RECEIPT = "duplicate-receipt"
    STALE_MESSAGE = "stale-message"


class PromotionError(Exception):
    """Base class for all promotion registry failures."""


class UnknownArtifactError(PromotionError):
    """The artifact was never registered in any channel."""


class FingerprintMismatchError(PromotionError):
    """
    The release number is known but the fingerprint is not.

    This is the signature of a swapped, re-signed package: the previous
    approvals were issued for different content and do not carry over.
    """


class SkippedApprovalError(PromotionError):
    """The promotion jumps over an intermediate channel and its approval."""


class MissingApprovalError(PromotionError):
    """No approval covers this artifact and this transition."""


@dataclass(frozen=True)
class Artifact:
    """
    An immutable artifact instance: a concrete version plus the fingerprint
    of its content.

    Builds that share the release number but differ in content differ in
    ``fingerprint`` and are tracked as separate instances.
    """

    version: Version
    fingerprint: str

    @property
    def release(self) -> Version:
        """The release number: the version without prerelease and build."""
        return self.version.finalize_version()


@dataclass(frozen=True)
class Approval:
    """
    An approval for exactly one transition of exactly one artifact instance.

    Because it is bound to the fingerprint, re-signing a package (new
    fingerprint, same release number) requires a fresh approval.
    """

    release: Version
    fingerprint: str
    from_channel: Channel
    to_channel: Channel
    approver: str
    approval_id: str


@dataclass(frozen=True)
class Receipt:
    """
    Evidence of one recorded handoff.

    ``from_channel`` is ``None`` for the initial intake into the first
    channel.  ``sequence`` orders all receipts of a registry in the order
    they were recorded.
    """

    receipt_id: str
    artifact: Artifact
    from_channel: Optional[Channel]
    to_channel: Channel
    approvals: Tuple[Approval, ...]
    sequence: int


@dataclass(frozen=True)
class PromotionOutcome:
    """
    The result of an :meth:`PromotionRegistry.intake` or
    :meth:`PromotionRegistry.promote` call.

    ``status`` tells apart a real transition, the redelivery of an already
    recorded receipt, and a late message from a channel the artifact has
    already left.  ``receipt`` is the receipt that represents the current
    state (the newly recorded one for :attr:`PromotionStatus.PROMOTED`,
    the previously recorded one otherwise).
    """

    status: PromotionStatus
    receipt: Receipt


def parse_release_tag(tag: str) -> Version:
    """
    Parse a release tag such as ``"v1.4.0"`` or ``"1.4.0"`` into the
    release number it refers to.

    Any prerelease or build part is dropped, so a tag always denotes the
    release line, not one particular build.

    :param tag: the tag to parse, with an optional leading ``v``
    :return: the finalized version the tag refers to
    """
    text = tag.strip()
    if text[:1] in ("v", "V"):
        text = text[1:]
    return Version.parse(text).finalize_version()


@dataclass
class _ArtifactState:
    """Mutable bookkeeping for one artifact instance."""

    artifact: Artifact
    channel: Channel
    receipt: Receipt


class PromotionRegistry:
    """
    Records and validates how artifacts move through the channels.

    The registry is append-only: once a handoff is recorded it is never
    rewritten, and late messages from older channels cannot demote an
    artifact that has already been promoted.
    """

    def __init__(self) -> None:
        self._states: Dict[Tuple[Version, str], _ArtifactState] = {}
        self._approvals: Dict[Tuple[Version, str, Channel, Channel], Approval] = {}
        self._receipts: Dict[str, Receipt] = {}
        self._history: Dict[Version, List[Receipt]] = {}
        self._sequence = 0

    def intake(
        self,
        artifact: Artifact,
        receipt_id: str,
        channel: Channel = Channel.EXPERIMENTAL,
    ) -> PromotionOutcome:
        """
        Register an artifact instance in its first channel.

        Redelivering an already recorded ``receipt_id`` is idempotent.
        A late intake into a channel the instance has already left is
        acknowledged as stale and changes nothing.

        :param artifact: the artifact instance to register
        :param receipt_id: unique id of the intake receipt
        :param channel: the channel the artifact enters, by default
            :attr:`Channel.EXPERIMENTAL`
        :return: the outcome of the request
        """
        known = self._receipts.get(receipt_id)
        if known is not None:
            return PromotionOutcome(PromotionStatus.DUPLICATE_RECEIPT, known)

        key = (artifact.release, artifact.fingerprint)
        state = self._states.get(key)
        if state is not None:
            if channel <= state.channel:
                return PromotionOutcome(PromotionStatus.STALE_MESSAGE, state.receipt)
            raise PromotionError(
                "artifact %s (%s) is already in %s; use promote() to move it"
                % (artifact.version, artifact.fingerprint, state.channel.name)
            )

        receipt = self._record(receipt_id, artifact, None, channel, ())
        return PromotionOutcome(PromotionStatus.PROMOTED, receipt)

    def approve(
        self,
        artifact: Artifact,
        from_channel: Channel,
        to_channel: Channel,
        approver: str,
        approval_id: str,
    ) -> Approval:
        """
        Record an approval for one transition of one artifact instance.

        The approval is bound to the artifact's release number *and*
        fingerprint, so it becomes void when the package is swapped and
        re-signed.

        :param artifact: the artifact instance the approval applies to
        :param from_channel: the channel the transition starts from
        :param to_channel: the channel the transition leads to
        :param approver: who granted the approval
        :param approval_id: unique id of the approval
        :return: the recorded approval
        """
        if to_channel != from_channel.next():
            raise SkippedApprovalError(
                "cannot approve %s -> %s: approvals only cover single steps"
                % (from_channel.name, to_channel.name)
            )
        approval = Approval(
            release=artifact.release,
            fingerprint=artifact.fingerprint,
            from_channel=from_channel,
            to_channel=to_channel,
            approver=approver,
            approval_id=approval_id,
        )
        key = (approval.release, approval.fingerprint, from_channel, to_channel)
        self._approvals[key] = approval
        return approval

    def promote(
        self, artifact: Artifact, to_channel: Channel, receipt_id: str
    ) -> PromotionOutcome:
        """
        Move an artifact instance into the next channel.

        The artifact must currently sit in the channel directly before
        ``to_channel``, its fingerprint must match the registered one, and
        an approval for exactly this instance and transition must exist.

        :param artifact: the artifact instance to promote
        :param to_channel: the channel to move it into
        :param receipt_id: unique id of the promotion receipt
        :return: the outcome of the request
        :raises UnknownArtifactError: if the release number was never
            registered
        :raises FingerprintMismatchError: if the release number is known
            but the fingerprint is not (swapped, re-signed package)
        :raises SkippedApprovalError: if the move jumps over an
            intermediate channel
        :raises MissingApprovalError: if no approval covers this transition
        """
        known = self._receipts.get(receipt_id)
        if known is not None:
            return PromotionOutcome(PromotionStatus.DUPLICATE_RECEIPT, known)

        key = (artifact.release, artifact.fingerprint)
        state = self._states.get(key)
        if state is None:
            if any(release == artifact.release for release, _ in self._states):
                raise FingerprintMismatchError(
                    "release %s is registered with different content; the "
                    "swapped, re-signed package %s needs its own intake and "
                    "approvals" % (artifact.release, artifact.fingerprint)
                )
            raise UnknownArtifactError(
                "release %s (%s) was never registered"
                % (artifact.release, artifact.fingerprint)
            )

        if to_channel <= state.channel:
            return PromotionOutcome(PromotionStatus.STALE_MESSAGE, state.receipt)

        if to_channel != state.channel.next():
            raise SkippedApprovalError(
                "cannot promote %s from %s to %s: the intermediate channel "
                "and its approval may not be skipped"
                % (artifact.version, state.channel.name, to_channel.name)
            )

        approval = self._approvals.get(
            (artifact.release, artifact.fingerprint, state.channel, to_channel)
        )
        if approval is None:
            raise MissingApprovalError(
                "no approval for %s (%s) from %s to %s"
                % (
                    artifact.version,
                    artifact.fingerprint,
                    state.channel.name,
                    to_channel.name,
                )
            )

        receipt = self._record(
            receipt_id, artifact, state.channel, to_channel, (approval,)
        )
        return PromotionOutcome(PromotionStatus.PROMOTED, receipt)

    def channel_of(self, artifact: Artifact) -> Optional[Channel]:
        """
        Return the channel the artifact instance currently sits in, or
        ``None`` if it was never registered.
        """
        state = self._states.get((artifact.release, artifact.fingerprint))
        return state.channel if state is not None else None

    def history(self, tag: str) -> List[Receipt]:
        """
        List every recorded handoff of the release line denoted by ``tag``,
        in the order they happened.

        Each receipt is the evidence of one handoff: it names the channels it
        connects, the artifact instance (version and fingerprint) that was
        moved, and the approvals that authorized the move.

        :param tag: a release tag such as ``"v1.4.0"``
        :return: the handoff receipts, oldest first; empty if the release
            line is unknown
        """
        return list(self._history.get(parse_release_tag(tag), ()))

    def channel_path(self, tag: str) -> List[Channel]:
        """
        List the channels the release line denoted by ``tag`` actually
        passed through, in the order they were entered.
        """
        return [receipt.to_channel for receipt in self.history(tag)]

    def _record(
        self,
        receipt_id: str,
        artifact: Artifact,
        from_channel: Optional[Channel],
        to_channel: Channel,
        approvals: Tuple[Approval, ...],
    ) -> Receipt:
        self._sequence += 1
        receipt = Receipt(
            receipt_id=receipt_id,
            artifact=artifact,
            from_channel=from_channel,
            to_channel=to_channel,
            approvals=approvals,
            sequence=self._sequence,
        )
        self._receipts[receipt_id] = receipt
        self._states[(artifact.release, artifact.fingerprint)] = _ArtifactState(
            artifact=artifact, channel=to_channel, receipt=receipt
        )
        self._history.setdefault(artifact.release, []).append(receipt)
        return receipt
