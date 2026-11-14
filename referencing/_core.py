from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Sequence
from enum import Enum
from typing import Any, ClassVar, Generic, Protocol, TypeVar
from urllib.parse import unquote, urldefrag, urljoin

from attrs import evolve, field
from rpds import HashTrieMap, HashTrieSet, List

from referencing import exceptions
from referencing._attrs import frozen
from referencing.policy import Policy
from referencing.typing import URI, Anchor as AnchorType, D, Mapping, Retrieve

EMPTY_UNCRAWLED: HashTrieSet[URI] = HashTrieSet()
EMPTY_PREVIOUS_RESOLVERS: List[URI] = List()


class _Unset(Enum):
    """
    What sillyness...
    """

    SENTINEL = 1


_UNSET = _Unset.SENTINEL


class _MaybeInSubresource(Protocol[D]):
    def __call__(
        self,
        segments: Sequence[int | str],
        resolver: Resolver[D],
        subresource: Resource[D],
    ) -> Resolver[D]: ...


def _detect_or_error(contents: D) -> Specification[D]:
    if not isinstance(contents, Mapping):
        raise exceptions.CannotDetermineSpecification(contents)

    jsonschema_dialect_id = contents.get("$schema")  # type: ignore[reportUnknownMemberType]
    if not isinstance(jsonschema_dialect_id, str):
        raise exceptions.CannotDetermineSpecification(contents)

    from referencing.jsonschema import specification_with

    return specification_with(jsonschema_dialect_id)


def _detect_or_default(
    default: Specification[D],
) -> Callable[[D], Specification[D]]:
    def _detect(contents: D) -> Specification[D]:
        if not isinstance(contents, Mapping):
            return default

        jsonschema_dialect_id = contents.get("$schema")  # type: ignore[reportUnknownMemberType]
        if jsonschema_dialect_id is None:
            return default

        from referencing.jsonschema import specification_with

        return specification_with(
            jsonschema_dialect_id,  # type: ignore[reportUnknownArgumentType]
            default=default,
        )

    return _detect


class _SpecificationDetector:
    def __get__(
        self,
        instance: Specification[D] | None,
        cls: type[Specification[D]],
    ) -> Callable[[D], Specification[D]]:
        if instance is None:
            return _detect_or_error
        else:
            return _detect_or_default(instance)


@frozen
class Specification(Generic[D]):
    """
    A specification which defines referencing behavior.

    The various methods of a `Specification` allow for varying referencing
    behavior across JSON Schema specification versions, etc.
    """

    #: A short human-readable name for the specification, used for debugging.
    name: str

    #: Find the ID of a given document.
    id_of: Callable[[D], URI | None]

    #: Retrieve the subresources of the given document (without traversing into
    #: the subresources themselves).
    subresources_of: Callable[[D], Iterable[D]]

    #: While resolving a JSON pointer, conditionally enter a subresource
    #: (if e.g. we have just entered a keyword whose value is a subresource)
    maybe_in_subresource: _MaybeInSubresource[D]

    #: Retrieve the anchors contained in the given document.
    _anchors_in: Callable[
        [Specification[D], D],
        Iterable[AnchorType[D]],
    ] = field(alias="anchors_in")

    #: An opaque specification where resources have no subresources
    #: nor internal identifiers.
    OPAQUE: ClassVar[Specification[Any]]

    #: Attempt to discern which specification applies to the given contents.
    #:
    #: May be called either as an instance method or as a class method, with
    #: slightly different behavior in the following case:
    #:
    #: Recall that not all contents contains enough internal information about
    #: which specification it is written for -- the JSON Schema ``{}``,
    #: for instance, is valid under many different dialects and may be
    #: interpreted as any one of them.
    #:
    #: When this method is used as an instance method (i.e. called on a
    #: specific specification), that specification is used as the default
    #: if the given contents are unidentifiable.
    #:
    #: On the other hand when called as a class method, an error is raised.
    #:
    #: To reiterate, ``DRAFT202012.detect({})`` will return ``DRAFT202012``
    #: whereas the class method ``Specification.detect({})`` will raise an
    #: error.
    #:
    #: (Note that of course ``DRAFT202012.detect(...)`` may return some other
    #: specification when given a schema which *does* identify as being for
    #: another version).
    #:
    #: Raises:
    #:
    #:     `CannotDetermineSpecification`
    #:
    #:         if the given contents don't have any discernible
    #:         information which could be used to guess which
    #:         specification they identify as
    detect = _SpecificationDetector()

    def __repr__(self) -> str:
        return f"<Specification name={self.name!r}>"

    def anchors_in(self, contents: D):
        """
        Retrieve the anchors contained in the given document.
        """
        return self._anchors_in(self, contents)

    def create_resource(self, contents: D) -> Resource[D]:
        """
        Create a resource which is interpreted using this specification.
        """
        return Resource(contents=contents, specification=self)


Specification.OPAQUE = Specification(
    name="opaque",
    id_of=lambda contents: None,
    subresources_of=lambda contents: [],
    anchors_in=lambda specification, contents: [],
    maybe_in_subresource=lambda segments, resolver, subresource: resolver,
)


