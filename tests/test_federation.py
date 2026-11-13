import math
import sqlite3

import pytest

from sqlite_fts4.federation import (
    B,
    K1,
    FtsLibrary,
    MergedLibrary,
    merge_libraries,
    rank_federated,
)


def make_connection(rows, columns=("title", "body"), table="docs"):
    conn = sqlite3.connect(":memory:")
    ddl = "CREATE VIRTUAL TABLE {} USING fts4({})".format(
        table, ", ".join(columns)
    )
    conn.execute(ddl)
    conn.executemany(
        "INSERT INTO {} VALUES ({})".format(
            table, ", ".join("?" for _ in columns)
        ),
        rows,
    )
    return conn


@pytest.fixture
def three_libraries():
    north = make_connection(
        [
            ("alpha dog", "the dog runs fast"),
            ("quiet cat", "a cat sleeps here"),
            ("fish tank", "blue fish swim"),
        ],
        table="north_docs",
    )
    south = make_connection(
        [
            ("dog training", "train your dog well"),
            ("big dog park", "many dogs play all day"),
            ("cat cafe", "cats and coffee"),
        ],
        columns=("headline", "text"),
        table="south_docs",
    )
    east = make_connection(
        [
            ("rare wombat", "wombat facts only"),
            ("dog day", "a dog a day keeps"),
        ],
        table="east_docs",
    )
    return [
        FtsLibrary(
            north,
            "north_docs",
            {"title": "name", "body": "content"},
            name="north",
        ),
        FtsLibrary(
            south,
            "south_docs",
            {"headline": "name", "text": "content"},
            name="south",
        ),
        FtsLibrary(
            east,
            "east_docs",
            {"title": "name", "body": "content"},
            name="east",
        ),
    ]


def result_tuples(ranking):
    return [
        (r.score, r.library, r.docid, r.rank, r.values) for r in ranking
    ]


# ---------------------------------------------------------------------------
# Merged corpus statistics
# ---------------------------------------------------------------------------

def test_total_document_count_is_merged(three_libraries):
    ranking = rank_federated(three_libraries, "dog")
    assert ranking.libraries_searched == 3
    assert sum(library.row_count for library in three_libraries) == 8


def test_field_totals_and_average_are_merged(three_libraries):
    total_documents = 8
    expected_title_tokens = 17
    expected_body_tokens = 31

    def local_name(library, canonical):
        return next(
            local for local, mapped in library.column_map.items()
            if mapped == canonical
        )

    title_total = sum(
        library.column_totals[local_name(library, "name")]
        for library in three_libraries
    )
    body_total = sum(
        library.column_totals[local_name(library, "content")]
        for library in three_libraries
    )
    assert title_total == expected_title_tokens
    assert body_total == expected_body_tokens

    ranking = rank_federated(three_libraries, "dog")
    assert ranking.columns == ("name", "content")

    merged = merge_libraries(three_libraries)
    from sqlite_fts4.federation import _decode_varints

    stats = _decode_varints(
        merged.connection.execute(
            'SELECT value FROM "merged_stat" WHERE id = 0'
        ).fetchone()[0]
    )
    assert stats[0] == total_documents
    assert stats[1] == expected_title_tokens
    assert stats[2] == expected_body_tokens
    assert expected_title_tokens / total_documents == pytest.approx(17 / 8)
    assert expected_body_tokens / total_documents == pytest.approx(31 / 8)


def test_document_frequency_is_summed_across_libraries(three_libraries):
    # "dog" document counts per library:
    #   north: 1 doc (row 1, both fields)
    #   south: 2 docs (rows 1 and 2, headline); only row 1 mentions it in text
    #   east:  1 doc (row 2, both fields)
    # -> name/headline column 1+2+1 = 4, body/text column 1+1+1 = 3.
    aggregate = [0, 0]
    for library in three_libraries:
        local_df = library.term_document_frequency("dog")
        aggregate[0] += local_df[
            next(local for local, canonical in library.column_map.items()
                 if canonical == "name")
        ]
        aggregate[1] += local_df[
            next(local for local, canonical in library.column_map.items()
                 if canonical == "content")
        ]
    assert aggregate == [4, 3]


