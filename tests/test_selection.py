import itertools
import json
import random
from io import StringIO

import pytest

from sqlite_fts4.selection import (
    Candidate,
    SelectionError,
    candidates_from_records,
    main,
    result_to_dict,
    select_candidates,
)


def make(candidate_id, topics, source=None, relevance=0.0):
    return Candidate(
        doc_id=candidate_id,
        topics=frozenset(topics),
        source=source,
        relevance=relevance,
    )


def assert_invariants(result, candidates, quotas, budget):
    sources = [c.source for c in result.selected if c.source is not None]
    assert len(sources) == len(set(sources))
    assert len({c.doc_id for c in result.selected}) == len(result.selected)
    assert result.reviews_used <= budget
    for topic, count in result.topic_counts.items():
        assert count == sum(
            1 for candidate in result.selected if topic in candidate.topics
        )
        if result.feasible:
            assert count >= quotas[topic]
    assert result.reviews_used == len(result.selected)


def brute_force(candidates, quotas, budget):
    """Return (feasible, best_subset, max_relevance, min_reviews)."""
    best = None
    min_reviews = None
    for size in range(len(candidates) + 1):
        for subset in itertools.combinations(candidates, size):
            sources = [c.source for c in subset if c.source is not None]
            if len(sources) != len(set(sources)):
                continue
            counts = {topic: 0 for topic in quotas}
            for candidate in subset:
                for topic in candidate.topics:
                    counts[topic] += 1
            if any(counts[t] < q for t, q in quotas.items()):
                if min_reviews is None:
                    continue
                continue
            if len(subset) > budget:
                continue
            relevance = sum(c.relevance for c in subset)
            if min_reviews is None or len(subset) < min_reviews:
                min_reviews = len(subset)
            key = (relevance, -len(subset))
            if best is None or key > best[0]:
                best = (key, subset, relevance)
    if best is None:
        return False, None, None, min_reviews
    return True, list(best[1]), best[2], min_reviews


def test_topic_sharing_bills_single_review():
    candidates = [
        make("d0", ["finance", "policy"], relevance=0.9),
        make("a1", ["finance"], relevance=0.8),
        make("b1", ["policy"], relevance=0.7),
        make("a2", ["finance"], relevance=0.6),
    ]
    result = select_candidates(
        candidates, {"finance": 2, "policy": 2}, budget=10
    )
    assert result.feasible
    # Leftover budget is spent on the remaining positive-relevance hits, but
    # the diagnostics still report the cheapest quota-satisfying selection.
    assert result.diagnostics["min_reviews_required"] == 3
    assert result.reviews_used == 4
    assert result.topic_counts == {"finance": 3, "policy": 2}
    assert_invariants(result, candidates, {"finance": 2, "policy": 2}, 10)


def test_source_conflict_allows_only_one_reprint():
    candidates = [
        make("s1", ["finance"], source="wire", relevance=0.9),
        make("s2", ["finance"], source="wire", relevance=0.8),
        make("s3", ["finance"], relevance=0.5),
    ]
    result = select_candidates(candidates, {"finance": 2}, budget=10)
    assert result.feasible
    ids = {c.doc_id for c in result.selected}
    assert not {"s1", "s2"} <= ids
    assert "s1" in ids
    assert result.topic_counts["finance"] == 2


def test_budget_shortfall_reports_gap():
    candidates = [
        make("f{}".format(i), ["finance"], relevance=0.5) for i in range(5)
    ] + [make("p{}".format(i), ["policy"], relevance=0.5) for i in range(4)]
    result = select_candidates(
        candidates, {"finance": 5, "policy": 4}, budget=6
    )
    assert not result.feasible
    assert sum(result.shortfalls.values()) >= 1
    assert result.diagnostics["min_reviews_required"] == 9
    assert result.diagnostics["budget_gap"] == 3
    assert result.reviews_used <= 6


def test_pool_shortfall_reports_ceiling():
    candidates = [
        make("t1", ["tech"], source="w1"),
        make("t2", ["tech"], source="w2"),
    ]
    result = select_candidates(candidates, {"tech": 3}, budget=10)
    assert not result.feasible
    assert result.shortfalls == {"tech": 1}
    assert result.diagnostics["topic_ceilings"] == {"tech": 2}
    assert result.diagnostics["min_reviews_required"] is None
    assert result.diagnostics["feasible_without_source_conflicts"] is False
    assert result.diagnostics["reachable_without_budget"] is False


def test_source_conflict_is_the_only_blocker():
    candidates = [
        make("x1", ["finance"], source="wire", relevance=0.9),
        make("x2", ["finance"], source="wire", relevance=0.8),
    ]
    result = select_candidates(candidates, {"finance": 2}, budget=5)
    assert not result.feasible
    assert result.shortfalls == {"finance": 1}
    assert result.diagnostics["feasible_without_source_conflicts"] is True
    assert result.diagnostics["reachable_without_budget"] is False
    assert result.diagnostics["conflict_groups"][0]["source"] == "wire"


