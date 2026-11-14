"""
Cross-specification, implementation-agnostic JSON referencing.
"""

from referencing._core import Anchor, Registry, Resource, Specification
from referencing.migration import (
    Match,
    MigrationReport,
    PointerMigrator,
    Status,
)

__all__ = [
    "Anchor",
    "Match",
    "MigrationReport",
    "PointerMigrator",
    "Registry",
    "Resource",
    "Specification",
    "Status",
]
