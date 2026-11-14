"""Tests for the common-policy leaderboard reconciler.

The checks cross-validate :mod:`channel_order` against a brute-force
reference that enumerates every possible channel total order.
"""

from itertools import permutations

import pytest

from channel_order import (
    PrecedenceConflict,
    Record,
    Reconciliation,
    analyze,
    shortest_cycle,
    topological_sort,
    transitive_reduction,
)


def _records(observation):
    return [Record.coerce(item) for item in observation]


def _brute_force_orders(observations, channels):
    """Every channel permutation that explains all observed prefixes."""
    coerced = [_records(obs) for obs in observations]
    valid = []
    for perm in permutations(sorted(channels)):
        rank = {channel: index for index, channel in enumerate(perm)}
        ok = True
        for observation in coerced:
            for i, earlier in enumerate(observation):
                for later in observation[i + 1 :]:
                    comparison = earlier.version.compare(later.version)
                    if comparison < 0:
                        ok = False
                        break
                    if (
                        comparison == 0
                        and earlier.channel != later.channel
                        and rank[earlier.channel] > rank[later.channel]
                    ):
                        ok = False
                        break
                if not ok:
                    break
            if not ok:
                break
        if ok:
            valid.append(perm)
    return valid


def _brute_force_mandatory(valid_orders):
    mandatory = set()
    channels = valid_orders[0]
    for source in channels:
        for target in channels:
            if source == target:
                continue
            if all(order.index(source) < order.index(target) for order in valid_orders):
                mandatory.add((source, target))
    return mandatory


def _closure(edges):
    graph: dict[str, set[str]] = {}
    for source, target in edges:
        graph.setdefault(source, set()).add(target)
        graph.setdefault(target, set())
    closure = set()
    for start in graph:
        stack = list(graph[start])
        seen = set()
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            closure.add((start, node))
            stack.extend(graph[node])
    return closure


def test_build_metadata_shares_one_precedence_class():
    result = analyze(
        [
            [("1.2.0+web.1", "web"), ("1.2.0+store.2", "store")],
            [("1.2.0+store.3", "store"), ("1.2.0+partner.4", "partner")],
        ]
    )
    assert result.feasible
    assert result.minimal_edges == frozenset({("web", "store"), ("store", "partner")})
    assert result.edges == frozenset({("web", "store"), ("store", "partner")})
    assert result.allowed_tables() == [["web", "store", "partner"]]


def test_truncated_tail_carries_no_information():
    # The kiosk entry is cut off in the second fragment; it must not be
    # treated as last place or constrain anything.
    result = analyze(
        [
            [("2.0.0", "web"), ("1.0.0", "store"), ("1.0.0", "kiosk")],
            [("2.0.0", "web"), ("1.0.0", "store")],
        ]
    )
    assert result.feasible
    assert result.minimal_edges == frozenset({("store", "kiosk")})
    assert set(result.channels) == {"web", "store", "kiosk"}


def test_primary_precedence_needs_no_channel_edge():
    result = analyze([[("2.0.0", "web"), ("1.0.0", "store")]])
    assert result.feasible and result.edges == frozenset()
    # Either channel order is allowed.
    assert sorted(result.allowed_tables()) == sorted(
        [["store", "web"], ["web", "store"]]
    )


def test_observation_inverting_standard_precedence_is_impossible():
    result = analyze([[("1.0.0", "web"), ("2.0.0", "store")]])
    assert not result.feasible
    assert result.cycle is None
    assert isinstance(result.precedence_conflict, PrecedenceConflict)
    assert result.precedence_conflict.earlier.channel == "web"
    assert result.precedence_conflict.later.channel == "store"
    # A release candidate cannot precede its own release: rc < release.
    rc_result = analyze([[("1.0.0-rc.1", "a"), ("1.0.0", "b")]])
    assert not rc_result.feasible
    assert isinstance(rc_result.precedence_conflict, PrecedenceConflict)


def test_contradiction_cycle_across_fragments_is_reported_shortest():
    result = analyze(
        [
            [("1.0.0", "a"), ("1.0.0", "b")],
            [("1.0.0", "b"), ("1.0.0", "c")],
            [("1.0.0", "c"), ("1.0.0", "a")],
        ]
    )
    assert not result.feasible
    assert result.cycle == ("a", "b", "c", "a")
    assert result.precedence_conflict is None
    with pytest.raises(ValueError):
        topological_sort(result.edges)


def test_short_cycle_preferred_over_long_one():
    edges = [("a", "b"), ("b", "a"), ("b", "c"), ("c", "d"), ("d", "b")]
    cycle = shortest_cycle(edges)
    assert cycle == ["a", "b", "a"]


def test_prerelease_comparison_follows_semver():
    # alpha < beta < rc < release, so a descending fragment is impossible.
    result = analyze(
        [[("1.0.0", "stable"), ("1.0.0-rc.1", "rc"), ("1.0.0-beta.1", "beta")]]
    )
    assert result.feasible and result.edges == frozenset()


def test_prediction_is_certain_only_under_unanimous_policies():
    result = analyze(
        [
            [("1.0.0", "web"), ("1.0.0", "store")],
        ]
    )
    # Different precedence classes: the primary key decides everywhere.
    assert result.predict(("3.0.0", "store"), ("2.0.0", "web")) == "before"
    assert result.predict(("2.0.0", "web"), ("3.0.0", "store")) == "after"
    # Tied precedence with a mandatory channel relation.
    assert result.predict(("1.0.0+x", "web"), ("1.0.0+y", "store")) == "before"
    assert result.predict(("1.0.0+x", "store"), ("1.0.0+y", "web")) == "after"
    # Unconstrained channel pair: the allowed tables disagree.
    assert result.predict(("1.0.0+x", "store"), ("1.0.0+y", "kiosk")) is None
    # Same channel, same precedence class: no table can separate them.
    assert result.predict(("1.0.0+x", "web"), ("1.0.0+y", "web")) == "equal"


