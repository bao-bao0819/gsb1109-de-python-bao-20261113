"""
Planning of release tags across parallel release lines.

A release line (for example an old maintenance branch or a current feature
branch) registers release candidates against a shared registry of version
numbers. The planner picks the next free tag for every line, respects the
priority of the competing lines and lets consumers declare that one
candidate must be released after another.

Numbers that were already sent to customers can never be handed out again,
even when the release was withdrawn. Numbers that were only reserved or
revoked before publication are blocked as well. Build metadata is ignored
for precedence and never emitted, so a ``+build`` suffix cannot be used to
fake a higher priority.
"""

from dataclasses import dataclass
from typing import (
    AbstractSet,
    Any,
    FrozenSet,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Union,
    cast,
)

from .version import Version

#: Something that can be interpreted as a :class:`~semver.version.Version`.
VersionLike = Union[str, Version]

#: A ``(later, earlier)`` pair: the ``later`` line must be released after
#: the ``earlier`` line.
Dependency = Tuple[str, str]


class ReleasePlanError(ValueError):
    """Raised when release lines or their constraints are invalid."""


class ReleaseConstraintsError(ReleasePlanError):
    """
    Raised when release constraints lock each other and no plan exists.

    :ivar blockers: lines involved in the contradictory constraint. For a
        circular dependency they are given in cycle order with the first
        line repeated at the end; for an unbridgeable ordering constraint
        they are the ``(later, earlier)`` pair.
    """

    def __init__(self, message: str, blockers: Sequence[str] = ()):
        super().__init__(message)
        self.blockers: Tuple[str, ...] = tuple(blockers)


@dataclass(slots=True)
class ReleaseLine:
    """
    A stream of releases derived from a base version.

    :param name: unique name of the release line
    :param base: latest version the line starts from; parsed with
        :meth:`~semver.version.Version.parse` when given as a string
    :param priority: higher values are planned first and claim the lowest
        free tag when lines compete
    :param prerelease_token: prerelease token of the planned candidates.
        Set to ``None`` to plan final releases instead of prereleases.
    """

    name: str
    base: VersionLike
    priority: int = 0
    prerelease_token: Optional[str] = "rc"

    def __post_init__(self) -> None:
        if not isinstance(self.base, Version):
            self.base = Version.parse(str(self.base))
        self.name = str(self.name)
        self.priority = int(self.priority)


@dataclass(slots=True)
class ReleaseCandidate:
    """A single tag planned for a :class:`ReleaseLine`."""

    line: str
    base: Version
    planned: Version
    priority: int

    def __str__(self) -> str:
        return f"{self.line}: {self.planned}"


@dataclass(slots=True)
class ReleaseRegistry:
    """
    Version numbers that can no longer be freely assigned.

    :param used: versions already published to customers. They can never be
        assigned again, even when the release was withdrawn afterwards.
    :param blocked: versions reserved or revoked before publication. They
        are skipped like used versions but were never public.
    """

    used: FrozenSet[Version] = frozenset()
    blocked: FrozenSet[Version] = frozenset()

    def __post_init__(self) -> None:
        self.used = frozenset(_as_version(value) for value in self.used)
        self.blocked = frozenset(_as_version(value) for value in self.blocked)

    @property
    def consumed(self) -> FrozenSet[Version]:
        """All numbers the planner must not assign."""
        return self.used | self.blocked


def _as_version(value: VersionLike) -> Version:
    return value if isinstance(value, Version) else Version.parse(str(value))


def _coerce_line(raw: Union[ReleaseLine, Mapping[str, object]]) -> ReleaseLine:
    if isinstance(raw, ReleaseLine):
        return raw
    if isinstance(raw, Mapping):
        try:
            prerelease_token = raw.get("prerelease_token", "rc")
            return ReleaseLine(
                name=str(raw["name"]),
                base=raw["base"],  # type: ignore[arg-type]
                priority=int(cast(Any, raw.get("priority", 0))),
                prerelease_token=prerelease_token,  # type: ignore[arg-type]
            )
        except KeyError as exc:
            raise ReleasePlanError(
                f"release line mapping is missing the key {exc.args[0]!r}"
            ) from exc
    raise ReleasePlanError(f"cannot interpret release line {raw!r}")


def _find_cycle(graph: Mapping[str, AbstractSet[str]]) -> List[str]:
    stack: List[str] = []
    on_stack: set[str] = set()

    def visit(node: str) -> Optional[List[str]]:
        stack.append(node)
        on_stack.add(node)
        for nxt in sorted(graph[node]):
            if nxt in on_stack:
                start = stack.index(nxt)
                return stack[start:] + [nxt]
            cycle = visit(nxt)
            if cycle is not None:
                return cycle
        stack.pop()
        on_stack.remove(node)
        return None

    for node in sorted(graph):
        cycle = visit(node)
        if cycle is not None:
            return cycle
    return []


def _base(line: ReleaseLine) -> Version:
    return cast(Version, line.base)


def _first_candidate(line: ReleaseLine) -> Version:
    base = _base(line)
    if line.prerelease_token is not None:
        return base.next_version("prerelease", line.prerelease_token)
    return base.next_version("patch")


def _next_candidate(version: Version, token: Optional[str]) -> Version:
    if token is not None:
        return version.next_version("prerelease", token)
    return version.bump_patch()