@frozen
class Resource(Generic[D]):
    r"""
    A document (deserialized JSON) with a concrete interpretation under a spec.

    In other words, a Python object, along with an instance of `Specification`
    which describes how the document interacts with referencing -- both
    internally (how it refers to other `Resource`\ s) and externally (how it
    should be identified such that it is referenceable by other documents).
    """

    contents: D
    _specification: Specification[D] = field(alias="specification")

    @classmethod
    def from_contents(
        cls,
        contents: D,
        default_specification: (
            type[Specification[D]] | Specification[D]
        ) = Specification,
    ) -> Resource[D]:
        """
        Create a resource guessing which specification applies to the contents.

        Raises:

            `CannotDetermineSpecification`

                if the given contents don't have any discernible
                information which could be used to guess which
                specification they identify as

        """
        specification = default_specification.detect(contents)
        return specification.create_resource(contents=contents)

    @classmethod
    def opaque(cls, contents: D) -> Resource[D]:
        """
        Create an opaque `Resource` -- i.e. one with opaque specification.

        See `Specification.OPAQUE` for details.
        """
        return Specification.OPAQUE.create_resource(contents=contents)

    def id(self) -> URI | None:
        """
        Retrieve this resource's (specification-specific) identifier.
        """
        id = self._specification.id_of(self.contents)
        if id is None:
            return
        return id.rstrip("#")

    def subresources(self) -> Iterable[Resource[D]]:
        """
        Retrieve this resource's subresources.
        """
        return (
            Resource.from_contents(
                each,
                default_specification=self._specification,
            )
            for each in self._specification.subresources_of(self.contents)
        )

    def anchors(self) -> Iterable[AnchorType[D]]:
        """
        Retrieve this resource's (specification-specific) anchors.
        """
        return self._specification.anchors_in(self.contents)

    def pointer(self, pointer: str, resolver: Resolver[D]) -> Resolved[D]:
        """
        Resolve the given JSON pointer.

        Raises:

            `exceptions.PointerToNowhere`

                if the pointer points to a location not present in the document

        """
        if not pointer:
            return Resolved(contents=self.contents, resolver=resolver)

        contents = self.contents
        segments: list[int | str] = []
        for segment in unquote(pointer[1:]).split("/"):
            if isinstance(contents, Sequence):
                segment = int(segment)
            else:
                segment = segment.replace("~1", "/").replace("~0", "~")
            try:
                contents = contents[segment]  # type: ignore[reportUnknownArgumentType]
            except LookupError as lookup_error:
                error = exceptions.PointerToNowhere(ref=pointer, resource=self)
                raise error from lookup_error

            segments.append(segment)
            last = resolver
            resolver = self._specification.maybe_in_subresource(
                segments=segments,
                resolver=resolver,
                subresource=self._specification.create_resource(contents),
            )
            if resolver is not last:
                segments = []
        return Resolved(contents=contents, resolver=resolver)  # type: ignore[reportUnknownArgumentType]


def _fail_to_retrieve(uri: URI):
    raise exceptions.NoSuchResource(ref=uri)


@frozen
class _BlockingConflict(Generic[D]):
    """
    A set of competing definitions which require manual resolution.

    The competing entries are retained alongside the policy that brought
    them in, so that a later combination with a more specifically authorized
    registry can still resolve the conflict automatically.
    """

    uri: URI
    name: str | None
    entries: List[tuple[Any, Policy]]
    scopes: HashTrieSet[URI]


def _dedupe(candidates: Iterable[tuple[Any, Policy]]):
    """
    Deduplicate (value, policy) candidates while preserving their order.

    Resource contents need not be hashable, so identity comparison is used.
    """
    unique: list[tuple[Any, Policy]] = []
    for candidate in candidates:
        value, policy = candidate
        if any(
            value is other_value and policy is other_policy
            for other_value, other_policy in unique
        ):
            continue
        unique.append(candidate)
    return unique


def _distinct(values: Iterable[Any]) -> list[Any]:
    """
    Return the distinct values, comparing by equality, not identity.

    Two equal resources (e.g. separately constructed but identical documents)
    count as a single definition, so they don't produce a conflict. Equality,
    rather than hashing, is used because resource contents need not be
    hashable.
    """
    distinct: list[Any] = []
    for value in values:
        if not any(value == other for other in distinct):
            distinct.append(value)
    return distinct


def _narrowest_claim(policy: Policy, uri: URI) -> URI | None:
    claims = policy._claims(uri)
    if not claims:
        return None
    return max(claims, key=len)


