"""
Errors, oh no!
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import attrs

from referencing._attrs import frozen

if TYPE_CHECKING:
    from referencing import Resource
    from referencing._core import Registry
    from referencing.typing import URI


@attrs.frozen  # Because here we allow subclassing below.
class NoSuchResource(KeyError):
    """
    The given URI is not present in a registry.

    Unlike most exceptions, this class *is* intended to be publicly
    instantiable and *is* part of the public API of the package.

    More specific subclasses let a retriever explain *why* a resource could
    not be returned, which controls whether the answer may be reused by later
    requests:

    * `NotYetAvailable` and `IncompleteRetrieval` describe a request which
      could not be completed this time. They are not authoritative, are never
      reused, and a later request will attempt retrieval again;

    * `ResourceWithdrawn` describes a publisher-confirmed, durable absence.
      It is authoritative and is reused for later requests within the same
      issuing scope until a concrete resource supersedes it.

    Raising this base class directly remains supported and preserves the
    original behavior of simply signaling that the retriever cannot look up
    the given URI.
    """

    ref: URI

    def __eq__(self, other: object) -> bool:
        if self.__class__ is not other.__class__:
            return NotImplemented
        return attrs.astuple(self) == attrs.astuple(other)

    def __hash__(self) -> int:
        return hash(attrs.astuple(self))


@frozen
class NotYetAvailable(NoSuchResource):
    """
    A resource which has not been published yet and may appear later.

    This represents a *temporary* absence: the origin simply has not made the
    resource available at the time of the request. It is not authoritative, so
    it must never be cached or reused for subsequent requests, each of which
    should attempt retrieval again.
    """


@frozen
class IncompleteRetrieval(NoSuchResource):
    """
    A request which could not be fully read.

    Unlike `NotYetAvailable`, the resource may in fact already exist; the
    problem is that this particular request did not finish reading it (e.g. a
    truncated response or an interrupted connection). Like `NotYetAvailable`,
    the result is not authoritative and must not be reused -- a later request
    should retry retrieval.
    """


@frozen
class ResourceWithdrawn(NoSuchResource):
    """
    A resource whose publisher has confirmed it has been withdrawn.

    This absence is authoritative and durable: the publisher asserts the
    resource will never exist at the given URI. The result is therefore reused
    for later requests within the same issuing scope, until a concrete
    resource for the URI supersedes it.

    Because registries are immutable, a `Registry.get_or_retrieve` call which
    first observes this exception raises a copy carrying the evolved registry
    that recorded the absence in ``registry``. Reusing that registry for later
    requests replays the same answer without calling the retriever again. The
    ``registry`` is intentionally excluded from equality and hashing, which
    depend only on ``ref``.
    """

    registry: Registry[Any] | None = attrs.field(default=None, eq=False)

    def __eq__(self, other: object) -> bool:
        # Only ``ref`` participates in equality. The inherited implementation
        # compares every field via ``attrs.astuple``, which would include the
        # non-equality ``registry`` field above. This class cannot be
        # subclassed, so the ``isinstance`` check is an exact-class check.
        if not isinstance(other, ResourceWithdrawn):
            return NotImplemented
        return self.ref == other.ref

    def __hash__(self) -> int:
        return hash(self.ref)


@frozen
class NoInternalID(Exception):
    """
    A resource has no internal ID, but one is needed.

    E.g. in modern JSON Schema drafts, this is the :kw:`$id` keyword.

    One might be needed if a resource was to-be added to a registry but no
    other URI is available, and the resource doesn't declare its canonical URI.
    """

    resource: Resource[Any]

    def __eq__(self, other: object) -> bool:
        if self.__class__ is not other.__class__:
            return NotImplemented
        return attrs.astuple(self) == attrs.astuple(other)

    def __hash__(self) -> int:
        return hash(attrs.astuple(self))


@frozen
class Unretrievable(KeyError):
    """
    The given URI is not present in a registry, and retrieving it failed.
    """

    ref: URI

    def __eq__(self, other: object) -> bool:
        if self.__class__ is not other.__class__:
            return NotImplemented
        return attrs.astuple(self) == attrs.astuple(other)

    def __hash__(self) -> int:
        return hash(attrs.astuple(self))


@frozen
class CannotDetermineSpecification(Exception):
    """
    Attempting to detect the appropriate `Specification` failed.

    This happens if no discernible information is found in the contents of the
    new resource which would help identify it.
    """

    contents: Any

    def __eq__(self, other: object) -> bool:
        if self.__class__ is not other.__class__:
            return NotImplemented
        return attrs.astuple(self) == attrs.astuple(other)

    def __hash__(self) -> int:
        return hash(attrs.astuple(self))


@attrs.frozen  # Because here we allow subclassing below.
class Unresolvable(Exception):
    """
    A reference was unresolvable.
    """

    ref: URI

    def __eq__(self, other: object) -> bool:
        if self.__class__ is not other.__class__:
            return NotImplemented
        return attrs.astuple(self) == attrs.astuple(other)

    def __hash__(self) -> int:
        return hash(attrs.astuple(self))


@frozen
class PointerToNowhere(Unresolvable):
    """
    A JSON Pointer leads to a part of a document that does not exist.
    """

    resource: Resource[Any]

    def __str__(self) -> str:
        msg = f"{self.ref!r} does not exist within {self.resource.contents!r}"
        if self.ref == "/":
            msg += (
                ". The pointer '/' is a valid JSON Pointer but it points to "
                "an empty string property ''. If you intended to point "
                "to the entire resource, you should use '#'."
            )
        return msg


@frozen
class NoSuchAnchor(Unresolvable):
    """
    An anchor does not exist within a particular resource.
    """

    resource: Resource[Any]
    anchor: str

    def __str__(self) -> str:
        return (
            f"{self.anchor!r} does not exist within {self.resource.contents!r}"
        )


@frozen
class InvalidAnchor(Unresolvable):
    """
    An anchor which could never exist in a resource was dereferenced.

    It is somehow syntactically invalid.
    """

    resource: Resource[Any]
    anchor: str

    def __str__(self) -> str:
        return (
            f"'#{self.anchor}' is not a valid anchor, neither as a "
            "plain name anchor nor as a JSON Pointer. You may have intended "
            f"to use '#/{self.anchor}', as the slash is required *before each "
            "segment* of a JSON pointer."
        )
