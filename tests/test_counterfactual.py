import sqlite3

import pytest

from sqlite_fts4 import bm25_score, register_functions
from sqlite_fts4.counterfactual import (
    DocumentObservation,
    Snapshot,
    build_ledger,
    collect_snapshot,
    score_document,
)

BEFORE_ROWS = [
    ("this is about a dog", "more about that dog dog"),
    ("this is about a cat", "stuff on that cat cat"),
    ("something about a ferret", "yeah a ferret ferret"),
    ("both of them", "both dog dog and cat here"),
    ("not mammals", "maybe talk about fish"),
]

# The week after: the target article was edited (fewer "dog" hits, longer
# text), the rival gained a hit, a batch of new rows landed (several with
# "dog" in the first column) and ops re-tuned the weights to b=1.0.
AFTER_ROWS = [
    (
        "this is about a dog and a long meandering tale about everything else",
        "more about that dog here",
    ),
    ("this is about a cat", "stuff on that cat cat"),
    ("something about a ferret", "yeah a ferret ferret"),
    ("both of them", "both dog dog dog and cat here"),
    ("not mammals", "maybe talk about fish"),
    ("dog day", "sunny afternoon"),
    ("dog run", "at the park"),
    ("dog show", "prizes awarded"),
    ("dog tales", "quiet evening"),
    ("dog notes", "dog dog"),
    ("quiet garden notes", "clouds and rain today"),
    ("shipping update", "boxes arrived on time"),
    ("lunch menu", "soup salad bread"),
    ("meeting minutes", "budget and roadmap"),
    ("travel log", "trains through the alps"),
]

TARGET = 1
RIVAL = 4


def make_db(rows):
    conn = sqlite3.connect(":memory:")
    register_functions(conn)
    conn.execute("CREATE VIRTUAL TABLE search USING fts4(c0, c1)")
    conn.executemany("INSERT INTO search (c0, c1) VALUES (?, ?)", rows)
    return conn


@pytest.fixture
def before_conn():
    return make_db(BEFORE_ROWS)


@pytest.fixture
def after_conn():
    return make_db(AFTER_ROWS)


@pytest.fixture
def snapshots(before_conn, after_conn):
    before = collect_snapshot(before_conn, "search", "dog")
    after = collect_snapshot(after_conn, "search", "dog", b=1.0)
    return before, after


def sql_bm25_scores(conn):
    return dict(
        conn.execute(
            "select rowid, rank_bm25(matchinfo(search, 'pcnalx')) "
            "from search where search match 'dog'"
        ).fetchall()
    )


def test_snapshot_scores_match_sql_rank_bm25(before_conn, after_conn, snapshots):
    before, after = snapshots
    for rowid, sql_score in sql_bm25_scores(before_conn).items():
        assert score_document(before, rowid) == pytest.approx(-sql_score)
    # The after snapshot was captured with re-tuned weights; scoring it with
    # the default weights must reproduce what SQLite's rank_bm25 reports.
    for rowid, sql_score in sql_bm25_scores(after_conn).items():
        assert score_document(after, rowid, weights_source=before) == pytest.approx(
            -sql_score
        )


def test_ledger_replays_both_rankings(snapshots):
    before, after = snapshots
    ledger = build_ledger(before, after, target=TARGET, rival=RIVAL)
    assert ledger.before["leader"] == str(TARGET)
    assert ledger.after["leader"] == str(RIVAL)
    # Every non-empty combination of the three factor groups is on the books
    assert 7 == len(ledger.entries)
    # Replaying all three factors reproduces the after snapshot exactly
    full = [e for e in ledger.entries if len(e.factors) == 3][0]
    assert full.target_score == pytest.approx(ledger.after["target_score"])
    assert full.rival_score == pytest.approx(ledger.after["rival_score"])
    assert full.flipped