def _settle(
    uri: URI,
    candidates: Iterable[tuple[Any, Policy]],
    *,
    governed: bool,
    alias: bool,
) -> tuple[Any | None, bool]:
    """
    Pick the authoritative definition among competing claimants.

    Returns the winning value (or ``None`` when the group is blocked) and
    whether the group requires manual handling.

    A policy which declares no naming range is *neutral*: it preserves the
    legacy last-writer-wins behavior and never triggers a conflict. A policy
    which does declare ranges only speaks for URIs its ranges cover. A
    narrower covering range overrides a broader one; two unrelated covering
    ranges (or competing definitions none of the declared ranges cover)
    block the URI until a human intervenes.

    For aliases (anchors), when the URI is governed by some declared range,
    definitions brought by a policy whose ranges do not cover the URI are
    inadmissible, so an unauthorized name cannot route to another
    authority's definition.
    """
    unique = _dedupe(candidates)
    if len(unique) == 1:
        return unique[0][0], False

    if alias and governed:
        admissible = [
            (value, policy)
            for value, policy in unique
            if not policy.scopes or policy.covers(uri)
        ]
        if not admissible:
            return None, True
        unique = admissible
        if len(unique) == 1:
            return unique[0][0], False

    declarers = [(value, policy) for value, policy in unique if policy.scopes]
    if not declarers:
        # No authority has declared any naming range: legacy behavior.
        return unique[-1][0], False

    covering = [
        (value, policy, claim)
        for value, policy in unique
        if (claim := _narrowest_claim(policy, uri)) is not None
    ]
    if covering:
        # Any two ranges covering the same URI are prefix-ordered, so the
        # narrowest covering range uniquely decides the authority. A tie at
        # the same narrowness means genuinely competing definitions.
        widest = max(len(claim) for _, _, claim in covering)
        winners = _distinct(
            value for value, _, claim in covering if len(claim) == widest
        )
        if len(winners) == 1:
            return winners[0], False
        return None, True

    neutrals = [
        (value, policy) for value, policy in unique if not policy.scopes
    ]
    if neutrals:
        # The declared ranges don't cover this URI, so the neutral,
        # legacy definition remains authoritative.
        return neutrals[-1][0], False
    return None, True


def _conflict(
    uri: URI,
    name: str | None,
    entries: Iterable[tuple[Any, Policy]],
) -> _BlockingConflict[D]:
    pairs = _dedupe(entries)
    scopes: HashTrieSet[URI] = HashTrieSet()
    for _, policy in pairs:
        scopes = scopes.update(policy.scopes)
    return _BlockingConflict(
        uri=uri,
        name=name,
        entries=List(pairs),
        scopes=scopes,
    )


