"""Power metering audit toolkit.

Infers whether the two power columns reported per card model represent two
independent rails (both counted) or one sensor published under two names
(counted once), using timed chassis totals, slot readings, meter tolerances
and card insert/remove events.
"""

from poweraudit.engine import (
    ALIAS,
    AMBIGUOUS,
    DUAL,
    EVENT_CHANGE,
    LOAD_CHANGE,
    NO_FIT,
    AuditResult,
    CardSample,
    ChassisSample,
    Event,
    Evidence,
    FitResult,
    Segment,
    alias_consistent,
    audit,
    build_segments,
    card_contribution,
)

__all__ = [
    'ALIAS',
    'AMBIGUOUS',
    'DUAL',
    'EVENT_CHANGE',
    'LOAD_CHANGE',
    'NO_FIT',
    'AuditResult',
    'CardSample',
    'ChassisSample',
    'Event',
    'Evidence',
    'FitResult',
    'Segment',
    'alias_consistent',
    'audit',
    'build_segments',
    'card_contribution',
]
