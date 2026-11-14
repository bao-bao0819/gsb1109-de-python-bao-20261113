import itertools

import pytest

from referencing import Resource, exceptions


def pairs(choices):
    return sorted(
        itertools.combinations(choices, 2),
        key=lambda error: str(type(error)),
    )


TRUE = Resource.opaque(True)


thunks = (
    lambda: exceptions.CannotDetermineSpecification(TRUE),
    lambda: exceptions.NoSuchResource("urn:example:foo"),
    lambda: exceptions.NotYetAvailable("urn:example:foo"),
    lambda: exceptions.IncompleteRetrieval("urn:example:foo"),
    lambda: exceptions.ResourceWithdrawn("urn:example:foo"),
    lambda: exceptions.NoInternalID(TRUE),
    lambda: exceptions.InvalidAnchor(resource=TRUE, anchor="foo", ref="a#b"),
    lambda: exceptions.NoSuchAnchor(resource=TRUE, anchor="foo", ref="a#b"),
    lambda: exceptions.PointerToNowhere(resource=TRUE, ref="urn:example:foo"),
    lambda: exceptions.Unresolvable("urn:example:foo"),
    lambda: exceptions.Unretrievable("urn:example:foo"),
)


@pytest.mark.parametrize("one, two", pairs(each() for each in thunks))
def test_eq_incompatible_types(one, two):
    assert one != two


@pytest.mark.parametrize("thunk", thunks)
def test_hash(thunk):
    assert thunk() in {thunk()}  # noqa: FURB171


@pytest.mark.parametrize(
    "subclass",
    [
        exceptions.NotYetAvailable,
        exceptions.IncompleteRetrieval,
        exceptions.ResourceWithdrawn,
    ],
)
def test_retrieval_failures_are_no_such_resources(subclass):
    failure = subclass(ref="urn:example:foo")
    assert issubclass(subclass, exceptions.NoSuchResource)
    assert isinstance(failure, exceptions.NoSuchResource)


def test_resource_withdrawn_equality_ignores_registry():
    one = exceptions.ResourceWithdrawn(ref="urn:example:foo")
    two = exceptions.ResourceWithdrawn(
        ref="urn:example:foo",
        registry=object(),  # any non-None registry marker
    )
    assert one == two
    assert hash(one) == hash(two)


def test_resource_withdrawn_default_registry_is_none():
    assert exceptions.ResourceWithdrawn(ref="urn:example:foo").registry is None
