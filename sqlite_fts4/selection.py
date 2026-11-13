"""Editorial candidate selection under a fixed manual-review budget.

Given the FTS4 hits for a set of business topics, choose which documents
should be sent for manual review while respecting:

* a per-topic minimum sample quota, which must be satisfied first;
* a hard review budget - a document evidencing two topics is reviewed
  (and billed) only once;
* same-source reprints - at most one document per source may be chosen;
* declared relevance - maximised among every feasible selection.

The exact solver is a dynamic program whose states are a topic-count
vector paired with the number of reviews already spent. Same-source
reprints become mutually exclusive groups, so source conflicts never need
a state of their own. A greedy fallback is used when the state space grows
past ``state_limit``.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from typing import Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple

_RELEVANCE_EPSILON = 1e-12

# A DP state maps (capped count vector, reviews used) to
# (total relevance, ordered tuple of selected doc ids).
_DPTable = Dict[Tuple[Tuple[int, ...], int], Tuple[float, Tuple[str, ...]]]


class SelectionError(ValueError):
    """Raised when the candidate or quota input is malformed."""


@dataclass(frozen=True)
class Candidate:
    """One FTS4 hit that may be picked for manual review.

    ``topics`` holds the business topics the document can evidence (one or
    two). ``source`` identifies reprints: candidates sharing a source are
    mutually exclusive. ``None`` means the document has no sibling source.
    """

    doc_id: str
    topics: FrozenSet[str]
    source: Optional[str] = None
    relevance: float = 0.0

    def __post_init__(self):
        topics = frozenset(self.topics)
        if not topics:
            raise SelectionError(
                "candidate {!r} does not evidence any topic".format(self.doc_id)
            )
        if len(topics) > 2:
            raise SelectionError(
                "candidate {!r} evidences {} topics; a document may evidence "
                "at most two topics".format(self.doc_id, len(topics))
            )
        object.__setattr__(self, "topics", topics)
        object.__setattr__(self, "relevance", float(self.relevance))


@dataclass
class SelectionResult:
    feasible: bool
    selected: List[Candidate]
    topic_counts: Dict[str, int]
    reviews_used: int
    budget: int
    total_relevance: float
    optimal: bool
    shortfalls: Dict[str, int]
    diagnostics: Dict[str, object]

    @property
    def budget_remaining(self) -> int:
        return self.budget - self.reviews_used


def _candidate_vector(
    candidate: Candidate, topic_order: Sequence[str]
) -> Tuple[int, ...]:
    return tuple(1 if topic in candidate.topics else 0 for topic in topic_order)


def _build_groups(
    candidates: Sequence[Candidate],
) -> List[List[Candidate]]:
    groups: List[List[Candidate]] = []
    by_source: Dict[str, List[Candidate]] = {}
    for candidate in candidates:
        if candidate.source is None:
            groups.append([candidate])
        else:
            by_source.setdefault(candidate.source, []).append(candidate)
    for members in by_source.values():
        members.sort(key=lambda item: (-item.relevance, item.doc_id))
        groups.append(members)
    groups.sort(key=lambda group: (group[0].source or "", group[0].doc_id))
    return groups


def _joint_dp(
    groups: Sequence[Sequence[Candidate]],
    topic_order: Sequence[str],
    caps: Sequence[int],
    budget: Optional[int],
    state_limit: int,
) -> Optional[_DPTable]:
    """Exact DP keyed by (capped count vector, reviews used).

    For every key only the maximum-relevance path is retained. The review
    count in the key guarantees that a high-relevance path can never crowd
    out a cheaper path that leaves room for later source groups.

    Returns ``None`` when the table grows past ``state_limit``.
    """
    zero = tuple(0 for _ in topic_order)
    dp: _DPTable = {(zero, 0): (0.0, ())}
    width = len(topic_order)
    for group in groups:
        options = [
            (candidate, _candidate_vector(candidate, topic_order))
            for candidate in group
        ]
        next_dp: _DPTable = dict(dp)  # skipping this source group is allowed
        get = next_dp.get
        for (counts, reviews), (relevance, ids) in dp.items():
            if budget is not None and reviews >= budget:
                continue
            next_reviews = reviews + 1
            for candidate, vector in options:
                new_counts = tuple(
                    cap if count >= cap else count + delta
                    for count, cap, delta in zip(counts, caps, vector)
                )
                key = (new_counts, next_reviews)
                new_relevance = relevance + candidate.relevance
                incumbent = get(key)
                if (
                    incumbent is None
                    or new_relevance > incumbent[0] + _RELEVANCE_EPSILON
                ):
                    next_dp[key] = (new_relevance, ids + (candidate.doc_id,))
        dp = next_dp
        if len(dp) > state_limit:
            return None
    return dp


def _min_cost_dp(
    groups: Sequence[Sequence[Candidate]],
    topic_order: Sequence[str],
    caps: Sequence[int],
) -> Dict[Tuple[int, ...], Tuple[int, Tuple[str, ...]]]:
    """Smallest number of reviews reaching each capped count vector."""
    zero = tuple(0 for _ in topic_order)
    dp: Dict[Tuple[int, ...], Tuple[int, Tuple[str, ...]]] = {zero: (0, ())}
    for group in groups:
        next_dp = dict(dp)
        get = next_dp.get
        for counts, (reviews, ids) in dp.items():
            for candidate in group:
                vector = _candidate_vector(candidate, topic_order)
                new_counts = tuple(
                    cap if count >= cap else count + delta
                    for count, cap, delta in zip(counts, caps, vector)
                )
                candidate_payload = (reviews + 1, ids + (candidate.doc_id,))
                incumbent = get(new_counts)
                if incumbent is None or candidate_payload < incumbent:
                    next_dp[new_counts] = candidate_payload
        dp = next_dp
    return dp


def _best_entry_for_target(
    dp: _DPTable, target: Tuple[int, ...]
) -> Optional[Tuple[float, int, Tuple[str, ...]]]:
    matches = [
        (relevance, reviews, ids)
        for (counts, reviews), (relevance, ids) in dp.items()
        if counts == target
    ]
    if not matches:
        return None
    return max(matches, key=lambda entry: (entry[0], -entry[1], entry[2]))


def _closest_entry(
    dp: _DPTable, quota_vector: Sequence[int]
) -> Tuple[Tuple[int, ...], Tuple[float, int, Tuple[str, ...]]]:
    def ranking(item):
        (counts, reviews), (relevance, ids) = item
        shortfall = sum(
            max(0, quota - count)
            for quota, count in zip(quota_vector, counts)
        )
        return (shortfall, -relevance, reviews, ids)

    (best_counts, best_reviews), (best_relevance, best_ids) = min(
        dp.items(), key=ranking
    )
    return best_counts, (best_relevance, best_reviews, best_ids)


def _topic_ceilings(
    groups: Sequence[Sequence[Candidate]], topic_order: Sequence[str]
) -> Dict[str, int]:
    ceilings = {topic: 0 for topic in topic_order}
    for group in groups:
        covered = set()
        for candidate in group:
            covered.update(candidate.topics)
        for topic in covered:
            ceilings[topic] += 1
    return ceilings


def _validate(
    candidates: Sequence[Candidate], quotas: Mapping[str, int], budget: int
) -> List[str]:
    if not isinstance(budget, int) or budget < 0:
        raise SelectionError("budget must be a non-negative integer")
    if any(not isinstance(quota, int) or quota < 0 for quota in quotas.values()):
        raise SelectionError("every quota must be a non-negative integer")
    seen_ids = set()
    unknown_topics = set()
    for candidate in candidates:
        if candidate.doc_id in seen_ids:
            raise SelectionError(
                "duplicate candidate doc_id: {!r}".format(candidate.doc_id)
            )
        seen_ids.add(candidate.doc_id)
        unknown_topics.update(candidate.topics - quotas.keys())
    if unknown_topics:
        raise SelectionError(
            "candidates reference topics without a quota: {}".format(
                sorted(unknown_topics)
            )
        )
    return sorted(quotas.keys())


def _greedy_select(
    candidates: Sequence[Candidate],
    quotas: Mapping[str, int],
    budget: int,
) -> Tuple[List[Candidate], Dict[str, int], float]:
    chosen: Dict[str, Candidate] = {}
    used_sources: set = set()
    counts = {topic: 0 for topic in quotas}

    def available(candidate: Candidate) -> bool:
        return (
            candidate.doc_id not in chosen
            and (candidate.source is None or candidate.source not in used_sources)
        )

    def take(candidate: Candidate) -> None:
        chosen[candidate.doc_id] = candidate
        if candidate.source is not None:
            used_sources.add(candidate.source)
        for topic in candidate.topics:
            counts[topic] += 1

    pool = list(candidates)
    # Phase 1: close every quota gap, preferring two-topic, high-relevance hits.
    while len(chosen) < budget and any(
        counts[topic] < quotas[topic] for topic in quotas
    ):
        options = []
        for candidate in pool:
            if not available(candidate):
                continue
            deficit_topics = [
                topic for topic in candidate.topics if counts[topic] < quotas[topic]
            ]
            if not deficit_topics:
                continue
            options.append(
                (
                    -len(deficit_topics),
                    -candidate.relevance,
                    candidate.doc_id,
                    candidate,
                )
            )
        if not options:
            break
        take(min(options, key=lambda item: item[:3])[3])

    # Phase 2: spend leftover reviews on the best remaining documents.
    while len(chosen) < budget:
        options = [
            (-candidate.relevance, candidate.doc_id, candidate)
            for candidate in pool
            if available(candidate)
        ]
        if not options:
            break
        take(min(options, key=lambda item: item[:2])[2])

    selected = sorted(
        chosen.values(), key=lambda item: (-item.relevance, item.doc_id)
    )
    total_relevance = sum(candidate.relevance for candidate in selected)
    return selected, dict(counts), total_relevance


def select_candidates(
    candidates: Sequence[Candidate],
    quotas: Mapping[str, int],
    budget: int,
    *,
    state_limit: int = 200_000,
) -> SelectionResult:
    """Select documents for review.

    Args:
        candidates: all FTS4 hits available for picking.
        quotas: mapping of topic name to minimum sample count.
        budget: maximum number of documents that may be reviewed.
        state_limit: DP state cap above which the greedy fallback kicks in.

    Returns:
        A :class:`SelectionResult`; when the minima cannot all be met,
        ``feasible`` is ``False`` and ``shortfalls`` / ``diagnostics``
        explain which topics are short and why.
    """
    topic_order = _validate(candidates, quotas, budget)
    quota_vector = [quotas[topic] for topic in topic_order]
    target = tuple(quota_vector)
    groups = _build_groups(candidates)
    by_id = {candidate.doc_id: candidate for candidate in candidates}

    vector_space = 1
    for quota in quota_vector:
        vector_space *= quota + 1
    if vector_space * (budget + 1) > state_limit:
        return _greedy_result(candidates, quotas, budget, topic_order)

    dp = _joint_dp(groups, topic_order, quota_vector, budget, state_limit)
    if dp is None:
        return _greedy_result(candidates, quotas, budget, topic_order)

    min_cost = _min_cost_dp(groups, topic_order, quota_vector)
    min_reviews_required = min_cost.get(target, (None,))[0]

    best_target = _best_entry_for_target(dp, target)
    if best_target is not None:
        relevance, reviews, ids = best_target
        selected = _materialize(ids, by_id)
        return SelectionResult(
            feasible=True,
            selected=selected,
            topic_counts=_count_for(selected, topic_order),
            reviews_used=reviews,
            budget=budget,
            total_relevance=relevance,
            optimal=True,
            shortfalls={},
            diagnostics={
                "candidate_count": len(candidates),
                "source_group_count": len(groups),
                "min_reviews_required": min_reviews_required,
            },
        )

    return _infeasible_result(
        candidates=candidates,
        quotas=quotas,
        budget=budget,
        topic_order=topic_order,
        quota_vector=quota_vector,
        groups=groups,
        by_id=by_id,
        min_reviews_required=min_reviews_required,
        budget_dp=dp,
    )


def _materialize(
    ids: Sequence[str], by_id: Mapping[str, Candidate]
) -> List[Candidate]:
    return sorted(
        (by_id[doc_id] for doc_id in ids),
        key=lambda item: (-item.relevance, item.doc_id),
    )


def _greedy_result(
    candidates: Sequence[Candidate],
    quotas: Mapping[str, int],
    budget: int,
    topic_order: Sequence[str],
) -> SelectionResult:
    selected, counts, relevance = _greedy_select(candidates, quotas, budget)
    shortfalls = {
        topic: quotas[topic] - counts.get(topic, 0)
        for topic in topic_order
        if counts.get(topic, 0) < quotas[topic]
    }
    return SelectionResult(
        feasible=not shortfalls,
        selected=selected,
        topic_counts={topic: counts.get(topic, 0) for topic in topic_order},
        reviews_used=len(selected),
        budget=budget,
        total_relevance=relevance,
        optimal=False,
        shortfalls=shortfalls,
        diagnostics={
            "mode": "greedy",
            "reason": "exact DP state space exceeds the configured limit",
        },
    )


def _infeasible_result(
    *,
    candidates: Sequence[Candidate],
    quotas: Mapping[str, int],
    budget: int,
    topic_order: Sequence[str],
    quota_vector: Sequence[int],
    groups: Sequence[Sequence[Candidate]],
    by_id: Mapping[str, Candidate],
    min_reviews_required: Optional[int],
    budget_dp: _DPTable,
) -> SelectionResult:
    target = tuple(quota_vector)
    # Best-effort selection always respects the review budget, so its count
    # gaps are exactly the shortfalls an editor must re-quota.
    _, (relevance, reviews, ids) = _closest_entry(budget_dp, quota_vector)
    best_effort = _materialize(ids, by_id)
    actual_counts = _count_for(best_effort, topic_order)
    shortfalls = {
        topic: quota - actual_counts[topic]
        for topic, quota in zip(topic_order, quota_vector)
        if actual_counts[topic] < quota
    }

    singleton_groups = [[candidate] for candidate in candidates]
    relaxed_cost = _min_cost_dp(singleton_groups, topic_order, quota_vector).get(
        target, (None,)
    )[0]
    feasible_without_conflicts = (
        relaxed_cost is not None and relaxed_cost <= budget
    )
    conflict_groups = [
        {
            "source": group[0].source,
            "members": [
                {
                    "doc_id": candidate.doc_id,
                    "topics": sorted(candidate.topics),
                    "relevance": candidate.relevance,
                }
                for candidate in group
            ],
        }
        for group in groups
        if len(group) > 1
    ]
    diagnostics = {
        "candidate_count": len(candidates),
        "source_group_count": len(groups),
        "topic_ceilings": _topic_ceilings(groups, topic_order),
        "min_reviews_required": min_reviews_required,
        "budget_gap": (
            min_reviews_required - budget
            if min_reviews_required is not None and min_reviews_required > budget
            else 0
        ),
        "reachable_without_budget": min_reviews_required is not None,
        "feasible_without_source_conflicts": feasible_without_conflicts,
        "conflict_groups": conflict_groups,
        "best_effort_counts": actual_counts,
        "best_effort_selection": [candidate.doc_id for candidate in best_effort],
    }
    return SelectionResult(
        feasible=False,
        selected=best_effort,
        topic_counts=actual_counts,
        reviews_used=reviews,
        budget=budget,
        total_relevance=relevance,
        optimal=True,
        shortfalls=shortfalls,
        diagnostics=diagnostics,
    )


def _count_for(
    selected: Sequence[Candidate], topic_order: Sequence[str]
) -> Dict[str, int]:
    counts = {topic: 0 for topic in topic_order}
    for candidate in selected:
        for topic in candidate.topics:
            counts[topic] += 1
    return counts


def candidates_from_records(
    records: Sequence[Mapping[str, object]],
) -> List[Candidate]:
    candidates = []
    for record in records:
        try:
            candidates.append(
                Candidate(
                    doc_id=str(record["doc_id"]),
                    topics=frozenset(record["topics"]),
                    source=record.get("source"),
                    relevance=float(record.get("relevance", 0.0)),
                )
            )
        except KeyError as exc:
            raise SelectionError(
                "candidate record is missing required field: {}".format(exc.args[0])
            )
    return candidates


def result_to_dict(result: SelectionResult) -> Dict[str, object]:
    return {
        "feasible": result.feasible,
        "optimal": result.optimal,
        "budget": result.budget,
        "reviews_used": result.reviews_used,
        "budget_remaining": result.budget_remaining,
        "total_relevance": result.total_relevance,
        "topic_counts": result.topic_counts,
        "shortfalls": result.shortfalls,
        "selected": [
            {
                "doc_id": candidate.doc_id,
                "topics": sorted(candidate.topics),
                "source": candidate.source,
                "relevance": candidate.relevance,
            }
            for candidate in result.selected
        ],
        "diagnostics": result.diagnostics,
    }


def demo_payload() -> Dict[str, object]:
    """A small scenario showing sharing, source dedup, and quota trade-offs."""
    return {
        "budget": 10,
        "quotas": {"finance": 4, "policy": 3, "tech": 4},
        "candidates": [
            {"doc_id": "f1", "topics": ["finance"], "source": "wire-a", "relevance": 0.95},
            {"doc_id": "f2", "topics": ["finance"], "source": "wire-a", "relevance": 0.80},
            {"doc_id": "f3", "topics": ["finance", "policy"], "source": None, "relevance": 0.90},
            {"doc_id": "f4", "topics": ["finance"], "source": "wire-b", "relevance": 0.70},
            {"doc_id": "p1", "topics": ["policy"], "source": None, "relevance": 0.88},
            {"doc_id": "p2", "topics": ["policy"], "source": "wire-d", "relevance": 0.65},
            {"doc_id": "p3", "topics": ["policy", "tech"], "source": None, "relevance": 0.84},
            {"doc_id": "t1", "topics": ["tech"], "source": "wire-c", "relevance": 0.99},
            {"doc_id": "t2", "topics": ["tech", "finance"], "source": "wire-c", "relevance": 0.91},
            {"doc_id": "t3", "topics": ["tech"], "source": None, "relevance": 0.83},
            {"doc_id": "t4", "topics": ["tech"], "source": None, "relevance": 0.72},
        ],
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Select FTS4 hits for manual review under a fixed budget."
    )
    parser.add_argument(
        "input",
        nargs="?",
        help="JSON input file; use '-' or omit to read from standard input",
    )
    parser.add_argument(
        "--demo", action="store_true", help="run the built-in demo scenario"
    )
    args = parser.parse_args(argv)

    if args.demo:
        payload = demo_payload()
    elif args.input in (None, "-"):
        payload = json.load(sys.stdin)
    else:
        with open(args.input, encoding="utf-8") as handle:
            payload = json.load(handle)

    candidates = candidates_from_records(payload["candidates"])
    result = select_candidates(candidates, payload["quotas"], payload["budget"])
    print(json.dumps(result_to_dict(result), indent=2, ensure_ascii=False))
    return 0 if result.feasible else 2


if __name__ == "__main__":
    raise SystemExit(main())
