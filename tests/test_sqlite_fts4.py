import sqlite3
from sqlite_fts4 import (
    register_functions,
    decode_matchinfo,
    _annotate_matchinfo,
)
import pytest
import json
import sys


sqlite_version = tuple(
    map(
        int,
        sqlite3.connect(":memory:")
        .execute("select sqlite_version()")
        .fetchone()[0]
        .split("."),
    )
)


@pytest.fixture
def conn():
    conn = sqlite3.connect(":memory:")
    register_functions(conn)
    conn.executescript(
        """
CREATE VIRTUAL TABLE search USING fts4(c0, c1);
INSERT INTO search (c0, c1) VALUES ("this is about a dog", "more about that dog dog");
INSERT INTO search (c0, c1) VALUES ("this is about a cat", "stuff on that cat cat");
INSERT INTO search (c0, c1) VALUES ("something about a ferret", "yeah a ferret ferret");
INSERT INTO search (c0, c1) VALUES ("both of them", "both dog dog and cat here");
INSERT INTO search (c0, c1) VALUES ("not mammals", "maybe talk about fish");
    """
    )
    return conn


def test_fixture_sets_up_database(conn):
    assert 5 == conn.execute("select count(*) from search").fetchone()[0]


@pytest.mark.parametrize(
    "search,expected",
    [
        ("dog", [1, 2, 1, 1, 1, 2, 4, 2, 5, 4, 5, 5, 5]),
        ("cat", [1, 2, 1, 1, 1, 2, 3, 2, 5, 4, 5, 5, 5]),
    ],
)
def test_decode_matchinfo(conn, search, expected):
    r = conn.execute(
        """
        select decode_matchinfo(matchinfo(search, 'pcxnal'))
        from search where search match ?
    """,
        [search],
    ).fetchone()[0]
    assert expected == json.loads(r)


@pytest.mark.parametrize(
    "buf,expected",
    [
        (
            b"\x01\x00\x00\x00\x02\x00\x00\x00\x02\x00\x00\x00\x02\x00\x00\x00"
            if sys.byteorder == "little"
            else b"\x00\x00\x00\x01\x00\x00\x00\x02\x00\x00\x00\x02\x00\x00\x00\x02",
            (1, 2, 2, 2),
        )
    ],
)
def test_underlying_decode_matchinfo(buf, expected):
    assert expected == decode_matchinfo(buf)


def test_rank_bm25(conn):
    results = conn.execute(
        """
        select c0, c1, rank_bm25(matchinfo(search, 'pcnalx')) as bm25
        from search where search match ?
    """,
        ["dog"],
    ).fetchall()
    assert ("this is about a dog", "more about that dog dog") == results[0][:2]
    assert pytest.approx(-1.459328) == results[0][2]
    assert ("both of them", "both dog dog and cat here") == results[1][:2]
    assert pytest.approx(-0.438011) == results[1][2]


def test_rank_bm25_no_match(conn):
    results = conn.execute(
        """
        select c0, c1, rank_bm25(matchinfo(search, 'pcnalx')) as bm25
        from search limit 1
    """
    ).fetchall()
    assert None == results[0][2]


def test_annotate_matchinfo(conn):
    r = conn.execute(
        """
        select annotate_matchinfo(matchinfo(search, 'pcxnals'), 'pcxnals')
        from search where search match ?
    """,
        ["dog"],
    ).fetchone()[0]
    expected = {
        "p": {
            "value": 1,
            "title": "Number of matchable phrases in the query",
            "idx": 0,
        },
        "c": {
            "value": 2,
            "title": "Number of user defined columns in the FTS table",
            "idx": 1,
        },
        "x": {
            "value": [
                {
                    "phrase_index": 0,
                    "phrase": None,
                    "column_index": 0,
                    "column": None,
                    "hits_this_column_this_row": 1,
                    "hits_this_column_all_rows": 1,
                    "docs_with_hits": 1,
                    "idxs": [2, 3, 4],
                },
                {
                    "phrase_index": 0,
                    "phrase": None,
                    "column_index": 1,
                    "column": None,
                    "hits_this_column_this_row": 2,
                    "hits_this_column_all_rows": 4,
                    "docs_with_hits": 2,
                    "idxs": [5, 6, 7],
                },
            ],
            "title": "Details for each phrase/column combination",
        },
        "n": {"value": 5, "title": "Number of rows in the FTS4 table", "idx": 8},
        "a": {
            "title": "Average number of tokens in each column across the whole table",
            "value": [
                {"column_index": 0, "column": None, "average_num_tokens": 4, "idx": 9},
                {"column_index": 1, "column": None, "average_num_tokens": 5, "idx": 10},
            ],
        },
        "l": {
            "title": "Number of tokens in each column of the current row of the FTS4 table",
            "value": [
                {"column_index": 0, "column": None, "num_tokens": 5, "idx": 11},
                {"column_index": 1, "column": None, "num_tokens": 5, "idx": 12},
            ],
        },
        "s": {
            "title": "Length of longest subsequence of phrase matching each column",
            "value": [
                {
                    "column_index": 0,
                    "column": None,
                    "length_phrase_subsequence_match": 1,
                    "idx": 13,
                },
                {
                    "column_index": 1,
                    "column": None,
                    "length_phrase_subsequence_match": 1,
                    "idx": 14,
                },
            ],
        },
    }
    assert expected == json.loads(r)