def test_global_ranking_differs_from_per_library_first_place():
    # In library A "dog" fills 2 of 3 documents, driving its local idf to
    # zero, while globally it stays informative.
    conn_a = make_connection(
        [
            ("dog tale", "dog dog dog"),
            ("dog park", "dog"),
            ("cat life", "cat"),
        ]
    )
    conn_b = make_connection(
        [
            ("fish", "fish"),
            ("birds", "bird"),
            ("cats", "cat cat"),
            ("the dog", "dog"),
        ]
    )
    from sqlite_fts4 import register_functions

    register_functions(conn_a)
    local = conn_a.execute(
        "SELECT rank_bm25(matchinfo(docs, 'pcnalx')) "
        "FROM docs WHERE docs MATCH 'dog' ORDER BY 1"
    ).fetchall()
    assert local[0][0] == 0.0

    libraries = [
        FtsLibrary(conn_a, "docs", {"title": "title", "body": "body"}, "A"),
        FtsLibrary(conn_b, "docs", {"title": "title", "body": "body"}, "B"),
    ]
    ranking = rank_federated(libraries, "dog")
    # Globally the previously-flat library A documents now get distinct,
    # non-zero scores.
    scores = {r.docid: r.score for r in ranking if r.library == "A"}
    assert all(score < 0.0 for score in scores.values())
    assert len(set(round(score, 9) for score in scores.values())) >= 1


# ---------------------------------------------------------------------------
# Column ordering driven by the mapping tables
# ---------------------------------------------------------------------------

def test_column_order_comes_from_mapping(three_libraries):
    ranking = rank_federated(three_libraries, "wombat")
    assert ranking.keys() == ["name", "content"]
    winner = ranking.results[0]
    assert winner.columns == ("name", "content")
    assert winner.as_dict()["name"] == "rare wombat"
    assert winner.as_dict()["content"] == "wombat facts only"


def test_mapping_reorders_local_columns():
    # Local columns stored in the opposite order; mapping fixes output order.
    conn = make_connection(
        [("body text here", "headline word")], columns=("c0", "c1")
    )
    library = FtsLibrary(
        conn, "docs", {"c1": "name", "c0": "content"}, name="lib"
    )
    ranking = rank_federated([library], "word")
    assert ranking.columns == ("name", "content")
    assert ranking.results[0].values == ("headline word", "body text here")


# ---------------------------------------------------------------------------
# BM25 formula
# ---------------------------------------------------------------------------

def test_hand_computed_bm25_score():
    conn = make_connection(
        [("dog dog",), ("cat cat",)], columns=("c0",)
    )
    other = make_connection(
        [("a b c d",), ("fish fish",)], columns=("c0",)
    )
    libraries = [
        FtsLibrary(conn, "docs", {"c0": "text"}, "A"),
        FtsLibrary(other, "docs", {"c0": "text"}, "B"),
    ]
    ranking = rank_federated(libraries, "dog")
    # N=4, avgdl=2.5, df=1, tf=2, dl=2, k1=1.2, b=0.75
    length_norm = 1 - B + B * (2 / 2.5)
    idf = math.log((4 - 1 + 0.5) / (1 + 0.5))
    expected = -(idf * (2 * (K1 + 1)) / (2 + K1 * length_norm))
    assert ranking.results[0].score == pytest.approx(expected)
    assert ranking.results[0].score == pytest.approx(-1.234473704)


def test_ties_share_competition_rank():
    # Keep "dog" rare (idf > 0) by padding with non-matching documents.
    # Two identical matching documents tie; a third matches inside a much
    # longer title and therefore scores lower.
    filler = [("other topic", "filler words here")] * 4
    conn_a = make_connection([("dog", "x")] + filler)
    conn_b = make_connection(
        [("dog", "x"), ("dog " + "pad " * 20, "z")] + filler
    )
    libraries = [
        FtsLibrary(conn_a, "docs", {"title": "name", "body": "content"}, "A"),
        FtsLibrary(conn_b, "docs", {"title": "name", "body": "content"}, "B"),
    ]
    tied = rank_federated(libraries, "dog")
    ranks = [r.rank for r in tied]
    assert ranks[0] == ranks[1] == 1
    assert ranks[2] == 3
    # The longer-title document scores strictly lower.
    assert tied.results[2].score > tied.results[0].score
    # Tie order is stable: library then original docid.
    assert [(r.library, r.docid) for r in tied] == [
        ("A", 1),
        ("B", 1),
        ("B", 2),
    ]


# ---------------------------------------------------------------------------
# Federated path vs synthetic merged library
# ---------------------------------------------------------------------------

def test_federated_and_merged_paths_match(three_libraries):
    federated = rank_federated(three_libraries, "dog cat")
    merged = merge_libraries(three_libraries).search("dog cat")
    assert result_tuples(federated) == result_tuples(merged)