def test_ledger_keeps_combo_that_only_flips_together(snapshots):
    before, after = snapshots
    ledger = build_ledger(before, after, target=TARGET, rival=RIVAL)
    # No single change is enough to cost the target its lead
    assert [] == ledger.single_factor_flips
    # The doc edit and the corpus shift only flip the ranking together
    assert [("doc", "corpus")] == ledger.interaction_combos
    combo = [e for e in ledger.entries if e.factors == ("doc", "corpus")][0]
    assert combo.leader == str(RIVAL)
    for factor in ("doc", "corpus"):
        single = [e for e in ledger.entries if e.factors == (factor,)][0]
        assert single.leader == str(TARGET)


def test_ledger_to_dict_is_json_ready(snapshots):
    import json

    before, after = snapshots
    ledger = build_ledger(before, after, target=TARGET, rival=RIVAL)
    json.dumps(ledger.to_dict())


def synthetic_snapshots():
    # One column, two phrases. The target only matches phrase 0, the rival
    # only matches phrase 1, so each has its own document frequency.
    before = Snapshot(
        query="alpha beta",
        k=1.2,
        b=0.75,
        total_rows=20,
        avg_tokens=(10,),
        docs_with_hits={(0, 0): 3, (1, 0): 4},
        docs={
            TARGET: DocumentObservation({(0, 0): 10}, {0: 20}),
            RIVAL: DocumentObservation({(1, 0): 2}, {0: 5}),
        },
        num_phrases=2,
        num_columns=1,
    )
    after = Snapshot(
        query="alpha beta",
        k=1.2,
        b=1.0,
        total_rows=40,
        avg_tokens=(10,),
        docs_with_hits={(0, 0): 6, (1, 0): 5},
        docs={
            TARGET: DocumentObservation({(0, 0): 10}, {0: 20}),
            RIVAL: DocumentObservation({(1, 0): 2}, {0: 5}),
        },
        num_phrases=2,
        num_columns=1,
    )
    return before, after


def test_corpus_and_weights_interaction():
    before, after = synthetic_snapshots()
    ledger = build_ledger(before, after, target=TARGET, rival=RIVAL)
    assert ledger.before["leader"] == str(TARGET)
    assert ledger.after["leader"] == str(RIVAL)
    # Neither the corpus shift nor the weight change flips anything alone
    assert [] == ledger.single_factor_flips
    # Together they do - so the combination stays on the ledger
    assert [("corpus", "weights")] == ledger.interaction_combos


def test_incompatible_snapshots_raise():
    before, after = synthetic_snapshots()
    after.num_phrases = 3
    with pytest.raises(ValueError, match="phrase"):
        build_ledger(before, after, target=TARGET, rival=RIVAL)


def test_missing_document_raises():
    before, after = synthetic_snapshots()
    del after.docs[RIVAL]
    with pytest.raises(ValueError, match="did not match"):
        build_ledger(before, after, target=TARGET, rival=RIVAL)


def test_term_frequency_and_length_come_from_the_same_snapshot():
    # Swapping the "doc" factor must move term frequency and document
    # length together - mixing them across captures is not allowed.
    shared_corpus = dict(total_rows=10, avg_tokens=(8,), docs_with_hits={(0, 0): 2})
    source = Snapshot(
        query="alpha",
        docs={TARGET: DocumentObservation({(0, 0): 2}, {0: 4})},
        num_phrases=1,
        num_columns=1,
        **shared_corpus
    )
    other = Snapshot(
        query="alpha",
        docs={TARGET: DocumentObservation({(0, 0): 5}, {0: 10})},
        num_phrases=1,
        num_columns=1,
        **shared_corpus
    )
    hybrid = score_document(source, TARGET, doc_source=other)
    assert hybrid == pytest.approx(score_document(other, TARGET))
    # A hypothetical mixed reading (frequency from other, length from
    # source) would score differently - the API never produces it.
    mixed = bm25_score(5, 2, 10, 8, 4, source.k, source.b)
    assert hybrid != pytest.approx(mixed)
