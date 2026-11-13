import itertools
from dataclasses import dataclass, field

from sqlite_fts4 import _annotate_matchinfo, bm25_score

DEFAULT_K = 1.2
DEFAULT_B = 0.75

# The three factor groups that can be swapped between two snapshots:
# - "doc": the document's own observations (term frequency + document length)
# - "corpus": index-wide statistics (row count, average column lengths, df)
# - "weights": the BM25 tuning weights (k, b)
FACTOR_GROUPS = ("doc", "corpus", "weights")


@dataclass
class DocumentObservation:
    # (phrase_index, column_index) -> hits in this row
    term_frequency: dict
    # column_index -> number of tokens in this row
    num_tokens: dict


@dataclass
class Snapshot:
    "A decoded matchinfo('pcnalx') capture of one FTS4 query run"
    query: str
    k: float = DEFAULT_K
    b: float = DEFAULT_B
    total_rows: int = 0
    avg_tokens: tuple = ()
    docs_with_hits: dict = field(default_factory=dict)
    docs: dict = field(default_factory=dict)
    num_phrases: int = 0
    num_columns: int = 0


def collect_snapshot(conn, table, query, k=DEFAULT_K, b=DEFAULT_B):
    "Run an FTS4 query and capture everything needed to re-score it offline"
    rows = conn.execute(
        'select rowid, matchinfo("{table}", \'pcnalx\') '
        'from "{table}" where "{table}" match ?'.format(table=table),
        [query],
    ).fetchall()
    snapshot = Snapshot(query=query, k=k, b=b)
    for rowid, raw_matchinfo in rows:
        info = _annotate_matchinfo(raw_matchinfo, "pcnalx")
        if not info:
            continue
        snapshot.num_phrases = info["p"]["value"]
        snapshot.num_columns = info["c"]["value"]
        snapshot.total_rows = info["n"]["value"]
        snapshot.avg_tokens = tuple(
            v["average_num_tokens"] for v in info["a"]["value"]
        )
        term_frequency = {}
        for detail in info["x"]["value"]:
            key = (detail["phrase_index"], detail["column_index"])
            term_frequency[key] = detail["hits_this_column_this_row"]
            snapshot.docs_with_hits[key] = detail["docs_with_hits"]
        num_tokens = {
            v["column_index"]: v["num_tokens"] for v in info["l"]["value"]
        }
        snapshot.docs[rowid] = DocumentObservation(term_frequency, num_tokens)
    return snapshot


def check_compatible(left, right):
    "Two snapshots can only share a ledger if they use the same index layout"
    if left.num_phrases != right.num_phrases:
        raise ValueError("Snapshots used different queries (phrase count differs)")
    if left.num_columns != right.num_columns:
        raise ValueError("Snapshots used different tables (column count differs)")


def score_document(snapshot, rowid, doc_source=None, corpus_source=None,
                   weights_source=None):
    """
    BM25 score for one document, with each factor group optionally drawn
    from a different snapshot. Term frequency and document length always
    come together from doc_source, so they stay within one compatible
    index snapshot and can never be mixed across captures.
    """
    doc_source = doc_source or snapshot
    corpus_source = corpus_source or snapshot
    weights_source = weights_source or snapshot
    observation = doc_source.docs[rowid]
    score = 0.0
    for phrase_index in range(snapshot.num_phrases):
        for column_index in range(snapshot.num_columns):
            key = (phrase_index, column_index)
            score += bm25_score(
                term_frequency=observation.term_frequency.get(key, 0),
                docs_with_hits=corpus_source.docs_with_hits[key],
                total_row_count=corpus_source.total_rows,
                average_num_tokens=corpus_source.avg_tokens[column_index],
                num_tokens=observation.num_tokens[column_index],
                k=weights_source.k,
                b=weights_source.b,
            )
    return score


@dataclass
class LedgerEntry:
    factors: tuple
    target_score: float
    rival_score: float
    margin: float
    leader: str
    flipped: bool


@dataclass
class Ledger:
    target: int
    rival: int
    before: dict
    after: dict
    entries: list
    single_factor_flips: list
    interaction_combos: list

    def to_dict(self):
        return {
            "target": self.target,
            "rival": self.rival,
            "before": self.before,
            "after": self.after,
            "entries": [
                {
                    "factors": list(entry.factors),
                    "target_score": entry.target_score,
                    "rival_score": entry.rival_score,
                    "margin": entry.margin,
                    "leader": entry.leader,
                    "flipped": entry.flipped,
                }
                for entry in self.entries
            ],
            "single_factor_flips": [list(f) for f in self.single_factor_flips],
            "interaction_combos": [list(c) for c in self.interaction_combos],
        }


def _leader(margin, target, rival):
    if margin > 0:
        return str(target)
    if margin < 0:
        return str(rival)
    return "tie"


def build_ledger(before, after, target, rival):
    """
    Put two snapshots on one diff ledger for the target/rival pair.

    Every non-empty combination of factor groups is replayed as a hybrid
    (those factors taken from `after`, the rest from `before`) to find
    which changes are enough to swap the ranking. Pairs that only flip
    the ranking together - never alone - are kept in interaction_combos.
    """
    check_compatible(before, after)
    for snapshot, label in ((before, "before"), (after, "after")):
        missing = [rowid for rowid in (target, rival) if rowid not in snapshot.docs]
        if missing:
            raise ValueError(
                "rowids {} did not match the query in the {} snapshot".format(
                    missing, label
                )
            )

    def hybrid_scores(factors):
        sources = {
            group: (after if group in factors else before)
            for group in FACTOR_GROUPS
        }
        target_score = score_document(
            before,
            target,
            doc_source=sources["doc"],
            corpus_source=sources["corpus"],
            weights_source=sources["weights"],
        )
        rival_score = score_document(
            before,
            rival,
            doc_source=sources["doc"],
            corpus_source=sources["corpus"],
            weights_source=sources["weights"],
        )
        return target_score, rival_score

    def score_pair(snapshot):
        return (
            score_document(snapshot, target),
            score_document(snapshot, rival),
        )

    before_target, before_rival = score_pair(before)
    after_target, after_rival = score_pair(after)
    baseline_leader = _leader(before_target - before_rival, target, rival)

    entries = []
    for size in range(1, len(FACTOR_GROUPS) + 1):
        for factors in itertools.combinations(FACTOR_GROUPS, size):
            target_score, rival_score = hybrid_scores(set(factors))
            margin = target_score - rival_score
            leader = _leader(margin, target, rival)
            entries.append(
                LedgerEntry(
                    factors=factors,
                    target_score=target_score,
                    rival_score=rival_score,
                    margin=margin,
                    leader=leader,
                    flipped=leader != baseline_leader,
                )
            )

    flipped_sets = [set(entry.factors) for entry in entries if entry.flipped]
    single_factor_flips = [
        entry.factors for entry in entries
        if entry.flipped and len(entry.factors) == 1
    ]
    # Keep combinations that flip only together: no proper subset of the
    # combination flips on its own, so the interaction itself is the cause.
    interaction_combos = [
        entry.factors
        for entry in entries
        if entry.flipped
        and len(entry.factors) > 1
        and not any(
            flipped < set(entry.factors) for flipped in flipped_sets
        )
    ]

    return Ledger(
        target=target,
        rival=rival,
        before={
            "target_score": before_target,
            "rival_score": before_rival,
            "leader": baseline_leader,
        },
        after={
            "target_score": after_target,
            "rival_score": after_rival,
            "leader": _leader(after_target - after_rival, target, rival),
        },
        entries=entries,
        single_factor_flips=single_factor_flips,
        interaction_combos=interaction_combos,
    )