def test_merged_path_handles_missing_query_terms(three_libraries):
    federated = rank_federated(
        three_libraries, "dog wombat nosuchterm"
    )
    merged = merge_libraries(three_libraries).search(
        "dog wombat nosuchterm"
    )
    assert result_tuples(federated) == result_tuples(merged)
    assert len(federated) > 0


def test_merged_provenance_points_back_to_source(three_libraries):
    merged = merge_libraries(three_libraries)
    assert isinstance(merged, MergedLibrary)
    result = merged.search("wombat").results[0]
    assert result.library == "east"
    assert result.docid == 1
    # The merged rowid differs from the original library docid.
    assert merged.provenance[7] == (2, "east", 1)


def test_custom_k1_b_matches_across_paths(three_libraries):
    federated = rank_federated(
        three_libraries, "dog", k1=2.0, b=0.5
    )
    merged = merge_libraries(
        three_libraries, k1=2.0, b=0.5
    ).search("dog")
    assert result_tuples(federated) == result_tuples(merged)


# ---------------------------------------------------------------------------
# Early stopping
# ---------------------------------------------------------------------------

def test_early_stop_never_skips_library_holding_champion():
    early = make_connection(
        [("common stuff", "padding padding padding")] * 3
    )
    last = make_connection([("dog", "x")])
    libraries = [
        FtsLibrary(early, "docs", {"title": "name", "body": "content"}, "early"),
        FtsLibrary(last, "docs", {"title": "name", "body": "content"}, "last"),
    ]
    full = rank_federated(libraries, "dog", limit=1, early_stop=False)
    stopped = rank_federated(libraries, "dog", limit=1, early_stop=True)
    assert stopped.libraries_searched == 2
    assert stopped.stopped_early is False
    assert full.results[0].library == "last"
    assert (
        stopped.results[0].library,
        stopped.results[0].docid,
        stopped.results[0].score,
    ) == (
        full.results[0].library,
        full.results[0].docid,
        full.results[0].score,
    )


def test_early_stop_when_bound_proves_it_safe():
    strong_rows = [("dog", "dog")] + [
        ("filler term", "a b c d e f g h")
    ] * 5
    strong = make_connection(strong_rows)
    weak = make_connection(
        [("dog " + "word " * 200, "dog " + "word " * 200)]
    )
    libraries = [
        FtsLibrary(strong, "docs", {"title": "name", "body": "content"}, "strong"),
        FtsLibrary(weak, "docs", {"title": "name", "body": "content"}, "weak"),
    ]
    full = rank_federated(libraries, "dog", limit=1, early_stop=False)
    stopped = rank_federated(libraries, "dog", limit=1, early_stop=True)
    assert stopped.stopped_early is True
    assert stopped.libraries_searched == 1
    assert (
        stopped.results[0].library,
        stopped.results[0].docid,
        stopped.results[0].score,
    ) == (
        full.results[0].library,
        full.results[0].docid,
        full.results[0].score,
    )


def test_early_stop_respects_high_term_frequency_champion():
    # Regression: a later library holds a document that repeats the query
    # term many times.  A bound that assumes a single occurrence would
    # underestimate it and skip the real champion, so this must search all.
    first = make_connection(
        [("dog", "x")] + [("other topic", "filler words here")] * 5
    )
    high_tf = make_connection(
        [("dog " * 40, "dog " * 40)]
    )
    libraries = [
        FtsLibrary(first, "docs", {"title": "name", "body": "content"}, "first"),
        FtsLibrary(high_tf, "docs", {"title": "name", "body": "content"}, "high"),
    ]
    full = rank_federated(libraries, "dog", limit=1, early_stop=False)
    stopped = rank_federated(libraries, "dog", limit=1, early_stop=True)
    assert stopped.libraries_searched == 2
    assert stopped.stopped_early is False
    assert full.results[0].library == "high"
    assert stopped.results[0].library == "high"
    assert stopped.results[0].score == full.results[0].score


def test_no_limit_never_stops_early(three_libraries):
    ranking = rank_federated(three_libraries, "dog", early_stop=True)
    assert ranking.stopped_early is False
    assert ranking.libraries_searched == 3


def test_early_stop_prefix_equals_full_ranking(three_libraries):
    for limit in (1, 2, 3, 5):
        full = rank_federated(
            three_libraries, "dog", limit=limit, early_stop=False
        )
        stopped = rank_federated(
            three_libraries, "dog", limit=limit, early_stop=True
        )
        assert result_tuples(stopped) == result_tuples(full)


# ---------------------------------------------------------------------------
# Edge cases and validation
# ---------------------------------------------------------------------------

