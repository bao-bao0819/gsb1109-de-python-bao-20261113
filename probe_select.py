"""Select a minimum-total-cost set of read-only MATCH probes to identify an
index version whose label was lost.

Input JSON schema:
{
  "queries": [{"id": "q1", "cost": 2.0}, ...],
  "versions": [
    {"id": "v1", "receipts": {"q1": <receipt>, "q2": <receipt>, ...}},
    ...
  ]
}

Receipt forms (per version, per query):
  null / missing key / {"status": "error"} (any non-"ok" status)
      -> probe failure: treated as UNKNOWN, never as zero hits
  {"status": "ok", "fields": {"hits": 3, ...}} or a plain {"hits": 3, ...}
      -> observed per-field statistics (one query may report many fields)

Semantics:
  * A missing field inside a receipt is UNKNOWN and is never equal to 0.
  * Two versions whose receipts are identical across the ENTIRE candidate
    query set are merged into one equivalence group.
  * The solver picks a subset of queries with minimum total cost such that
    every pair of groups differs on at least one chosen query.

Usage: python probe_select.py data.json   (or pipe JSON via stdin)
"""

import json
import sys
from itertools import combinations

FAILED = ("__failed__",)


def normalize_receipt(receipt):
    if receipt is None:
        return FAILED
    if isinstance(receipt, dict):
        status = receipt.get("status", "ok")
        if status != "ok":
            return FAILED
        fields = receipt.get("fields", receipt)
        fields = {k: v for k, v in fields.items() if k != "status"}
        if not fields:
            return FAILED
        return ("ok", json.dumps(fields, sort_keys=True, default=str))
    return ("ok", json.dumps(receipt, sort_keys=True, default=str))


def load_problem(data):
    queries = [(q["id"], float(q.get("cost", 1.0))) for q in data["queries"]]
    query_ids = [qid for qid, _ in queries]
    versions = []
    for v in data["versions"]:
        receipts = v.get("receipts", {})
        obs = tuple(normalize_receipt(receipts.get(qid)) for qid in query_ids)
        versions.append({"id": v["id"], "obs": obs})
    return queries, versions


def group_versions(versions):
    groups = {}
    for v in versions:
        groups.setdefault(v["obs"], []).append(v["id"])
    return list(groups.values())


def min_cost_probes(queries, versions):
    query_ids = [qid for qid, _ in queries]
    costs = [c for _, c in queries]
    groups = group_versions(versions)
    representatives = []
    for member_ids in groups:
        rep_obs = next(v["obs"] for v in versions if v["id"] == member_ids[0])
        representatives.append(rep_obs)

    pairs = []
    for i, j in combinations(range(len(groups)), 2):
        diff = frozenset(
            q for q in range(len(queries))
            if representatives[i][q] != representatives[j][q]
        )
        pairs.append((i, j, diff))

    useful = sorted(
        {q for _, _, diff in pairs for q in diff}, key=lambda q: costs[q]
    )
    best = {"cost": float("inf"), "set": None}

    def greedy():
        chosen = set()
        uncovered = set(range(len(pairs)))
        while uncovered:
            best_q, best_gain = None, -1
            for q in useful:
                if q in chosen:
                    continue
                gain = sum(1 for p in uncovered if q in pairs[p][2])
                if gain > best_gain or (gain == best_gain and best_q is not None
                                        and costs[q] < costs[best_q]):
                    best_q, best_gain = q, gain
            if best_q is None or best_gain <= 0:
                break
            chosen.add(best_q)
            uncovered = {p for p in uncovered if best_q not in pairs[p][2]}
        return chosen

    incumbent = greedy()
    best["cost"] = sum(costs[q] for q in incumbent)
    best["set"] = incumbent

    def covers(chosen):
        return all(diff & chosen for _, _, diff in pairs)

    def search(chosen, chosen_cost, uncovered):
        if chosen_cost >= best["cost"]:
            return
        if not uncovered:
            best["cost"] = chosen_cost
            best["set"] = set(chosen)
            return
        hardest = min(uncovered, key=lambda p: len(pairs[p][2] - chosen))
        remaining = pairs[hardest][2] - chosen
        for q in sorted(remaining, key=lambda q: costs[q]):
            newly_covered = {p for p in uncovered if q in pairs[p][2]}
            search(chosen | {q}, chosen_cost + costs[q],
                   uncovered - newly_covered)

    if pairs:
        search(set(), 0.0, set(range(len(pairs))))
    else:
        best = {"cost": 0.0, "set": set()}

    assert covers(best["set"]), "solver failed to cover all group pairs"
    return {
        "groups": [sorted(g) for g in groups],
        "probes": [
            {"id": query_ids[q], "cost": costs[q]}
            for q in sorted(best["set"], key=lambda q: costs[q])
        ],
        "total_cost": best["cost"],
    }


def main():
    raw = open(sys.argv[1]).read() if len(sys.argv) > 1 else sys.stdin.read()
    queries, versions = load_problem(json.loads(raw))
    result = min_cost_probes(queries, versions)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