def test_budget_is_the_only_blocker():
    candidates = [
        make("f{}".format(i), ["finance"], relevance=0.5) for i in range(3)
    ]
    result = select_candidates(candidates, {"finance": 3}, budget=2)
    assert not result.feasible
    assert result.shortfalls == {"finance": 1}
    assert result.diagnostics["min_reviews_required"] == 3
    assert result.diagnostics["budget_gap"] == 1
    assert result.diagnostics["reachable_without_budget"] is True
    assert result.reviews_used == 2


def test_relevance_maximised_among_feasible_sets():
    candidates = [
        make("low", ["finance"], relevance=0.1),
        make("high", ["finance"], relevance=0.99),
        make("mid", ["finance"], relevance=0.5),
    ]
    result = select_candidates(candidates, {"finance": 1}, budget=2)
    assert result.feasible
    ids = [c.doc_id for c in result.selected]
    assert ids[0] == "high"
    assert len(ids) == 2
    assert result.total_relevance == pytest.approx(0.99 + 0.5)


def test_brute_force_cross_check_random_scenarios():
    rng = random.Random(20261114)
    topics = ["finance", "policy", "tech"]
    for scenario in range(200):
        quotas = {
            topic: rng.randint(0, 3) for topic in topics if rng.random() < 0.8
        }
        if not quotas:
            quotas = {topics[0]: 1}
        active_topics = list(quotas)
        candidates = []
        for index in range(rng.randint(1, 10)):
            covered = rng.sample(active_topics, rng.randint(1, min(2, len(active_topics))))
            source = (
                "w{}".format(rng.randint(0, 3))
                if rng.random() < 0.4
                else None
            )
            candidates.append(
                make(
                    "d{}".format(index),
                    covered,
                    source=source,
                    relevance=round(rng.random(), 3),
                )
            )
        budget = rng.randint(0, len(candidates) + 2)
        result = select_candidates(candidates, quotas, budget)
        feasible, subset, relevance, min_reviews = brute_force(
            candidates, quotas, budget
        )
        assert result.feasible == feasible
        assert_invariants(result, candidates, quotas, budget)
        if feasible:
            assert result.total_relevance == pytest.approx(relevance)
            assert (
                result.diagnostics["min_reviews_required"] == min_reviews
            )
            assert len(result.selected) <= budget
        else:
            assert sum(result.shortfalls.values()) > 0


def test_greedy_fallback_respects_constraints():
    candidates = [
        make("d{}".format(i), ["finance" if i % 2 else "policy"], relevance=0.4 + i * 0.05)
        for i in range(8)
    ]
    result = select_candidates(
        candidates, {"finance": 2, "policy": 2}, budget=5, state_limit=1
    )
    assert result.optimal is False
    assert result.feasible
    assert result.diagnostics["mode"] == "greedy"
    assert_invariants(result, candidates, {"finance": 2, "policy": 2}, 5)


def test_validation_errors():
    with pytest.raises(SelectionError):
        Candidate(doc_id="x", topics=frozenset())
    with pytest.raises(SelectionError):
        Candidate(doc_id="x", topics=frozenset(["a", "b", "c"]))
    with pytest.raises(SelectionError):
        select_candidates([make("d1", ["a"])], {"a": 1}, budget=-1)
    with pytest.raises(SelectionError):
        select_candidates(
            [make("d1", ["a"]), make("d1", ["a"])], {"a": 1}, budget=3
        )
    with pytest.raises(SelectionError):
        select_candidates([make("d1", ["ghost"])], {"a": 1}, budget=3)


def test_records_json_round_trip():
    records = [
        {
            "doc_id": "d1",
            "topics": ["finance", "policy"],
            "source": "wire",
            "relevance": 0.8,
        }
    ]
    candidates = candidates_from_records(records)
    result = select_candidates(
        candidates, {"finance": 1, "policy": 1}, budget=2
    )
    payload = result_to_dict(result)
    assert payload["feasible"] is True
    assert json.loads(json.dumps(payload))["selected"][0]["doc_id"] == "d1"


def test_cli_demo(monkeypatch, capsys):
    exit_code = main(["--demo"])
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["feasible"] is True
    assert payload["reviews_used"] <= payload["budget"]
    assert payload["optimal"] is True


def test_cli_infeasible_exit_code(monkeypatch, capsys):
    raw = json.dumps(
        {
            "budget": 1,
            "quotas": {"finance": 3},
            "candidates": [
                {"doc_id": "f1", "topics": ["finance"]},
                {"doc_id": "f2", "topics": ["finance"]},
            ],
        }
    )
    monkeypatch.setattr("sys.stdin", StringIO(raw))
    exit_code = main(["-"])
    assert exit_code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["feasible"] is False
    assert payload["shortfalls"] == {"finance": 2}
