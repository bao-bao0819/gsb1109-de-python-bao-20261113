"""
Naming-range policies which decide who is authorized to provide a resource.

Each authority (headquarters, a region, a project package, ...) declares the
range of URIs it is responsible for. When registries are combined, these
declarations decide which definition wins for any given URI:

* a more specific (narrower) authorization always overrides a broader one,
  even if the broader one belongs to a hierarchically "higher" authority;
* when two *unrelated* ranges -- neither of which contains the other -- both
  claim the same URI, there is no automatic winner, and the resulting
  `~referencing.exceptions.PendingBlockingConflict` must be handled manually.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from attrs import field
from rpds import HashTrieSet

from referencing._attrs import frozen

if TYPE_CHECKING:
    from collections.abc import Iterable

    from referencing.typing import URI


def _hierarchical(uri: URI) -> bool:
    """
    Whether the given URI supports path-style (hierarchical) containment.

    Hierarchical URIs (``http``, ``https``, ``file``, ... or any URI with an
    authority component) are compared by path prefix, where containment only
    holds at a ``/`` boundary. Other URIs (e.g. ``urn:`` ones) are opaque and
    are compared component by component on their ``:``-separated namespace.
    """
    return bool(urlsplit(uri).netloc)


def _prefix_contains(prefix: URI, uri: URI) -> bool:
    """
    Hierarchical path-prefix containment at path-segment boundaries.
    """
    if not uri.startswith(prefix):
        return False
    if len(uri) == len(prefix):
        return True
    return prefix.endswith("/") or uri[len(prefix)] == "/"


def _opaque_contains(prefix: URI, uri: URI) -> bool:
    """
    Opaque-namespace containment at ``:``-component boundaries.

    For example, ``urn:example:a`` covers ``urn:example:a:b``.
    """
    if not uri.startswith(prefix):
        return False
    if len(uri) == len(prefix):
        return True
    return prefix.endswith(":") or uri[len(prefix)] == ":"


def contains(prefix: URI, uri: URI) -> bool:
    """
    Whether the naming range declared at ``prefix`` covers ``uri``.
    """
    if _hierarchical(prefix) or _hierarchical(uri):
        return _prefix_contains(prefix, uri)
    return _opaque_contains(prefix, uri)


@frozen
class Policy:
    """
    The declared naming range(s) of an authority.

    A policy is the set of URI prefixes an authority claims responsibility
    for. The empty policy makes no claims at all, which represents an
    authority that may only contribute resources nobody else is responsible
    for.
    """

    _scopes: HashTrieSet[URI] = field(
        default=HashTrieSet(),
        converter=HashTrieSet,
        alias="scopes",
    )

    @classmethod
    def from_scopes(cls, scopes: Iterable[URI]) -> Policy:
        """
        Create a policy claiming each of the given naming ranges.
        """
        return cls(scopes=scopes)

    @property
    def scopes(self) -> HashTrieSet[URI]:
        """
        The URI prefixes this policy claims.
        """
        return self._scopes

    def covers(self, uri: URI) -> bool:
        """
        Whether ``uri`` falls within one of the declared naming ranges.
        """
        return any(contains(scope, uri) for scope in self._scopes)

    def _claims(self, uri: URI) -> HashTrieSet[URI]:
        return HashTrieSet(
            scope for scope in self._scopes if contains(scope, uri)
        )

    def relationship(self, uri: URI, other: Policy) -> str:
        """
        Describe how two policies relate with regard to a claimed ``uri``.

        Returns one of:

        * ``"only-self"`` -- only this policy is responsible for ``uri``;
        * ``"only-other"`` -- only the other policy is responsible;
        * ``"self-narrower"`` -- both are, and this policy is more specific;
        * ``"other-narrower"`` -- both are, and the other is more specific;
        * ``"equal"`` -- both make the same claims for ``uri``;
        * ``"unrelated"`` -- neither policy's ranges cover ``uri``.

        Two ranges which both cover the same URI are always prefix-ordered,
        so when both policies claim the URI the result is always one of the
        nested (or equal) relationships.
        """
        self_claims = self._claims(uri)
        other_claims = other._claims(uri)

        if self_claims and not other_claims:
            return "only-self"
        if other_claims and not self_claims:
            return "only-other"
        if not self_claims and not other_claims:
            return "unrelated"

        self_narrowest = self._narrowest(self_claims)
        other_narrowest = other._narrowest(other_claims)

        if contains(other_narrowest, self_narrowest):
            if contains(self_narrowest, other_narrowest):
                return "equal"
            return "self-narrower"
        return "other-narrower"

    @staticmethod
    def _narrowest(claims: HashTrieSet[URI]) -> URI:
        return max(claims, key=len)
