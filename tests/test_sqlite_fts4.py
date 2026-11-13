import sqlite3
from sqlite_fts4 import (
    register_functions,
    decode_matchinfo,
    migrate_matchinfo_batch,
)
import pytest
import json
import struct
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
                    "column_index": 0,
                    "hits_this_column_this_row": 1,
                    "hits_this_column_all_rows": 1,
                    "docs_with_hits": 1,
                    "idxs": [2, 3, 4],
                },
                {
                    "phrase_index": 0,
                    "column_index": 1,
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
                {"column_index": 0, "average_num_tokens": 4, "idx": 9},
                {"column_index": 1, "average_num_tokens": 5, "idx": 10},
            ],
        },
        "l": {
            "title": "Number of tokens in each column of the current row of the FTS4 table",
            "value": [
                {"column_index": 0, "num_tokens": 5, "idx": 11},
                {"column_index": 1, "num_tokens": 5, "idx": 12},
            ],
        },
        "s": {
            "title": "Length of longest subsequence of phrase matching each column",
            "value": [
                {"column_index": 0, "length_phrase_subsequence_match": 1, "idx": 13},
                {"column_index": 1, "length_phrase_subsequence_match": 1, "idx": 14},
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
                "column_index": 0,
                "hits_for_phrase_in_col": 1,
                "idx": 2,
            },
            {
                "phrase_index": 0,
                "column_index": 1,
                "hits_for_phrase_in_col": 0,
                "idx": 3,
            },
            {
                "phrase_index": 1,
                "column_index": 0,
                "hits_for_phrase_in_col": 1,
                "idx": 4,
            },
            {
                "phrase_index": 1,
                "column_index": 1,
                "hits_for_phrase_in_col": 2,
                "idx": 5,
            },
        ],
        "title": "Usable phrase matches for each phrase/column combination",
    }
    assert expected == json.loads(r)["y"]


def _pack(values, byte_order):
    return struct.pack(
        ("<" if byte_order == "little" else ">") + "I" * len(values), *values
    )


# matchinfo(search, 'pcx') for the "dog" query against the fixture table
DOG_PCX = [1, 2, 1, 1, 1, 2, 4, 2]
DOG_PCX_CHECKSUMS = {
    "column_total_hits": [1, 4],
    "column_docs_with_hits": [1, 2],
    "total_hits": 5,
}


@pytest.mark.parametrize("byte_order", ["little", "big"])
def test_migrate_matchinfo_batch_detects_byte_order(byte_order):
    (result,) = migrate_matchinfo_batch(
        [
            {
                "id": "dog-query",
                "buf": _pack(DOG_PCX, byte_order),
                "num_phrases": 1,
                "num_columns": 2,
                "checksums": DOG_PCX_CHECKSUMS,
            }
        ]
    )
    assert result["status"] == "ok"
    assert result["byte_order"] == byte_order
    assert result["values"] == DOG_PCX
    assert result["id"] == "dog-query"
    assert result["verified_checksums"] == sorted(DOG_PCX_CHECKSUMS)


def test_migrate_matchinfo_batch_never_assumes_native_order():
    # A big-endian export must decode as big-endian even on a little-endian
    # machine and vice versa
    foreign_order = "big" if sys.byteorder == "little" else "little"
    (result,) = migrate_matchinfo_batch(
        [
            {
                "buf": _pack(DOG_PCX, foreign_order),
                "num_phrases": 1,
                "num_columns": 2,
            }
        ]
    )
    assert result["status"] == "ok"
    assert result["byte_order"] == foreign_order
    assert result["values"] == DOG_PCX


def test_migrate_matchinfo_batch_shape_errors():
    results = migrate_matchinfo_batch(
        [
            # Not a multiple of 4 bytes
            {"buf": b"\x01\x00\x00", "num_phrases": 1, "num_columns": 2},
            # Wrong integer count for 'pcx' with 1 phrase and 2 columns
            {
                "buf": _pack([1, 2, 1], "little"),
                "num_phrases": 1,
                "num_columns": 2,
            },
        ]
    )
    assert [r["status"] for r in results] == ["invalid", "invalid"]
    assert "multiple of 4" in results[0]["error"]
    assert "requires 8" in results[1]["error"]


def test_migrate_matchinfo_batch_hit_relationship_errors():
    # hits_this_column_this_row (5) exceeds hits_this_column_all_rows (2)
    # and docs_with_hits (6) exceeds hits_this_column_all_rows too
    (result,) = migrate_matchinfo_batch(
        [
            {
                "buf": _pack([1, 1, 5, 2, 6], "little"),
                "num_phrases": 1,
                "num_columns": 1,
            }
        ]
    )
    assert result["status"] == "invalid"
    assert result["error"] == "no byte order interpretation passes validation"
    little_problems = result["problems"]["little"]
    assert any("hits this row 5 exceed hits all rows 2" in p for p in little_problems)
    assert any("docs with hits 6 exceed hits all rows 2" in p for p in little_problems)


def test_migrate_matchinfo_batch_checksum_contradiction():
    (result,) = migrate_matchinfo_batch(
        [
            {
                "buf": _pack(DOG_PCX, "little"),
                "num_phrases": 1,
                "num_columns": 2,
                "checksums": {"total_hits": 999},
            }
        ]
    )
    assert result["status"] == "invalid"
    assert result["checksum_mismatches"] == [
        {"name": "total_hits", "expected": 999, "actual": 5}
    ]


def test_migrate_matchinfo_batch_all_zero_accepted_either_order():
    # All-zero segments decode identically under both byte orders
    (result,) = migrate_matchinfo_batch(
        [
            {
                "buf": _pack([0, 0, 0], "little"),
                "num_phrases": 0,
                "num_columns": 0,
                "format": "pcn",
                "checksums": {"row_count": 0},
            }
        ]
    )
    assert result["status"] == "ok"
    assert result["byte_order"] == "either"
    assert result["values"] == [0, 0, 0]


def test_migrate_matchinfo_batch_ambiguous_lists_distinguishing_checksums():
    # p and c are 0 (byte-order palindromes) so both interpretations are
    # structurally valid, but 'n' decodes differently: 5 vs 83886080
    record = {
        "buf": _pack([0, 0, 5], "little"),
        "num_phrases": 0,
        "num_columns": 0,
        "format": "pcn",
    }
    (result,) = migrate_matchinfo_batch([record])
    assert result["status"] == "ambiguous"
    assert result["candidates"] == {"little": [0, 0, 5], "big": [0, 0, 83886080]}
    assert result["differing_indices"] == [2]
    assert result["distinguishing_checksums"] == [
        {"name": "row_count", "little": 5, "big": 83886080}
    ]
    # Supplying the distinguishing checksum resolves the ambiguity
    (resolved,) = migrate_matchinfo_batch(
        [dict(record, checksums={"row_count": 5})]
    )
    assert resolved["status"] == "ok"
    assert resolved["byte_order"] == "little"
    assert resolved["values"] == [0, 0, 5]
    (resolved_big,) = migrate_matchinfo_batch(
        [dict(record, checksums={"row_count": 83886080})]
    )
    assert resolved_big["status"] == "ok"
    assert resolved_big["byte_order"] == "big"