@frozen
class Registry(Mapping[URI, Resource[D]]):
    r"""
    A registry of `Resource`\ s, each identified by their canonical URIs.

    Registries store a collection of in-memory resources, and optionally
    enable additional resources which may be stored elsewhere (e.g. in a
    database, a separate set of files, over the network, etc.).

    They also lazily walk their known resources, looking for subresources
    within them. In other words, subresources contained within any added
    resources will be retrievable via their own IDs (though this discovery of
    subresources will be delayed until necessary).

    Registries are immutable, and their methods return new instances of the
    registry with the additional resources added to them.

    The ``retrieve`` argument can be used to configure retrieval of resources
    dynamically, either over the network, from a database, or the like.
    Pass it a callable which will be called if any URI not present in the
    registry is accessed. It must either return a `Resource` or else raise a
    `NoSuchResource` exception indicating that the resource does not exist
    even according to the retrieval logic.

    The ``policy`` argument declares the naming range(s) (URI prefixes) the
    authority populating this registry is responsible for. When registries
    are combined, these declarations decide which definition of a contested
    URI or anchor wins: a narrower authorization overrides a broader one,
    while two unrelated authorizations produce a *blocking conflict* which
    must be resolved manually (see `combine`, `blocking_resource_conflicts`
    and `blocking_anchor_conflicts`).
    """

    _resources: HashTrieMap[URI, Resource[D]] = field(
        default=HashTrieMap(),
        converter=HashTrieMap.convert,  # type: ignore[reportGeneralTypeIssues]
        alias="resources",
    )
    _anchors: HashTrieMap[tuple[URI, str], AnchorType[D]] = HashTrieMap()
    _uncrawled: HashTrieSet[URI] = EMPTY_UNCRAWLED
    _retrieve: Retrieve[D] = field(default=_fail_to_retrieve, alias="retrieve")
    # Authorization bookkeeping. These fields deliberately do not participate
    # in registry equality, which remains based on the retrievable contents.
    _policy: Policy = field(
        factory=Policy,
        alias="policy",
        eq=False,
    )
    #: The policy responsible for each (not yet blocked) resource URI.
    _policies: HashTrieMap[URI, Policy] = field(
        default=HashTrieMap(),
        alias="policies",
        eq=False,
    )
    #: The policy responsible for each (not yet blocked) anchor.
    _anchor_policies: HashTrieMap[tuple[URI, str], Policy] = field(
        default=HashTrieMap(),
        alias="anchor_policies",
        eq=False,
    )
    #: Blocking conflicts, keyed by URI (resources) or ``(URI, name)``.
    _conflicts: HashTrieMap[Any, _BlockingConflict[D]] = field(
        default=HashTrieMap(),
        alias="conflicts",
        eq=False,
    )

    def __getitem__(self, uri: URI) -> Resource[D]:
        """
        Return the (already crawled) `Resource` identified by the given URI.
        """
        uri = uri.rstrip("#")
        conflict = self._conflicts.get(uri)
        if conflict is not None:
            raise exceptions.PendingBlockingConflict(ref=uri)
        try:
            return self._resources[uri]
        except KeyError:
            raise exceptions.NoSuchResource(ref=uri) from None

    def __iter__(self) -> Iterator[URI]:
        """
        Iterate over all crawled URIs in the registry.
        """
        return iter(self._resources)

    def __len__(self) -> int:
        """
        Count the total number of fully crawled resources in this registry.
        """
        return len(self._resources)

    def __rmatmul__(
        self,
        new: Resource[D] | Iterable[Resource[D]],
    ) -> Registry[D]:
        """
        Create a new registry with resource(s) added using their internal IDs.

        Resources must have a internal IDs (e.g. the :kw:`$id` keyword in
        modern JSON Schema versions), otherwise an error will be raised.

        Both a single resource as well as an iterable of resources works, i.e.:

            * ``resource @ registry`` or

            * ``[iterable, of, multiple, resources] @ registry``

        which -- again, assuming the resources have internal IDs -- is
        equivalent to calling `Registry.with_resources` as such:

        .. code:: python

            registry.with_resources(
                (resource.id(), resource) for resource in new_resources
            )

        Raises:

            `NoInternalID`

                if the resource(s) in fact do not have IDs

        """
        if isinstance(new, Resource):
            new = (new,)

        pairs = []
        for resource in new:
            id = resource.id()
            if id is None:
                raise exceptions.NoInternalID(resource=resource)
            pairs.append((id, resource))
        return self.with_resources(pairs)

    def __repr__(self) -> str:
        size = len(self)
        pluralized = "resource" if size == 1 else "resources"
        if self._uncrawled:
            uncrawled = len(self._uncrawled)
            if uncrawled == size:
                summary = f"uncrawled {pluralized}"
            else:
                summary = f"{pluralized}, {uncrawled} uncrawled"
        else:
            summary = f"{pluralized}"
        conflicts = len(self._conflicts)
        if conflicts:
            summary += f", {conflicts} blocking conflict"
            if conflicts != 1:
                summary += "s"
        return f"<Registry ({size} {summary})>"

    def get_or_retrieve(self, uri: URI) -> Retrieved[D, Resource[D]]:
        """
        Get a resource from the registry, crawling or retrieving if necessary.

        May involve crawling to find the given URI if it is not already known,
        so the returned object is a `Retrieved` object which contains both the
        resource value as well as the registry which ultimately contained it.
        """
        uri = uri.rstrip("#")
        if self._conflicts.get(uri) is not None:
            raise exceptions.PendingBlockingConflict(ref=uri)

        resource = self._resources.get(uri)
        if resource is not None:
            return Retrieved(registry=self, value=resource)

        registry = self.crawl()
        if registry._conflicts.get(uri) is not None:
            raise exceptions.PendingBlockingConflict(ref=uri)
        resource = registry._resources.get(uri)
        if resource is not None:
            return Retrieved(registry=registry, value=resource)

        try:
            resource = registry._retrieve(uri)
        except (
            exceptions.CannotDetermineSpecification,
            exceptions.NoSuchResource,
        ):
            raise
        except Exception as error:
            raise exceptions.Unretrievable(ref=uri) from error
        else:
            registry = registry.with_resource(uri, resource)
            return Retrieved(registry=registry, value=resource)

    def remove(self, uri: URI):
        """
        Return a registry with the resource identified by a given URI removed.
        """
        if uri not in self._resources:
            raise exceptions.NoSuchResource(ref=uri)

        anchor_policies = self._anchor_policies
        for key in [key for key in anchor_policies if key[0] == uri]:
            anchor_policies = anchor_policies.discard(key)
        conflicts = HashTrieMap(
            (key, conflict)
            for key, conflict in self._conflicts.items()
            if key != uri and not (isinstance(key, tuple) and key[0] == uri)
        )
        return evolve(
            self,
            resources=self._resources.remove(uri),
            uncrawled=self._uncrawled.discard(uri),
            anchors=HashTrieMap(
                (k, v) for k, v in self._anchors.items() if k[0] != uri
            ),
            policies=self._policies.discard(uri),
            anchor_policies=anchor_policies,
            conflicts=conflicts,
        )

    def anchor(self, uri: URI, name: str):
        """
        Retrieve a given anchor from a resource which must already be crawled.
        """
        blocked = self._blocked_anchor(uri, name)
        if blocked is not None:
            raise exceptions.PendingBlockingConflict(ref=blocked)

        value = self._anchors.get((uri, name))
        if value is not None:
            return Retrieved(value=value, registry=self)

        registry = self.crawl()
        blocked = registry._blocked_anchor(uri, name)
        if blocked is not None:
            raise exceptions.PendingBlockingConflict(ref=blocked)
        value = registry._anchors.get((uri, name))
        if value is not None:
            return Retrieved(value=value, registry=registry)

        resource = self[uri]
        canonical_uri = resource.id()
        if canonical_uri is not None:
            blocked = registry._blocked_anchor(canonical_uri, name)
            if blocked is not None:
                raise exceptions.PendingBlockingConflict(ref=blocked)
            value = registry._anchors.get((canonical_uri, name))
            if value is not None:
                return Retrieved(value=value, registry=registry)

        if "/" in name:
            raise exceptions.InvalidAnchor(
                ref=uri,
                resource=resource,
                anchor=name,
            )
        raise exceptions.NoSuchAnchor(ref=uri, resource=resource, anchor=name)

    def _blocked_anchor(self, uri: URI, name: str) -> str | None:
        for key in ((uri, name), (uri.rstrip("#"), name)):
            if self._conflicts.get(key) is not None:
                return f"{uri}#{name}"
        return None

    def contents(self, uri: URI) -> D:
        """
        Retrieve the (already crawled) contents identified by the given URI.
        """
        return self[uri].contents

    def crawl(self) -> Registry[D]:
        """
        Crawl all added resources, discovering subresources.

        Discovered subresources and anchors are settled under the same naming
        rules as explicitly added ones: a definition brought by an authority
        whose declared ranges do not cover the URI cannot override one
        belonging to an authority which does, and unrelated competing
        definitions produce a blocking conflict.
        """
        resources = self._resources
        anchors = self._anchors
        anchor_policies = self._anchor_policies
        policies = self._policies
        conflicts = self._conflicts
        uncrawled = [
            (uri, resources[uri], policies.get(uri, self._policy))
            for uri in self._uncrawled
        ]
        while uncrawled:
            uri, resource, contributing_policy = uncrawled.pop()

            id = resource.id()
            if id is not None:
                uri = urljoin(uri, id)
                incoming_policy = contributing_policy
                previous_conflict = conflicts.get(uri)
                existing_policy = policies.get(uri)
                if previous_conflict is not None:
                    candidates = [
                        *previous_conflict.entries,
                        (resource, incoming_policy),
                    ]
                elif existing_policy is not None:
                    candidates = [
                        (resources[uri], existing_policy),
                        (resource, incoming_policy),
                    ]
                else:
                    candidates = [(resource, incoming_policy)]

                winner, blocked = _settle(
                    uri,
                    candidates,
                    governed=False,
                    alias=False,
                )
                if blocked:
                    (
                        resources,
                        _ignored,
                        policies,
                        anchors,
                        anchor_policies,
                        conflicts,
                    ) = self._block_uri(
                        uri,
                        candidates,
                        resources,
                        HashTrieSet(),
                        policies,
                        anchors,
                        anchor_policies,
                        conflicts,
                    )
                    uncrawled = [each for each in uncrawled if each[0] != uri]
                    continue
                if previous_conflict is not None:
                    conflicts = conflicts.discard(uri)
                resources = resources.insert(uri, winner)
                policies = policies.insert(
                    uri,
                    incoming_policy
                    if winner is resource
                    else (existing_policy or incoming_policy),
                )

            for each in resource.anchors():
                key = (uri, each.name)
                anchor_incoming_policy = contributing_policy
                previous = conflicts.get(key)
                existing_anchor_policy = anchor_policies.get(key)
                if previous is not None:
                    anchor_candidates = [
                        *previous.entries,
                        (each, anchor_incoming_policy),
                    ]
                elif existing_anchor_policy is not None:
                    anchor_candidates = [
                        (anchors[key], existing_anchor_policy),
                        (each, anchor_incoming_policy),
                    ]
                else:
                    anchor_candidates = [(each, anchor_incoming_policy)]

                governed = any(
                    each_policy.covers(uri)
                    for _, each_policy in anchor_candidates
                )
                anchor_winner, anchor_blocked = _settle(
                    uri,
                    anchor_candidates,
                    governed=governed,
                    alias=True,
                )
                if anchor_blocked:  # pragma: no cover
                    # Defensive: anchors are derived from resource contents, so
                    # diverging anchors always diverge at the resource level,
                    # which is blocked (and its aliases sealed) above first.
                    conflicts = conflicts.insert(
                        key,
                        _conflict(uri, each.name, anchor_candidates),
                    )
                    anchors = anchors.discard(key)
                    anchor_policies = anchor_policies.discard(key)
                    continue
                if previous is not None:
                    conflicts = conflicts.discard(key)
                anchors = anchors.insert(key, anchor_winner)
                anchor_policies = anchor_policies.insert(
                    key,
                    anchor_incoming_policy
                    if anchor_winner is each
                    else (existing_anchor_policy or anchor_incoming_policy),
                )
            uncrawled.extend(
                (uri, each, contributing_policy)
                for each in resource.subresources()
            )
        return evolve(
            self,
            resources=resources,
            anchors=anchors,
            anchor_policies=anchor_policies,
            policies=policies,
            conflicts=conflicts,
            uncrawled=EMPTY_UNCRAWLED,
        )

    def with_resource(self, uri: URI, resource: Resource[D]):
        """
        Add the given `Resource` to the registry, without crawling it.
        """
        return self.with_resources([(uri, resource)])

    def _block_uri(
        self,
        uri: URI,
        candidates: list[tuple[Any, Policy]],
        resources: HashTrieMap[URI, Resource[D]],
        uncrawled: HashTrieSet[URI],
        policies: HashTrieMap[URI, Policy],
        anchors: HashTrieMap[tuple[URI, str], AnchorType[D]],
        anchor_policies: HashTrieMap[tuple[URI, str], Policy],
        conflicts: HashTrieMap[Any, _BlockingConflict[D]],
        incoming: Registry[D] | None = None,
    ) -> tuple[
        HashTrieMap[URI, Resource[D]],
        HashTrieSet[URI],
        HashTrieMap[URI, Policy],
        HashTrieMap[tuple[URI, str], AnchorType[D]],
        HashTrieMap[tuple[URI, str], Policy],
        HashTrieMap[Any, _BlockingConflict[D]],
    ]:
        """
        Record a blocked resource and seal every alias route into it.

        A contested resource must not stay retrievable, nor may an anchor
        provide a back door to one of its competing definitions.
        """
        conflicts = conflicts.insert(uri, _conflict(uri, None, candidates))
        resources = resources.discard(uri)
        uncrawled = uncrawled.discard(uri)
        policies = policies.discard(uri)

        anchor_keys = [key for key in anchors if key[0] == uri]
        if incoming is not None:
            anchor_keys = [
                *anchor_keys,
                *(key for key in incoming._anchors if key[0] == uri),
            ]
        for key in dict.fromkeys(anchor_keys):
            entries = []
            existing = anchors.get(key)
            if existing is not None:
                entries.append((existing, anchor_policies.get(key, Policy())))
            if incoming is not None:
                other = incoming._anchors.get(key)
                if other is not None:
                    entries.append(
                        (
                            other,
                            incoming._anchor_policies.get(
                                key,
                                incoming._policy,
                            ),
                        ),
                    )
            anchors = anchors.discard(key)
            anchor_policies = anchor_policies.discard(key)
            # Every collected key comes from an existing or incoming anchor,
            # so `entries` is always non-empty here.
            if entries and conflicts.get(key) is None:  # pragma: no branch
                conflicts = conflicts.insert(
                    key,
                    _conflict(uri, key[1], entries),
                )
        return (
            resources,
            uncrawled,
            policies,
            anchors,
            anchor_policies,
            conflicts,
        )

    def with_resources(
        self,
        pairs: Iterable[tuple[URI, Resource[D]]],
    ) -> Registry[D]:
        r"""
        Add the given `Resource`\ s to the registry, without crawling them.

        Each resource is authorized by this registry's policy. If another
        authority already provides the same URI, the declared naming ranges
        decide the winner (or produce a blocking conflict), exactly as in
        `combine`.
        """
        resources = self._resources
        uncrawled = self._uncrawled
        policies = self._policies
        conflicts = self._conflicts
        anchors = self._anchors
        anchor_policies = self._anchor_policies
        for raw_uri, resource in pairs:
            # Empty fragment URIs are equivalent to URIs without the fragment.
            # TODO: Is this true for non JSON Schema resources? Probably not.
            uri = raw_uri.rstrip("#")
            existing_policy = policies.get(uri)
            previous_conflict = conflicts.get(uri)
            if previous_conflict is not None:
                candidates = [
                    *previous_conflict.entries,
                    (resource, self._policy),
                ]
            elif existing_policy is not None:
                candidates = [
                    (resources[uri], existing_policy),
                    (resource, self._policy),
                ]
            else:
                candidates = [(resource, self._policy)]

            winner, blocked = _settle(
                uri,
                candidates,
                governed=False,
                alias=False,
            )
            if blocked:
                (
                    resources,
                    uncrawled,
                    policies,
                    anchors,
                    anchor_policies,
                    conflicts,
                ) = self._block_uri(
                    uri,
                    candidates,
                    resources,
                    uncrawled,
                    policies,
                    anchors,
                    anchor_policies,
                    conflicts,
                )
                continue
            if previous_conflict is not None:
                conflicts = conflicts.discard(uri)
            resources = resources.insert(uri, winner)
            uncrawled = uncrawled.insert(uri)
            policies = policies.insert(
                uri,
                self._policy
                if winner is resource
                else (existing_policy or self._policy),
            )
        return evolve(
            self,
            resources=resources,
            uncrawled=uncrawled,
            policies=policies,
            anchors=anchors,
            anchor_policies=anchor_policies,
            conflicts=conflicts,
        )

    def with_contents(
        self,
        pairs: Iterable[tuple[URI, D]],
        **kwargs: Any,
    ) -> Registry[D]:
        r"""
        Add the given contents to the registry, autodetecting when necessary.
        """
        return self.with_resources(
            (uri, Resource.from_contents(each, **kwargs))
            for uri, each in pairs
        )

    def with_policy(self, scopes: Iterable[URI] | Policy) -> Registry[D]:
        """
        Return a registry whose resources are authorized under named ranges.

        The argument is either an iterable of URI prefixes the authority
        claims, or an already-constructed `~referencing.policy.Policy`.
        Only resources added (or combined in) *after* calling this method
        carry the declaration; resources already present keep the policy
        they were contributed under.
        """
        policy = (
            scopes
            if isinstance(scopes, Policy)
            else Policy.from_scopes(scopes)
        )
        return evolve(self, policy=policy)

    def combine(self, *registries: Registry[D]) -> Registry[D]:
        """
        Combine together one or more other registries, producing a unified one.

        For every contested URI or anchor, the naming ranges declared by each
        registry's `~referencing.policy.Policy` decide the authority:

        * a narrower covering range overrides a broader one regardless of
          combination order;
        * a definition from an authority whose ranges don't cover the URI is
          inadmissible and cannot override an authorized one;
        * when no single authority wins (two equally specific, differing
          definitions, or competing definitions none of the ranges cover), a
          blocking conflict is recorded and surfaced via
          `blocking_resource_conflicts` / `blocking_anchor_conflicts`;
        * registries without a declared policy remain neutral and preserve
          the previous last-writer-wins behavior.

        The result is independent of the order the registries are combined
        in: the same resources are retrievable and the same conflicts remain.
        """
        if len(registries) == 1 and registries[0] is self:
            return self

        resources = self._resources
        anchors = self._anchors
        uncrawled = self._uncrawled
        policies = self._policies
        anchor_policies = self._anchor_policies
        conflicts = self._conflicts
        retrieve = self._retrieve
        policy = self._policy

        for registry in registries:
            if not policy.scopes and registry._policy.scopes:
                policy = registry._policy

            if registry._retrieve is not _fail_to_retrieve:
                if registry._retrieve is not retrieve is not _fail_to_retrieve:
                    raise ValueError(  # noqa: TRY003
                        "Cannot combine registries with conflicting retrieval "
                        "functions.",
                    )
                retrieve = registry._retrieve
            uncrawled = uncrawled.update(registry._uncrawled)

            handled: HashTrieSet[Any] = HashTrieSet()
            for uri, incoming in registry._resources.items():
                handled = handled.insert(uri)
                incoming_policy = registry._policies.get(
                    uri,
                    registry._policy,
                )
                previous_conflict = conflicts.get(uri)
                if previous_conflict is not None:
                    candidates = [
                        *previous_conflict.entries,
                        (incoming, incoming_policy),
                    ]
                else:
                    existing_policy = policies.get(uri)
                    if existing_policy is None:
                        candidates = [(incoming, incoming_policy)]
                    else:
                        candidates = [
                            (resources[uri], existing_policy),
                            (incoming, incoming_policy),
                        ]

                winner, blocked = _settle(
                    uri,
                    candidates,
                    governed=False,
                    alias=False,
                )
                if blocked:
                    (
                        resources,
                        uncrawled,
                        policies,
                        anchors,
                        anchor_policies,
                        conflicts,
                    ) = self._block_uri(
                        uri,
                        candidates,
                        resources,
                        uncrawled,
                        policies,
                        anchors,
                        anchor_policies,
                        conflicts,
                        incoming=registry,
                    )
                    continue
                if previous_conflict is not None:
                    conflicts = conflicts.discard(uri)
                resources = resources.insert(uri, winner)
                winning_policy = (
                    incoming_policy
                    if winner is incoming
                    else (policies.get(uri) or incoming_policy)
                )
                policies = policies.insert(uri, winning_policy)

            for key, incoming in registry._anchors.items():
                handled = handled.insert(key)
                # A blocked resource keeps every alias into it sealed, so
                # don't let the anchor loop resurrect one of its names.
                resource_conflict = conflicts.get(key[0])
                if (
                    resource_conflict is not None
                    and resource_conflict.name is None
                ):
                    continue
                incoming_policy = registry._anchor_policies.get(
                    key,
                    registry._policy,
                )
                previous_conflict = conflicts.get(key)
                if previous_conflict is not None:
                    candidates = [
                        *previous_conflict.entries,
                        (incoming, incoming_policy),
                    ]
                else:
                    existing_policy = anchor_policies.get(key)
                    if existing_policy is None:
                        candidates = [(incoming, incoming_policy)]
                    else:
                        candidates = [
                            (anchors[key], existing_policy),
                            (incoming, incoming_policy),
                        ]

                governed = any(
                    each_policy.covers(key[0]) for _, each_policy in candidates
                )
                winner, blocked = _settle(
                    key[0],
                    candidates,
                    governed=governed,
                    alias=True,
                )
                if blocked:  # pragma: no cover
                    # Defensive: see the equivalent branch in `crawl`; the
                    # resource-level conflict above seals these aliases first.
                    conflicts = conflicts.insert(
                        key,
                        _conflict(key[0], key[1], candidates),
                    )
                    anchors = anchors.discard(key)
                    anchor_policies = anchor_policies.discard(key)
                    continue
                if previous_conflict is not None:
                    conflicts = conflicts.discard(key)
                anchors = anchors.insert(key, winner)
                anchor_policies = anchor_policies.insert(
                    key,
                    incoming_policy
                    if winner is incoming
                    else (anchor_policies.get(key) or incoming_policy),
                )

            # Carry over conflicts the incoming registry knew about which
            # were not re-settled above (e.g. a contested URI absent from its
            # own resource map but already recorded as blocked).
            for key, conflict in registry._conflicts.items():
                if key not in handled and conflicts.get(key) is None:
                    conflicts = conflicts.insert(key, conflict)

        return evolve(
            self,
            anchors=anchors,
            resources=resources,
            uncrawled=uncrawled,
            policies=policies,
            anchor_policies=anchor_policies,
            conflicts=conflicts,
            retrieve=retrieve,
            policy=policy,
        )

    def blocking_resource_conflicts(
        self,
    ) -> dict[URI, tuple[Resource[D], ...]]:
        """
        Return the unresolved resource conflicts, keyed by contested URI.

        Each value is the tuple of competing `Resource` definitions brought by
        unrelated authorities. The registry cannot retrieve a contested URI
        until the conflict is handled manually.
        """
        return {
            key: tuple(entry[0] for entry in conflict.entries)
            for key, conflict in sorted(
                self._conflicts.items(),
                key=lambda item: repr(item[0]),
            )
            if isinstance(key, str)
        }

    def blocking_anchor_conflicts(
        self,
    ) -> dict[tuple[URI, str], tuple[AnchorType[D], ...]]:
        """
        Return the unresolved anchor conflicts, keyed by ``(URI, name)``.

        Each value is the tuple of competing anchor definitions brought by
        unrelated authorities.
        """
        return {
            key: tuple(entry[0] for entry in conflict.entries)
            for key, conflict in sorted(
                self._conflicts.items(),
                key=lambda item: repr(item[0]),
            )
            if isinstance(key, tuple)
        }

    def resolver(self, base_uri: URI = "") -> Resolver[D]:
        """
        Return a `Resolver` which resolves references against this registry.
        """
        return Resolver(base_uri=base_uri, registry=self)

    def resolver_with_root(self, resource: Resource[D]) -> Resolver[D]:
        """
        Return a `Resolver` with a specific root resource.
        """
        uri = resource.id() or ""
        return Resolver(
            base_uri=uri,
            registry=self.with_resource(uri, resource),
        )


