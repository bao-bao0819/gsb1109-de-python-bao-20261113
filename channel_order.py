"""Reconcile truncated per-store version leaderboards under one ranking rule.

Each store kept only the top entries of a version leaderboard, so the
original ranking configuration is gone. An entry is described by a semantic
version and a distribution channel.

The only ranking rule ("common policy") that may have produced every
observed fragment is:

1. standard semantic-version precedence as the primary key; and
2. one global channel precedence table used *only* to break ties between
   entries whose versions have equal precedence.

Build metadata never affects semantic-version precedence, so two versions
that differ only in build metadata compare equal and therefore rely on the
channel table. The channel table is a strict total order, i.e. it must be
acyclic.

Observations are prefixes: an entry that was truncated away is not
considered last, so nothing may be inferred from its absence.

The module derives the channel constraints that every explaining policy
must satisfy, reports the shortest contradiction cycle when no policy
exists, and predicts the order of two new records only when every allowed
policy agrees.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from semver import Version

__all__ = [
    "Record",
    "Reconciliation",
    "PrecedenceConflict",
    "analyze",
    "topological_sort",
    "shortest_cycle",
    "transitive_reduction",
]


@dataclass(frozen=True)
class Record:
    """One leaderboard entry: a semantic version on a distribution channel."""

    version: Version
    channel: str

    @classmethod
    def coerce(cls, value: "Record | Mapping | Sequence | object") -> "Record":
        if isinstance(value, Record):
            return value
        if isinstance(value, Mapping):
            try:
                version = value["version"]
                channel = value["channel"]
            except KeyError as exc:
                raise ValueError(
                    f"mapping record missing key {exc.args[0]!r}"
                ) from None
        elif isinstance(value, (tuple, list)):
            if len(value) != 2:
                raise ValueError("sequence record must be (version, channel)")
            version, channel = value
        else:
            version = getattr(value, "version")
            channel = getattr(value, "channel")
        if not isinstance(version, Version):
            version = Version.parse(str(version))
        return cls(version=version, channel=str(channel))

    def __str__(self) -> str:
        return f"{self.version}@{self.channel}"


class PrecedenceConflict(ValueError):
    """An observation inverts standard semantic-version precedence.

    The channel table only breaks ties, so this kind of inversion cannot be
    explained by any allowed policy. It is attached to the infeasible
    :class:`Reconciliation` as ``precedence_conflict``.
    """

    def __init__(self, earlier: Record, later: Record) -> None:
        self.earlier = earlier
        self.later = later
        super().__init__(
            f"observed order {earlier} before {later} contradicts standard "
            "version precedence; no channel table can override it"
        )


def _coerce_observation(observation: Iterable[object]) -> list[Record]:
    return [Record.coerce(item) for item in observation]


def _adjacency(edges: Iterable[tuple[str, str]]) -> dict[str, set[str]]:
    graph: dict[str, set[str]] = defaultdict(set)
    for source, target in edges:
        graph[source].add(target)
        graph.setdefault(target, set())
        graph.setdefault(source, set())
    return graph


def topological_sort(edges: Iterable[tuple[str, str]]) -> list[str]:
    """Return a topological ordering of the channel graph.

    Raises :class:`ValueError` when the graph contains a cycle.
    """
    graph = _adjacency(edges)
    indegree = {node: 0 for node in graph}
    for targets in graph.values():
        for target in targets:
            indegree[target] += 1
    ready = deque(sorted(node for node, degree in indegree.items() if degree == 0))
    order: list[str] = []
    while ready:
        node = ready.popleft()
        order.append(node)
        for target in sorted(graph[node]):
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
    if len(order) != len(graph):
        raise ValueError("channel constraints contain a cycle")
    return order


def shortest_cycle(edges: Iterable[tuple[str, str]]) -> list[str] | None:
    """Return the shortest directed cycle as a closed list of channels.

    For a cycle ``a -> b -> c -> a`` the result is ``[a, b, c, a]``.
    Ties are broken deterministically: the cycle is rotated to start at its
    lexicographically smallest channel, then the smallest such tuple wins.
    Returns ``None`` when the graph is acyclic.
    """
    graph = _adjacency(edges)
    candidates: list[list[str]] = []

    def canonical(cycle: list[str]) -> list[str]:
        nodes = cycle[:-1]
        pivot = min(range(len(nodes)), key=lambda idx: nodes[idx])
        rotated = nodes[pivot:] + nodes[:pivot]
        return [*rotated, rotated[0]]

    for start in graph:
        queue = deque([(start, [start])])
        seen = {start}
        while queue:
            node, path = queue.popleft()
            if candidates and len(path) >= len(candidates[0]):
                continue
            for target in sorted(graph[node]):
                if target == start:
                    candidates.append(canonical([*path, start]))
                    break
                if target not in seen:
                    seen.add(target)
                    queue.append((target, [*path, target]))
    if not candidates:
        return None
    return min(candidates)


def transitive_reduction(edges: Iterable[tuple[str, str]]) -> set[tuple[str, str]]:
    """Remove edges already implied by another path (valid for DAGs)."""
    edge_set = set(edges)
    graph = _adjacency(edge_set)

    def reaches(source: str, target: str, blocked: tuple[str, str]) -> bool:
        queue = deque([source])
        visited = {source}
        while queue:
            node = queue.popleft()
            for nxt in graph[node]:
                if (node, nxt) == blocked:
                    continue
                if nxt == target:
                    return True
                if nxt not in visited:
                    visited.add(nxt)
                    queue.append(nxt)
        return False

    return {
        (source, target)
        for source, target in edge_set
        if not reaches(source, target, (source, target))
    }


def _reachability(graph: Mapping[str, set[str]]) -> dict[str, set[str]]:
    reachable: dict[str, set[str]] = {}
    for start in graph:
        seen: set[str] = set()
        queue = deque(graph[start])
        while queue:
            node = queue.popleft()
            if node in seen:
                continue
            seen.add(node)
            queue.extend(graph[node])
        reachable[start] = seen
    return reachable


@dataclass(frozen=True)
class Reconciliation:
    """Result of checking whether one common policy explains every fragment."""

    feasible: bool
    channels: tuple[str, ...]
    edges: frozenset[tuple[str, str]]
    minimal_edges: frozenset[tuple[str, str]]
    cycle: tuple[str, ...] | None
    precedence_conflict: "PrecedenceConflict | None"

    @staticmethod
    def _conflict(
        channels: set[str],
        edges: set[tuple[str, str]],
        conflict: "PrecedenceConflict",
    ) -> "Reconciliation":
        cycle = shortest_cycle(edges)
        reduced = transitive_reduction(edges) if cycle is None else frozenset()
        return Reconciliation(
            feasible=False,
            channels=tuple(sorted(channels)),
            edges=frozenset(edges),
            minimal_edges=frozenset(reduced),
            cycle=tuple(cycle) if cycle is not None else None,
            precedence_conflict=conflict,
        )

    def necessarily_before(self, channel_a: str, channel_b: str) -> bool:
        """True when every allowed channel table ranks ``channel_a`` first."""
        if not self.feasible:
            raise ValueError("no allowed policy exists; constraints are contradictory")
        if channel_a == channel_b:
            return False
        graph = _adjacency(self.edges)
        return channel_b in _reachability(graph).get(channel_a, set())

    def mandatory_relations(self) -> frozenset[tuple[str, str]]:
        """All channel pairs whose relative order is forced by the observations.

        This is the transitive closure of the constraints: each returned
        pair ``(a, b)`` ranks ``a`` before ``b`` under every allowed table.
        """
        if not self.feasible:
            raise ValueError("no allowed policy exists; constraints are contradictory")
        reachable = _reachability(_adjacency(self.edges))
        return frozenset(
            (source, target)
            for source, targets in reachable.items()
            for target in targets
        )

    def predict(self, first: object, second: object) -> str | None:
        """Predict the order of two new records.

        Returns ``"before"`` when *first* precedes *second* under every
        allowed policy, ``"after"`` for the reverse, ``"equal"`` when no
        policy can separate them, and ``None`` when the allowed policies
        disagree (the prediction is not certain).
        """
        record_a = Record.coerce(first)
        record_b = Record.coerce(second)
        if not self.feasible:
            raise ValueError("no allowed policy exists; constraints are contradictory")
        comparison = record_a.version.compare(record_b.version)
        if comparison > 0:
            return "before"
        if comparison < 0:
            return "after"
        if record_a.channel == record_b.channel:
            return "equal"
        if self.necessarily_before(record_a.channel, record_b.channel):
            return "before"
        if self.necessarily_before(record_b.channel, record_a.channel):
            return "after"
        return None

    def allowed_tables(self) -> list[list[str]]:
        """All channel total orders (linear extensions) consistent with observations."""
        if not self.feasible:
            return []
        graph = _adjacency(self.edges)
        for channel in self.channels:
            graph.setdefault(channel, set())
        prerequisites: dict[str, set[str]] = {node: set() for node in graph}
        for source, targets in graph.items():
            for target in targets:
                prerequisites[target].add(source)
        channels = sorted(graph)
        results: list[list[str]] = []

        def extend(order: list[str], remaining: set[str]) -> None:
            if not remaining:
                results.append(order.copy())
                return
            placed = set(order)
            for node in sorted(remaining):
                if prerequisites[node] <= placed:
                    order.append(node)
                    extend(order, remaining - {node})
                    order.pop()

        extend([], set(channels))
        return results


def analyze(observations: Iterable[Iterable[object]]) -> Reconciliation:
    """Derive the common-policy constraints behind truncated leaderboards.

    Each observation is the retained prefix of one store's leaderboard,
    ordered best first. An observation only constrains the channel table
    for pairs whose versions have equal standard precedence; pairs with
    different precedence are already decided by the primary key and only
    matter when their observed order is outright impossible.
    """
    channels: set[str] = set()
    edges: set[tuple[str, str]] = set()
    precedence_conflict: PrecedenceConflict | None = None

    for raw_observation in observations:
        observation = _coerce_observation(raw_observation)
        channels.update(record.channel for record in observation)
        for index, earlier in enumerate(observation):
            for later in observation[index + 1 :]:
                comparison = earlier.version.compare(later.version)
                if comparison < 0:
                    precedence_conflict = PrecedenceConflict(earlier, later)
                    break
                if comparison == 0 and earlier.channel != later.channel:
                    edges.add((earlier.channel, later.channel))
            if precedence_conflict is not None:
                break
        if precedence_conflict is not None:
            break

    if precedence_conflict is not None:
        return Reconciliation._conflict(channels, edges, precedence_conflict)

    cycle = shortest_cycle(edges)
    if cycle is not None:
        return Reconciliation(
            feasible=False,
            channels=tuple(sorted(channels)),
            edges=frozenset(edges),
            minimal_edges=frozenset(),
            cycle=tuple(cycle),
            precedence_conflict=None,
        )
    return Reconciliation(
        feasible=True,
        channels=tuple(sorted(channels)),
        edges=frozenset(edges),
        minimal_edges=frozenset(transitive_reduction(edges)),
        cycle=None,
        precedence_conflict=None,
    )


def _demo() -> None:  # pragma: no cover - illustrative only
    # Three stores kept short prefixes of their leaderboards. Versions that
    # differ only in build metadata share one precedence class, so their
    # order can only come from the channel table.
    observations = [
        [("1.2.0+app", "web"), ("1.2.0+app", "store"), ("1.1.0", "kiosk")],
        [("1.2.0+x", "store"), ("1.2.0+y", "partner")],
        [("1.2.0+z", "web"), ("1.2.0+w", "partner"), ("0.9.9", "kiosk")],
    ]
    result = analyze(observations)
    if not result.feasible:
        if result.cycle is not None:
            print("contradiction cycle:", " -> ".join(result.cycle))
        else:
            conflict = result.precedence_conflict
            print("precedence conflict:", conflict)
        return
    print("mandatory channel constraints:")
    for source, target in sorted(result.minimal_edges):
        print(f"  {source} before {target}")
    print("allowed channel tables:", result.allowed_tables())
    for pair in [
        (("1.3.0", "web"), ("1.2.0", "store")),
        (("1.2.0+a", "web"), ("1.2.0+b", "store")),
        (("1.2.0+a", "partner"), ("1.2.0+b", "kiosk")),
    ]:
        print(pair, "->", result.predict(*pair))


if __name__ == "__main__":  # pragma: no cover
    _demo()