def plan_releases(
    lines: Iterable[Union[ReleaseLine, Mapping[str, object]]],
    *,
    registry: Optional[ReleaseRegistry] = None,
    dependencies: Iterable[Dependency] = (),
) -> List[ReleaseCandidate]:
    """
    Plan the next release tag of every release line.

    Lines are handled in priority order (highest first, name as tie breaker)
    and each receives the smallest tag that is greater than its base version,
    not consumed by the *registry* and compatible with its *dependencies*.
    A dependency given as ``("later", "earlier")`` guarantees that the
    planned tag of ``later`` is strictly greater than the one of ``earlier``.

    A line stays on the major version of its base version. Asking it to be
    later than a line planned on a higher major raises
    :class:`ReleaseConstraintsError`, as do circular dependencies.

    :param lines: release lines, as :class:`ReleaseLine` objects or mappings
        with the keys ``name``, ``base``, ``priority`` and
        ``prerelease_token``
    :param registry: published and reserved or revoked version numbers
    :param dependencies: ``(later, earlier)`` ordering constraints
    :return: planned candidates in planning (priority) order
    :raises ReleasePlanError: for unknown line names or duplicate lines
    :raises ReleaseConstraintsError: for circular or unbridgeable constraints

    >>> legacy = ReleaseLine("legacy-2.x", "2.4.7", priority=10)
    >>> feature = ReleaseLine("feature-2.x", "2.5.0-rc.2", priority=20)
    >>> registry = ReleaseRegistry(
    ...     used=["2.4.8-rc.1"], blocked=["2.4.8-rc.2", "2.5.0-rc.3"]
    ... )
    >>> plan = plan_releases(
    ...     [legacy, feature],
    ...     registry=registry,
    ...     dependencies=[("legacy-2.x", "feature-2.x")],
    ... )
    >>> [(candidate.line, str(candidate.planned)) for candidate in plan]
    [('feature-2.x', '2.5.0-rc.4'), ('legacy-2.x', '2.5.0-rc.5')]
    """
    registry = registry if registry is not None else ReleaseRegistry()
    consumed = registry.consumed

    line_map: dict[str, ReleaseLine] = {}
    for raw in lines:
        line = _coerce_line(raw)
        if line.name in line_map:
            raise ReleasePlanError(f"duplicate release line {line.name!r}")
        line_map[line.name] = line

    prerequisites: dict[str, set[str]] = {name: set() for name in line_map}
    for later, earlier in dependencies:
        if later not in line_map or earlier not in line_map:
            raise ReleasePlanError(
                f"dependency ({later!r}, {earlier!r}) references an unknown "
                "release line"
            )
        if later == earlier:
            raise ReleaseConstraintsError(
                f"release line {later!r} cannot be later than itself",
                blockers=[later, later],
            )
        prerequisites[later].add(earlier)

    order = _topological_order(line_map, prerequisites)

    candidates: dict[str, ReleaseCandidate] = {}
    claimed: set[Version] = set(consumed)
    for name in order:
        line = line_map[name]
        target = _first_candidate(line)
        target = _respect_dependencies(line, target, prerequisites[name], candidates)
        target = _skip_consumed(line.prerelease_token, target, claimed, registry.used)
        claimed.add(target)
        candidates[name] = ReleaseCandidate(
            line=name,
            base=_base(line),
            planned=target,
            priority=line.priority,
        )
    return [candidates[name] for name in order]


def _topological_order(
    line_map: Mapping[str, ReleaseLine],
    prerequisites: Mapping[str, AbstractSet[str]],
) -> List[str]:
    remaining = {name: set(prereq) for name, prereq in prerequisites.items()}
    order: List[str] = []
    while remaining:
        ready = [name for name, prereq in remaining.items() if not prereq]
        if not ready:
            cycle = _find_cycle(remaining)
            raise ReleaseConstraintsError(
                "release lines are locked in a circular 'must be later "
                f"than' constraint: {' -> '.join(cycle)}",
                blockers=cycle,
            )
        ready.sort(key=lambda name: (-line_map[name].priority, name))
        chosen = ready[0]
        order.append(chosen)
        remaining.pop(chosen)
        for prereq in remaining.values():
            prereq.discard(chosen)
    return order


def _respect_dependencies(
    line: ReleaseLine,
    target: Version,
    prerequisites: AbstractSet[str],
    candidates: Mapping[str, ReleaseCandidate],
) -> Version:
    if not prerequisites:
        return target
    bound = max(candidates[name].planned for name in prerequisites)
    if bound < target:
        return target
    if bound.major > target.major:
        blocker = max(
            prerequisites, key=lambda name: candidates[name].planned
        )
        raise ReleaseConstraintsError(
            f"release line {line.name!r} stays on major {target.major} but "
            f"must be later than line {blocker!r} planned as {bound}, which "
            "is on a higher major",
            blockers=[line.name, blocker],
        )
    return _next_candidate(bound, line.prerelease_token)


def _first_prerelease(core: Version, token: Optional[str]) -> Version:
    if token is not None:
        prerelease = f"{token}.1" if token else "1"
        return core.replace(prerelease=prerelease, build=None)
    return core


def _skip_consumed(
    token: Optional[str],
    target: Version,
    consumed: AbstractSet[Version],
    used: FrozenSet[Version],
) -> Version:
    # A published final release seals the whole core version: any
    # prerelease beneath it would be older than a public release.
    while target.prerelease is not None and target.finalize_version() in used:
        target = _first_prerelease(target.finalize_version().bump_patch(), token)
    while target in consumed:
        target = _next_candidate(target, token)
    return target
