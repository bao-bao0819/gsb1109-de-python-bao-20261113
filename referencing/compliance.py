"""
Compliance impact analysis for a withdrawn external resource.

Given a `Registry` of registered entrypoints (each a `Resource`) and the URI
of a single withdrawn external resource, trace the references made by each
entrypoint -- including references nested inside subresources and reached
through chains of cross-resource references -- and classify every entrypoint
into one of three categories:

* `DependencyStatus.DEPENDENT` -- a reference path genuinely depends on the
  withdrawn resource, and no approved mirror is able to replace it;
* `DependencyStatus.MIRRORABLE` -- the dependency can be served by an
  approved mirror (either the entrypoint already points at a mirror URI, or
  an ingested approved mirror exists for the withdrawn URI);
* `DependencyStatus.UNRELATED` -- no reference from the entrypoint ever
  reaches the withdrawn resource's contract.

Cross-references between resources cannot make the trace run forever: each
resource is visited at most once, so mutually referring resources and
reference cycles always terminate.

Because only a single withdrawal changes at a time, the entrypoints which
must be re-validated are directly queryable via
`ImpactReport.requires_revalidation`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import Enum
from typing import TYPE_CHECKING
from urllib.parse import urldefrag, urljoin

from rpds import HashTrieMap, HashTrieSet

from referencing import exceptions
from referencing._attrs import frozen

if TYPE_CHECKING:
    from referencing.typing import URI

#: Reference keywords whose string values identify other resources.
_REF_KEYWORDS = HashTrieSet(("$ref", "$recursiveRef", "$dynamicRef"))

#: Keywords which change the base URI for references within a document.
_BASE_KEYWORDS = HashTrieSet(("$id", "id"))


class DependencyStatus(Enum):
    """
    How an entrypoint relates to a withdrawn external resource.
    """

    DEPENDENT = "dependent"
    MIRRORABLE = "mirrorable"
    UNRELATED = "unrelated"


@frozen
class Reference:
    """
    A single reference path reaching a withdrawn resource or its mirror.

    ``source`` is the canonical URI of the resource containing the reference,
    ``target`` the canonical URI the reference resolves to (without
    fragment), ``fragment`` any JSON pointer or anchor fragment carried by
    the original reference, ``pointer`` a JSON pointer locating the
    reference field within the source resource, ``ref`` the original
    reference string, and ``via`` the chain of resource URIs traversed from
    the entrypoint to the source.
    """

    source: URI
    target: URI
    fragment: str
    pointer: str
    ref: str
    via: tuple[URI, ...]


@frozen
class Impact:
    """
    The compliance status of one entrypoint, with the evidence behind it.
    """

    status: DependencyStatus
    references: tuple[Reference, ...]


@frozen
class ImpactReport:
    """
    The result of analyzing one withdrawal against a set of entrypoints.
    """

    withdrawn: URI
    mirrors: tuple[URI, ...]
    impacts: HashTrieMap[URI, Impact]

    def requires_revalidation(self) -> tuple[URI, ...]:
        """
        Return the entrypoint URIs impacted by the withdrawal.

        These are exactly the entrypoints whose status is not
        `DependencyStatus.UNRELATED`, i.e. the ingestion records which must
        be re-validated because of the single withdrawn resource.
        """
        return tuple(
            sorted(
                uri
                for uri, impact in self.impacts.items()
                if impact.status is not DependencyStatus.UNRELATED
            ),
        )

    def entrypoints(self, status: DependencyStatus) -> tuple[URI, ...]:
        """
        Return the entrypoint URIs with the given status, sorted.
        """
        return tuple(
            sorted(
                uri
                for uri, impact in self.impacts.items()
                if impact.status is status
            ),
        )

    def impact_for(self, uri: URI) -> Impact:
        """
        Return the `Impact` recorded for the given entrypoint URI.
        """
        try:
            return self.impacts[uri]
        except KeyError:
            raise exceptions.NoSuchResource(ref=uri) from None


def analyze(
    registry,
    withdrawn: URI,
    mirrors: Sequence[URI] | frozenset[URI] = (),
) -> ImpactReport:
    """
    Analyze the impact of withdrawing one external resource URI.

    ``mirrors`` is an optional collection of approved mirror URIs for the
    withdrawn resource. An entrypoint with no reaching path is
    `DependencyStatus.UNRELATED`. An entrypoint whose reaching paths only
    point at mirror URIs is `DependencyStatus.MIRRORABLE` without any
    substitution; one whose paths also reach the withdrawn URI is still
    `DependencyStatus.MIRRORABLE` when at least one approved mirror is
    itself present in the registry (use `redirect` to apply the
    substitution), and is `DependencyStatus.DEPENDENT` otherwise.
    """
    entrypoints = tuple(sorted(registry))
    registry = registry.crawl()
    mirror_uris = HashTrieSet(mirrors)
    ingested_mirrors = HashTrieSet(
        mirror for mirror in mirror_uris if mirror in registry
    )
    watched = HashTrieSet((withdrawn,)).update(mirror_uris)

    impacts = HashTrieMap()
    for entrypoint in entrypoints:
        references = _trace(
            registry=registry,
            entrypoint=entrypoint,
            watched=watched,
        )
        impacts = impacts.insert(
            entrypoint,
            Impact(
                status=_classify(
                    withdrawn=withdrawn,
                    ingested_mirrors=ingested_mirrors,
                    references=references,
                ),
                references=references,
            ),
        )
    return ImpactReport(
        withdrawn=withdrawn,
        mirrors=tuple(sorted(mirror_uris)),
        impacts=impacts,
    )


def redirect(registry, withdrawn: URI, mirror: URI):
    """
    Return a registry where the withdrawn URI is served by a mirror.

    The mirror resource must already be present in the registry. The
    returned registry resolves the withdrawn URI to the mirror's contents,
    demonstrating the approved substitution without mutating any registered
    resource.
    """
    registry = registry.crawl()
    if mirror not in registry:
        raise exceptions.NoSuchResource(ref=mirror)
    return registry.with_resource(withdrawn, registry[mirror])


def _classify(
    withdrawn: URI,
    ingested_mirrors: HashTrieSet[URI],
    references: tuple[Reference, ...],
) -> DependencyStatus:
    if not references:
        return DependencyStatus.UNRELATED
    reaches_withdrawn = any(each.target == withdrawn for each in references)
    if not reaches_withdrawn or ingested_mirrors:
        return DependencyStatus.MIRRORABLE
    return DependencyStatus.DEPENDENT


def _trace(
    registry,
    entrypoint: URI,
    watched: HashTrieSet[URI],
) -> tuple[Reference, ...]:
    found: list[Reference] = []
    seen: set[URI] = set()
    stack: list[tuple[URI, tuple[URI, ...]]] = [(entrypoint, (entrypoint,))]
    while stack:
        uri, via = stack.pop()
        if uri in seen:
            continue
        seen.add(uri)
        resource = registry[uri]

        for ref, pointer, base in _iter_refs(resource.contents, base=uri):
            target, fragment = urldefrag(urljoin(base, ref))
            target = target.rstrip("#")
            if target in watched:
                found.append(
                    Reference(
                        source=uri,
                        target=target,
                        fragment=fragment,
                        pointer=pointer,
                        ref=ref,
                        via=via,
                    ),
                )
            if target not in seen and target in registry:
                stack.append((target, (*via, target)))
    return tuple(found)


def _iter_refs(
    contents,
    base: URI,
    pointer: tuple[str | int, ...] = (),
):
    if isinstance(contents, Mapping):
        for base_keyword in _BASE_KEYWORDS:
            declared = contents.get(base_keyword)
            if isinstance(declared, str) and not declared.startswith("#"):
                base = urljoin(base, declared)
                break
        for ref_keyword in _REF_KEYWORDS:
            value = contents.get(ref_keyword)
            if isinstance(value, str):
                yield (
                    value,
                    _encode_pointer(
                        (*pointer, ref_keyword),
                    ),
                    base,
                )
        for key, value in contents.items():
            if key not in _REF_KEYWORDS:
                yield from _iter_refs(value, base, (*pointer, key))
    elif isinstance(contents, Sequence) and not isinstance(
        contents,
        (str, bytes),
    ):
        for index, item in enumerate(contents):
            yield from _iter_refs(item, base, (*pointer, index))


def _encode_pointer(segments: tuple[str | int, ...]) -> str:
    return "".join(
        "/" + str(segment).replace("~", "~0").replace("/", "~1")
        for segment in segments
    )