#: An anchor or resource.
AnchorOrResource = TypeVar(
    "AnchorOrResource",
    AnchorType[Any],
    Resource[Any],
    default=Resource[Any],
)


@frozen
class Retrieved(Generic[D, AnchorOrResource]):
    """
    A value retrieved from a `Registry`.
    """

    value: AnchorOrResource
    registry: Registry[D]


@frozen
class Resolved(Generic[D]):
    """
    A reference resolved to its contents by a `Resolver`.
    """

    contents: D
    resolver: Resolver[D]


@frozen
class Resolver(Generic[D]):
    """
    A reference resolver.

    Resolvers help resolve references (including relative ones) by
    pairing a fixed base URI with a `Registry`.

    This object, under normal circumstances, is expected to be used by
    *implementers of libraries* built on top of `referencing` (e.g. JSON Schema
    implementations or other libraries resolving JSON references),
    not directly by end-users populating registries or while writing
    schemas or other resources.

    References are resolved against the base URI, and the combined URI
    is then looked up within the registry.

    The process of resolving a reference may itself involve calculating
    a *new* base URI for future reference resolution (e.g. if an
    intermediate resource sets a new base URI), or may involve encountering
    additional subresources and adding them to a new registry.
    """

    _base_uri: URI = field(alias="base_uri")
    _registry: Registry[D] = field(alias="registry")
    _previous: List[URI] = field(default=List(), repr=False, alias="previous")

    def lookup(self, ref: URI) -> Resolved[D]:
        """
        Resolve the given reference to the resource it points to.

        Raises:

            `exceptions.Unresolvable`

                or a subclass thereof (see below) if the reference isn't
                resolvable

            `exceptions.NoSuchAnchor`

                if the reference is to a URI where a resource exists but
                contains a plain name fragment which does not exist within
                the resource

            `exceptions.PointerToNowhere`

                if the reference is to a URI where a resource exists but
                contains a JSON pointer to a location within the resource
                that does not exist

        """
        if ref.startswith("#"):
            uri, fragment = self._base_uri, ref[1:]
        else:
            uri, fragment = urldefrag(urljoin(self._base_uri, ref))
        try:
            retrieved = self._registry.get_or_retrieve(uri)
        except exceptions.NoSuchResource:
            raise exceptions.Unresolvable(ref=ref) from None
        except exceptions.Unretrievable as error:
            raise exceptions.Unresolvable(ref=ref) from error

        if fragment.startswith("/"):
            resolver = self._evolve(registry=retrieved.registry, base_uri=uri)
            return retrieved.value.pointer(pointer=fragment, resolver=resolver)

        if fragment:
            retrieved = retrieved.registry.anchor(uri, fragment)
            resolver = self._evolve(registry=retrieved.registry, base_uri=uri)
            return retrieved.value.resolve(resolver=resolver)

        resolver = self._evolve(registry=retrieved.registry, base_uri=uri)
        return Resolved(contents=retrieved.value.contents, resolver=resolver)

    def in_subresource(self, subresource: Resource[D]) -> Resolver[D]:
        """
        Create a resolver for a subresource (which may have a new base URI).
        """
        id = subresource.id()
        if id is None:
            return self
        return evolve(self, base_uri=urljoin(self._base_uri, id))

    def dynamic_scope(self) -> Iterable[tuple[URI, Registry[D]]]:
        """
        In specs with such a notion, return the URIs in the dynamic scope.
        """
        for uri in self._previous:
            yield uri, self._registry

    def _evolve(self, base_uri: URI, **kwargs: Any):
        """
        Evolve, appending to the dynamic scope.
        """
        previous = self._previous
        if self._base_uri and (not previous or base_uri != self._base_uri):
            previous = previous.push_front(self._base_uri)
        return evolve(self, base_uri=base_uri, previous=previous, **kwargs)


@frozen
class Anchor(Generic[D]):
    """
    A simple anchor in a `Resource`.
    """

    name: str
    resource: Resource[D]

    def resolve(self, resolver: Resolver[D]):
        """
        Return the resource for this anchor.
        """
        return Resolved(contents=self.resource.contents, resolver=resolver)