def test_annotate_matchinfo_empty(conn):
    r = conn.execute(
        """
        select annotate_matchinfo(matchinfo(search, 'pcxnals'), 'pcxnals')
        from search limit 1
    """
    ).fetchone()[0]
    assert {} == json.loads(r)


@pytest.mark.skipif(
    sqlite_version < (3, 8, 11), reason="matchinfo 'b' was added in SQLite 3.8.11"
)
def test_annotate_matchinfo_b(conn):
    r = conn.execute(
        """
        select annotate_matchinfo(matchinfo(search, 'pcb'), 'pcb')
        from search where search match ?
    """,
        ["something ferret"],
    ).fetchone()[0]
    expected = {
        "title": "Bitfield showing which phrases occur in which columns",
        "value": [1, 3],
        "decoded": {
            "phrase_0": "10000000000000000000000000000000",
            "phrase_1": "11000000000000000000000000000000",
        },
        "phrases": [
            {
                "phrase_index": 0,
                "phrase": None,
                "column_indexes": [0],
                "columns": [{"column_index": 0, "column": None}],
                "unrecognized_columns": [],
            },
            {
                "phrase_index": 1,
                "phrase": None,
                "column_indexes": [0, 1],
                "columns": [
                    {"column_index": 0, "column": None},
                    {"column_index": 1, "column": None},
                ],
                "unrecognized_columns": [],
            },
        ],
    }
    assert expected == json.loads(r)["b"]


@pytest.mark.skipif(
    sqlite_version < (3, 8, 10), reason="matchinfo 'y' was added in SQLite 3.8.10"
)
def test_annotate_matchinfo_y(conn):
    r = conn.execute(
        """
        select annotate_matchinfo(matchinfo(search, 'pcy'), 'pcy')
        from search where search match ?
    """,
        ["something ferret"],
    ).fetchone()[0]
    expected = {
        "value": [
            {
                "phrase_index": 0,
                "phrase": None,
                "column_index": 0,
                "column": None,
                "hits_for_phrase_in_col": 1,
                "idx": 2,
            },
            {
                "phrase_index": 0,
                "phrase": None,
                "column_index": 1,
                "column": None,
                "hits_for_phrase_in_col": 0,
                "idx": 3,
            },
            {
                "phrase_index": 1,
                "phrase": None,
                "column_index": 0,
                "column": None,
                "hits_for_phrase_in_col": 1,
                "idx": 4,
            },
            {
                "phrase_index": 1,
                "phrase": None,
                "column_index": 1,
                "column": None,
                "hits_for_phrase_in_col": 2,
                "idx": 5,
            },
        ],
        "title": "Usable phrase matches for each phrase/column combination",
    }
    assert expected == json.loads(r)["y"]


@pytest.fixture
def wide_conn():
    conn = sqlite3.connect(":memory:")
    register_functions(conn)
    columns = ["c{}".format(i) for i in range(33)]
    conn.execute(
        "CREATE VIRTUAL TABLE wide USING fts4({})".format(", ".join(columns))
    )
    values = [""] * 33
    values[0] = "alpha"
    values[32] = "zeta"
    conn.execute(
        "INSERT INTO wide ({}) VALUES ({})".format(
            ", ".join(columns), ", ".join(["?"] * 33)
        ),
        values,
    )
    return conn, columns


def _matchinfo(conn, table, format_string, query):
    return conn.execute(
        "select matchinfo({}, ?) from {} where {} match ?".format(
            table, table, table
        ),
        [format_string, query],
    ).fetchone()[0]


