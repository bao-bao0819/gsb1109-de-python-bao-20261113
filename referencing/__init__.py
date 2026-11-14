"""
Cross-specification, implementation-agnostic JSON referencing.
"""

from referencing._core import Anchor, Registry, Resource, Specification
from referencing.policy import Policy

__all__ = ["Anchor", "Policy", "Registry", "Resource", "Specification"]
