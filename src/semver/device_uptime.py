"""
Time-based version occupancy from device heartbeat observations.

Heartbeat-count based version shares are biased towards chatty devices:
a device reporting every minute weights the same fleet share as a device
reporting once an hour. The functions in this module attribute *online
duration* to versions instead.

Observations are interval-censored: a device only reports its version at
discrete points in time, so a version change is only known to happen
somewhere between two consecutive observations. For each gap between two
consecutive observations of the same device the set of feasible version
labels is derived from the two endpoint observations:

* equal known version ``A`` at both ends -> the whole gap is fixed to ``A``;
* two different known versions ``A`` and ``B`` -> the gap is a free choice
  between ``A`` and ``B`` (an upgrade, or a filed rollback, may happen at
  any instant inside the gap; the new version is not assumed to last until
  the end of the gap, and no third version is feasible);
* one known version ``A`` and one missing version -> the gap is a free
  choice between ``A`` and the unknown bucket, it can never be attributed
  to another target version;
* missing version at both ends -> the whole gap is fixed to the unknown
  bucket.

Every gap is evaluated against one self-consistent evolution path defined
by these endpoint rules instead of picking whichever side of a gap favors
a target version. For every version (and for the unknown bucket) the
smallest and the largest duration feasible across all gaps are reported as
lower and upper bounds.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Hashable, Iterable, Optional, Sequence

__all__ = [
    "Observation",
    "VersionDuration",
    "VersionShare",
    "VersionTimeReport",
    "version_time_bounds",
]


@dataclass(frozen=True)
class Observation:
    """
    A single heartbeat observation of one device.

    :param device_id: identifier of the device; observations with the same
        identifier belong to one timeline
    :param timestamp: observation time, either numeric (seconds, epoch
        values, ...) or :class:`datetime.datetime`; only subtraction and
        ordering are required
    :param version: reported version string, or ``None`` (also an empty or
        blank string) when the version could not be observed
    """

    device_id: Hashable
    timestamp: Any
    version: Optional[str]


@dataclass(frozen=True)
class VersionDuration:
    """
    Feasible online-duration interval for one version.

    ``version`` is ``None`` for the unknown-version bucket.
    """

    version: Optional[str]
    lower_bound: Any
    upper_bound: Any


@dataclass(frozen=True)
class VersionShare:
    """
    Feasible share of total online time for one version.

    The bounds are fractions of the total observed online time.
    ``version`` is ``None`` for the unknown-version bucket.
    """

    version: Optional[str]
    lower_bound: float
    upper_bound: float


@dataclass(frozen=True)
class VersionTimeReport:
    """Result of :func:`version_time_bounds`."""

    total_online_time: Any
    durations: tuple[VersionDuration, ...]
    _by_version: dict[Optional[str], VersionDuration]

    def duration_for(self, version: Optional[str]) -> VersionDuration:
        """
        Return the duration bounds for *version*.

        A version never observed (including ``None`` for the unknown bucket
        when it is empty) gets a zero-width range.
        """
        zero = self.total_online_time - self.total_online_time
        return self._by_version.get(
            version, VersionDuration(version, zero, zero)
        )

    def share_for(self, version: Optional[str]) -> VersionShare:
        """
        Return the online-time share bounds for *version*.

        Both bounds are ``0.0`` when no online time was observed at all.
        """
        duration = self.duration_for(version)
        if self.total_online_time == 0:
            return VersionShare(version, 0.0, 0.0)
        return VersionShare(
            version,
            duration.lower_bound / self.total_online_time,
            duration.upper_bound / self.total_online_time,
        )

    @property
    def unknown_duration(self) -> VersionDuration:
        """Duration bounds of the unknown-version bucket."""
        return self.duration_for(None)


def _normalize_version(version: Any) -> Optional[str]:
    if version is None:
        return None
    if isinstance(version, str):
        text = version.strip()
        return text or None
    raise TypeError("version must be a string or None")


def _coerce_observation(raw: Any) -> Observation:
    if isinstance(raw, Observation):
        return Observation(
            raw.device_id, raw.timestamp, _normalize_version(raw.version)
        )
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        if len(raw) != 3:
            raise TypeError(
                "tuple observations must contain "
                "(device_id, timestamp, version)"
            )
        device_id, timestamp, version = raw
        return Observation(device_id, timestamp, _normalize_version(version))
    raise TypeError(
        "observations must be Observation objects or "
        "(device_id, timestamp, version) tuples"
    )


def _feasible_versions(
    left: Optional[str], right: Optional[str]
) -> frozenset[Optional[str]]:
    """
    Return the versions that may occupy a gap.

    The gap is bounded by observations reporting *left* and *right*;
    ``None`` stands for a missing version.
    """
    if left is None and right is None:
        return frozenset((None,))
    if left is None:
        return frozenset((right, None))
    if right is None:
        return frozenset((left, None))
    if left == right:
        return frozenset((left,))
    return frozenset((left, right))


def version_time_bounds(
    observations: Iterable[Any],
) -> VersionTimeReport:
    """
    Compute online-duration bounds per version from heartbeat observations.

    Observations may be :class:`Observation` objects or plain
    ``(device_id, timestamp, version)`` tuples; they do not need to be
    sorted. Timestamps of the same device are ordered internally and each
    pair of consecutive observations spans one gap whose duration is
    ``timestamp[i+1] - timestamp[i]``. Both numeric timestamps and
    :class:`datetime.datetime` timestamps are supported.

    A device with a single observation contributes no online time because a
    duration needs two endpoints. Zero-duration gaps (duplicate timestamps)
    are ignored.

    :return: a :class:`VersionTimeReport` with duration bounds for every
        known version (sorted by version string) followed by the
        unknown-version bucket; use
        :meth:`VersionTimeReport.duration_for` and
        :meth:`VersionTimeReport.share_for` to read the bounds of a target
        version

    >>> report = version_time_bounds([
    ...     Observation("d1", 0, "1.0.0"),
    ...     Observation("d1", 10, "2.0.0"),
    ... ])
    >>> report.total_online_time
    10
    >>> report.duration_for("2.0.0")
    VersionDuration(version='2.0.0', lower_bound=0, upper_bound=10)
    >>> report.duration_for("1.0.0")
    VersionDuration(version='1.0.0', lower_bound=0, upper_bound=10)
    """
    grouped: dict[Hashable, list[Observation]] = {}
    for raw in observations:
        observation = _coerce_observation(raw)
        grouped.setdefault(observation.device_id, []).append(observation)

    lower: dict[Optional[str], Any] = {}
    upper: dict[Optional[str], Any] = {}
    total_online_time: Any = None

    for rows in grouped.values():
        rows.sort(key=lambda row: row.timestamp)
        for previous, current in zip(rows, rows[1:]):
            duration = current.timestamp - previous.timestamp
            if duration == 0:
                continue
            zero = duration - duration
            feasible = _feasible_versions(previous.version, current.version)
            if len(feasible) == 1:
                label = next(iter(feasible))
                lower[label] = lower.get(label, zero) + duration
                upper[label] = upper.get(label, zero) + duration
            else:
                for label in feasible:
                    if label not in lower:
                        lower[label] = zero
                    upper[label] = upper.get(label, zero) + duration
            if total_online_time is None:
                total_online_time = duration
            else:
                total_online_time = total_online_time + duration

    if total_online_time is None:
        total_online_time = 0

    known_versions = sorted(label for label in lower if label is not None)
    ordered_labels = known_versions + ([None] if None in lower else [])
    durations = tuple(
        VersionDuration(label, lower[label], upper[label])
        for label in ordered_labels
    )
    by_version = {item.version: item for item in durations}
    return VersionTimeReport(total_online_time, durations, by_version)