def test_annotate_matchinfo_33_columns_distinguishes_first_last_and_unhit(wide_conn):
    conn, columns = wide_conn
    phrases = ["alpha", "zeta", "gamma"]
    buf = _matchinfo(conn, "wide", "pcb", "alpha OR zeta OR gamma")
    result = _annotate_matchinfo(buf, "pcb", columns=columns, phrases=phrases)
    b = result["b"]
    # Two 32-bit chunks per phrase: column 32 lands in chunk 2, bit 0
    assert b["value"] == [1, 0, 0, 1, 0, 0]
    assert b["phrases"] == [
        {
            "phrase_index": 0,
            "phrase": "alpha",
            "column_indexes": [0],
            "columns": [{"column_index": 0, "column": "c0"}],
            "unrecognized_columns": [],
        },
        {
            "phrase_index": 1,
            "phrase": "zeta",
            "column_indexes": [32],
            "columns": [{"column_index": 32, "column": "c32"}],
            "unrecognized_columns": [],
        },
        {
            "phrase_index": 2,
            "phrase": "gamma",
            "column_indexes": [],
            "columns": [],
            "unrecognized_columns": [],
        },
    ]
    assert "unrecognized" not in result


def test_annotate_matchinfo_same_word_in_two_phrases_tracked_separately(conn):
    buf = _matchinfo(conn, "search", "pcb", '"dog dog" OR dog')
    result = _annotate_matchinfo(
        buf,
        "pcb",
        columns=["title", "body"],
        phrases=['"dog dog"', "dog"],
    )
    phrases = result["b"]["phrases"]
    # The two-word phrase only hits the body column; the single word hits both
    assert phrases[0]["phrase"] == '"dog dog"'
    assert phrases[0]["column_indexes"] == [1]
    assert phrases[0]["columns"] == [{"column_index": 1, "column": "body"}]
    assert phrases[1]["phrase"] == "dog"
    assert phrases[1]["column_indexes"] == [0, 1]


def test_annotate_matchinfo_names_statistics_sections(conn):
    buf = _matchinfo(conn, "search", "pcxnals", "dog")
    result = _annotate_matchinfo(
        buf, "pcxnals", columns=["title", "body"], phrases=["dog"]
    )
    assert result["x"]["value"][0]["phrase"] == "dog"
    assert result["x"]["value"][0]["column"] == "title"
    assert result["x"]["value"][1]["column"] == "body"
    assert result["a"]["value"][0]["column"] == "title"
    assert result["l"]["value"][1]["column"] == "body"
    assert result["s"]["value"][0]["column"] == "title"
    assert "unrecognized" not in result


def test_annotate_matchinfo_reports_unmappable_indexes(wide_conn):
    conn, _ = wide_conn
    buf = _matchinfo(conn, "wide", "pcb", "alpha OR zeta")
    result = _annotate_matchinfo(
        buf, "pcb", columns=["c0"], phrases=["alpha"]
    )
    zeta_phrase = result["b"]["phrases"][1]
    assert zeta_phrase["phrase"] is None
    assert zeta_phrase["column_indexes"] == [32]
    assert zeta_phrase["columns"] == [{"column_index": 32, "column": None}]
    assert zeta_phrase["unrecognized_columns"] == [
        {
            "column_index": 32,
            "reason": "column index 32 is out of range for directory of length 1",
        }
    ]
    assert result["unrecognized"] == {
        "columns": [
            {
                "index": 32,
                "reason": "column index 32 is out of range for directory of length 1",
                "sections": ["b"],
            }
        ],
        "phrases": [
            {
                "index": 1,
                "reason": "phrase index 1 is out of range for directory of length 1",
                "sections": ["b"],
            }
        ],
    }


def test_annotate_matchinfo_unmappable_indexes_in_x_section(wide_conn):
    conn, _ = wide_conn
    buf = _matchinfo(conn, "wide", "pcx", "alpha OR zeta")
    result = _annotate_matchinfo(buf, "pcx", columns=["c0"])
    zeta_entry = [
        entry
        for entry in result["x"]["value"]
        if entry["phrase_index"] == 1 and entry["column_index"] == 32
    ][0]
    assert zeta_entry["column"] is None
    assert result["unrecognized"]["columns"][0]["sections"] == ["x"]


def test_annotate_matchinfo_rejects_non_sequence_directories(wide_conn):
    conn, _ = wide_conn
    buf = _matchinfo(conn, "wide", "pcb", "alpha")
    with pytest.raises(TypeError):
        _annotate_matchinfo(buf, "pcb", columns="c0")
    with pytest.raises(TypeError):
        _annotate_matchinfo(buf, "pcb", phrases=123)


def test_annotate_matchinfo_wrapped_function_round_trip(wide_conn):
    from sqlite_fts4 import annotate_matchinfo

    conn, columns = wide_conn
    buf = _matchinfo(conn, "wide", "pcb", "alpha OR zeta")
    result = json.loads(
        annotate_matchinfo(
            buf, "pcb", columns=columns, phrases=["alpha", "zeta"]
        )
    )
    assert result["b"]["phrases"][0]["columns"] == [
        {"column_index": 0, "column": "c0"}
    ]
    assert result["b"]["phrases"][1]["columns"] == [
        {"column_index": 32, "column": "c32"}
    ]