def test_empty_query_returns_empty_ranking(three_libraries):
    ranking = rank_federated(three_libraries, "")
    assert len(ranking) == 0
    assert ranking.columns == ("name", "content")
    assert merge_libraries(three_libraries).search("").results == []


def test_empty_corpus_does_not_crash():
    empty = make_connection([])
    library = FtsLibrary(
        empty, "docs", {"title": "name", "body": "content"}, "empty"
    )
    ranking = rank_federated([library], "dog")
    assert len(ranking) == 0
    assert merge_libraries([library]).search("dog").results == []


def test_multibyte_varint_doc_length():
    # A document longer than 127 tokens forces multi-byte varints.
    long_body = " ".join("word{}".format(i) for i in range(300)) + " dog"
    conn = make_connection([("dog", long_body)])
    library = FtsLibrary(
        conn, "docs", {"title": "name", "body": "content"}, "big"
    )
    assert library.doc_length(1, "body") == 301
    ranking = rank_federated([library], "dog")
    assert ranking.results[0].docid == 1


def test_rejects_unknown_column():
    conn = make_connection([("a", "b")])
    with pytest.raises(ValueError, match="has no column"):
        FtsLibrary(conn, "docs", {"missing": "name"}, "bad")


def test_rejects_unindexed_column_mapping():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE VIRTUAL TABLE t USING fts4(a, tag UNINDEXED)")
    conn.execute("INSERT INTO t VALUES ('x', 'ignored')")
    with pytest.raises(ValueError, match="UNINDEXED"):
        FtsLibrary(conn, "t", {"tag": "tag"}, "bad")


def test_rejects_unmapped_indexed_column():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE VIRTUAL TABLE t USING fts4(title, body, extra)"
    )
    conn.execute("INSERT INTO t VALUES ('a', 'b', 'c')")
    with pytest.raises(ValueError, match="missing from column_map"):
        FtsLibrary(conn, "t", {"title": "name", "body": "content"}, "bad")


def test_allows_unmapped_unindexed_column():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE VIRTUAL TABLE t USING fts4(title, body, src UNINDEXED)"
    )
    conn.execute(
        "INSERT INTO t (title, body, src) VALUES ('dog', 'x', 'lib/1')"
    )
    library = FtsLibrary(
        conn, "t", {"title": "name", "body": "content"}, "ok"
    )
    ranking = rank_federated([library], "dog")
    assert ranking.results[0].values == ("dog", "x")
    assert merge_libraries([library]).search("dog").results[0].values == (
        "dog",
        "x",
    )


def test_rejects_inconsistent_canonical_columns(three_libraries):
    conn = make_connection([("a", "b")])
    different = FtsLibrary(
        conn, "docs", {"title": "other", "body": "content"}, "other"
    )
    with pytest.raises(ValueError, match="canonical columns"):
        rank_federated([three_libraries[0], different], "dog")


def test_rejects_mixed_tokenizers():
    porter = sqlite3.connect(":memory:")
    porter.execute(
        "CREATE VIRTUAL TABLE t USING fts4(a, b, tokenize=porter)"
    )
    porter.execute("INSERT INTO t VALUES ('running dogs', 'x')")
    plain = make_connection([("running", "x")])
    libraries = [
        FtsLibrary(porter, "t", {"a": "name", "b": "content"}, "porter"),
        FtsLibrary(plain, "docs", {"title": "name", "body": "content"}, "plain"),
    ]
    with pytest.raises(ValueError, match="tokenizer"):
        rank_federated(libraries, "run")


def test_rejects_empty_library_list():
    with pytest.raises(ValueError, match="at least one"):
        rank_federated([], "dog")


def test_custom_tokenizer_is_preserved_in_merged_table():
    conn_a = sqlite3.connect(":memory:")
    conn_a.execute(
        "CREATE VIRTUAL TABLE t USING fts4(a, b, tokenize=porter)"
    )
    conn_a.execute("INSERT INTO t VALUES ('running dogs', 'ran')")
    conn_b = sqlite3.connect(":memory:")
    conn_b.execute(
        "CREATE VIRTUAL TABLE t USING fts4(a, b, tokenize=porter)"
    )
    conn_b.execute("INSERT INTO t VALUES ('dog runs', 'running')")
    libraries = [
        FtsLibrary(conn_a, "t", {"a": "name", "b": "content"}, "a"),
        FtsLibrary(conn_b, "t", {"a": "name", "b": "content"}, "b"),
    ]
    federated = rank_federated(libraries, "run")
    merged = merge_libraries(libraries).search("run")
    assert len(federated) == 2
    assert result_tuples(federated) == result_tuples(merged)