def test_prediction_requires_feasibility():
    result = analyze(
        [
            [("1.0.0", "a"), ("1.0.0", "b")],
            [("1.0.0", "b"), ("1.0.0", "a")],
        ]
    )
    assert not result.feasible
    with pytest.raises(ValueError):
        result.predict(("1.0.0", "a"), ("1.0.0", "b"))


def test_transitive_reduction_keeps_exactly_non_redundant_edges():
    edges = {("a", "b"), ("b", "c"), ("a", "c"), ("c", "d"), ("a", "d")}
    reduced = transitive_reduction(edges)
    assert reduced == {("a", "b"), ("b", "c"), ("c", "d")}
    assert _closure(reduced) == _closure(edges)


def test_mandatory_relations_is_the_transitive_closure():
    result = analyze(
        [
            [("1.0.0", "a"), ("1.0.0", "b")],
            [("1.0.0", "b"), ("1.0.0", "c")],
        ]
    )
    assert result.feasible
    assert result.mandatory_relations() == frozenset(
        {("a", "b"), ("b", "c"), ("a", "c")}
    )
    assert result.necessarily_before("a", "c")
    assert not result.necessarily_before("c", "a")
    infeasible = analyze(
        [
            [("1.0.0", "a"), ("1.0.0", "b")],
            [("1.0.0", "b"), ("1.0.0", "a")],
        ]
    )
    with pytest.raises(ValueError):
        infeasible.mandatory_relations()


def test_record_coercion_forms_and_value_semantics():
    tuple_record = Record.coerce(("1.2.3-rc.1+build.9", "web"))
    mapping_record = Record.coerce(
        {"version": "1.2.3-rc.1+build.9", "channel": "web"}
    )
    object_record = Record.coerce(tuple_record)
    assert tuple_record == mapping_record == object_record
    assert str(tuple_record) == "1.2.3-rc.1+build.9@web"
    with pytest.raises(ValueError):
        Record.coerce(("1.0.0",))
    with pytest.raises(ValueError):
        Record.coerce({"version": "1.0.0"})


@pytest.mark.parametrize("seed", range(80))
def test_matches_brute_force_on_random_fragments(seed):
    import random

    rng = random.Random(seed)
    channels = [f"ch{i}" for i in range(rng.randint(2, 5))]
    versions = [
        "1.0.0",
        "1.0.0+alt",
        "1.0.0-rc.1",
        "1.1.0",
        "2.0.0-alpha",
        "2.0.0",
    ]
    observations = []
    for _ in range(rng.randint(1, 4)):
        length = rng.randint(2, 4)
        fragment = [
            (rng.choice(versions), rng.choice(channels)) for _ in range(length)
        ]
        observations.append(fragment)

    result = analyze(observations)
    observed = {channel for fragment in observations for _, channel in fragment}
    valid_orders = _brute_force_orders(observations, observed)

    if valid_orders:
        assert isinstance(result, Reconciliation)
        assert result.feasible
        assert set(result.channels) == observed
        mandatory = _brute_force_mandatory(valid_orders)
        assert _closure(result.edges) == mandatory
        # Enumerating the allowed linear extensions gives the same tables.
        assert sorted(map(tuple, result.allowed_tables())) == sorted(valid_orders)
    else:
        assert not result.feasible
        assert result.cycle is not None or result.precedence_conflict is not None
        if result.cycle is not None:
            # The reported cycle really closes and every step is an edge.
            edge_set = set(result.edges)
            cycle = result.cycle
            assert cycle[0] == cycle[-1]
            assert all(
                (cycle[i], cycle[i + 1]) in edge_set for i in range(len(cycle) - 1)
            )

    if result.feasible:
        observed_channels = sorted(observed)
        sample_pairs = [
            ((rng.choice(versions), rng.choice(observed_channels)),
             (rng.choice(versions), rng.choice(observed_channels)))
            for _ in range(6)
        ]
        for raw_a, raw_b in sample_pairs:
            record_a = Record.coerce(raw_a)
            record_b = Record.coerce(raw_b)
            verdict = result.predict(raw_a, raw_b)
            comparison = record_a.version.compare(record_b.version)
            if comparison != 0:
                assert verdict == ("after" if comparison < 0 else "before")
            elif record_a.channel == record_b.channel:
                assert verdict == "equal"
            else:
                before = all(
                    order.index(record_a.channel) < order.index(record_b.channel)
                    for order in valid_orders
                )
                after = all(
                    order.index(record_b.channel) < order.index(record_a.channel)
                    for order in valid_orders
                )
                if before:
                    assert verdict == "before"
                elif after:
                    assert verdict == "after"
                else:
                    assert verdict is None


def test_demo_scenario_from_module_docstring():
    observations = [
        [("1.2.0+app", "web"), ("1.2.0+app", "store"), ("1.1.0", "kiosk")],
        [("1.2.0+x", "store"), ("1.2.0+y", "partner")],
        [("1.2.0+z", "web"), ("1.2.0+w", "partner"), ("0.9.9", "kiosk")],
    ]
    result = analyze(observations)
    assert result.feasible
    assert result.minimal_edges == frozenset(
        {("web", "store"), ("store", "partner")}
    )
    assert result.predict(("1.3.0", "web"), ("1.2.0", "store")) == "before"
    assert result.predict(("1.2.0+a", "web"), ("1.2.0+b", "store")) == "before"
    assert result.predict(("1.2.0+a", "partner"), ("1.2.0+b", "kiosk")) is None
