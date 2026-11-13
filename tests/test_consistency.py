import sqlite3

import pytest

from sqlite_fts4.consistency import (
    ConsistencyError,
    audit,
    format_report,
    repair,
    result_to_json,
)


def make_conn(ddl_docs, ddl_fts):
    conn = sqlite3.connect(":memory:")
    conn.execute(ddl_docs)
    conn.execute(ddl_fts)
    return conn


def seed_reused_rowid(conn):
    # docs rowids 1 and 2 are replaced with new content reusing the same
    # rowids, but the FTS index is never told.  Row 3 stays untouched.
    conn.executemany(
        "INSERT INTO docs VALUES (?, ?, ?)",
        [
            (1, "alpha apples", "old body alpha"),
            (2, "beta bananas", "second doc"),
            (3, "quiet keep", "stable row"),
        ],
    )
    conn.execute("INSERT INTO docs_fts(docs_fts) VALUES('rebuild')")
    conn.execute("DELETE FROM docs WHERE id=1")
    conn.execute(
        "INSERT INTO docs VALUES (1, 'gamma grapes', 'brand new body')"
    )
    conn.execute("DELETE FROM docs WHERE id=2")
    conn.execute(
        "INSERT INTO docs VALUES (2, 'delta dates', 'fresh content here')"
    )


@pytest.fixture
def conn():
    conn = make_conn(
        "CREATE TABLE docs(id INTEGER PRIMARY KEY, title, body)",
        "CREATE VIRTUAL TABLE docs_fts USING fts4("
        "title, body, content=docs)",
    )
    seed_reused_rowid(conn)
    yield conn
    conn.close()


def terms_for(conn, table, term):
    return [
        row[0]
        for row in conn.execute(
            "SELECT rowid FROM {t} WHERE {t} MATCH ?".format(t=table),
            ['"' + term.replace('"', '""') + '"'],
        )
    ]


def test_stale_and_missing_terms_are_evidenced_per_row(conn):
    result = audit(conn, "docs_fts")
    assert result.affected_rowids == [1, 2]
    by_key = {(m.rowid, m.column_index): m for m in result.mismatches}

    title_row1 = by_key[(1, 0)]
    assert dict(title_row1.stale) == {"alpha": 1, "apples": 1}
    assert dict(title_row1.missing) == {"gamma": 1, "grapes": 1}

    body_row2 = by_key[(2, 1)]
    assert dict(body_row2.stale) == {"doc": 1, "second": 1}
    assert dict(body_row2.missing) == {
        "content": 1,
        "fresh": 1,
        "here": 1,
    }
    # The untouched row produces no evidence.
    assert all(m.rowid != 3 for m in result.mismatches)


def test_repair_removes_old_terms_and_indexes_new_ones(conn):
    result = audit(conn, "docs_fts")
    assert repair(conn, result) == [1, 2]

    for old_term in ("alpha", "apples", "old", "beta", "bananas", "second"):
        assert terms_for(conn, "docs_fts", old_term) == []
    assert terms_for(conn, "docs_fts", "gamma") == [1]
    assert terms_for(conn, "docs_fts", "grapes") == [1]
    assert terms_for(conn, "docs_fts", "delta") == [2]
    # The untouched row is still fully searchable.
    assert terms_for(conn, "docs_fts", "quiet") == [3]
    assert terms_for(conn, "docs_fts", "stable") == [3]


def test_repair_leaves_content_table_untouched(conn):
    before = conn.execute("SELECT id, title, body FROM docs ORDER BY id").fetchall()
    repair(conn, audit(conn, "docs_fts"))
    after = conn.execute("SELECT id, title, body FROM docs ORDER BY id").fetchall()
    assert before == after


def test_second_audit_and_repair_have_no_new_fixes(conn):
    repair(conn, audit(conn, "docs_fts"))
    recheck = audit(conn, "docs_fts")
    assert recheck.mismatches == ()
    assert repair(conn, recheck) == []


def test_scratch_tables_are_dropped(conn):
    audit(conn, "docs_fts")
    names = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE '_fts4_audit_%'"
        )
    }
    assert names == set()


def test_shared_terms_keep_correct_global_counts(conn):
    # Replace row 1 again, this time sharing a term with the replacement.
    conn.execute("DELETE FROM docs WHERE id=1")
    conn.execute(
        "INSERT INTO docs VALUES (1, 'gamma shared', 'brand shared body')"
    )
    repair(conn, audit(conn, "docs_fts"))

    aux = "_shared_aux"
    conn.execute("CREATE VIRTUAL TABLE {} USING fts4aux(docs_fts)".format(aux))
    counts = dict(
        conn.execute(
            "SELECT term, occurrences FROM {} WHERE col='*'".format(aux)
        )
    )
    conn.execute("DROP TABLE {}".format(aux))
    # 'shared' occurs twice in row 1 only: no double counting from the
    # delete/re-index cycle.
    assert counts["shared"] == 2
    assert terms_for(conn, "docs_fts", "shared") == [1]


def test_orphan_posting_when_content_row_is_gone(conn):
    # Row 3's content disappears while its postings stay in the index.
    conn.execute("DELETE FROM docs WHERE id=3")
    result = audit(conn, "docs_fts")
    orphan = [m for m in result.mismatches if m.rowid == 3]
    assert orphan
    assert dict(orphan[0].stale)  # everything indexed is now stale

    repair(conn, result)
    assert terms_for(conn, "docs_fts", "quiet") == []
    assert terms_for(conn, "docs_fts", "stable") == []
    # The content row is not resurrected.
    assert conn.execute("SELECT count(*) FROM docs WHERE id=3").fetchone()[0] == 0


def test_failure_rolls_back_index_and_content():
    # Autocommit connection so repair owns (and rolls back) the transaction.
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("CREATE TABLE docs(id INTEGER PRIMARY KEY, title, body)")
    conn.execute(
        "CREATE VIRTUAL TABLE docs_fts USING fts4("
        "title, body, content=docs)"
    )
    seed_reused_rowid(conn)
    # Abort the "restore current content" step, after the stale rewrite and
    # the FTS delete have already run.
    conn.execute(
        "CREATE TRIGGER fail_restore BEFORE UPDATE ON docs "
        "WHEN NEW.title = 'gamma grapes' "
        "BEGIN SELECT RAISE(ABORT, 'injected restore failure'); END"
    )
    result = audit(conn, "docs_fts")
    with pytest.raises(sqlite3.DatabaseError):
        repair(conn, result)

    # The whole repair transaction rolled back.
    assert conn.in_transaction is False
    assert conn.execute(
        "SELECT title FROM docs WHERE id=1"
    ).fetchone()[0] == "gamma grapes"
    assert terms_for(conn, "docs_fts", "alpha") == [1]
    conn.close()


def test_porter_tokenizer_round_trip():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE docs2(id INTEGER PRIMARY KEY, t)")
    conn.execute(
        "CREATE VIRTUAL TABLE ft2 USING fts4("
        "t, content=docs2, tokenize=porter)"
    )
    conn.execute("INSERT INTO docs2 VALUES (1, 'running jumps quickly')")
    conn.execute("INSERT INTO ft2(ft2) VALUES('rebuild')")
    conn.execute("DELETE FROM docs2 WHERE id=1")
    conn.execute("INSERT INTO docs2 VALUES (1, 'walking slowly')")

    result = audit(conn, "ft2")
    assert result.affected_rowids == [1]
    repair(conn, result)
    assert audit(conn, "ft2").mismatches == ()
    # Porter stems of the old text are gone.
    assert terms_for(conn, "ft2", "run") == []
    assert terms_for(conn, "ft2", "jump") == []
    conn.close()


def test_self_content_table_is_rejected():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE VIRTUAL TABLE ft USING fts4(t)")
    conn.execute("INSERT INTO ft VALUES('alpha')")
    with pytest.raises(ConsistencyError):
        audit(conn, "ft")
    conn.close()


def test_contentless_table_is_rejected():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE VIRTUAL TABLE cl USING fts4(content='', t)")
    with pytest.raises(ConsistencyError):
        audit(conn, "cl")
    conn.close()


def test_unknown_table_is_rejected(conn):
    with pytest.raises(ConsistencyError):
        audit(conn, "does_not_exist")


def test_audit_does_not_leave_a_transaction_open(conn):
    # The fixture's own DML leaves a caller-owned transaction; audit must not
    # commit it behind the caller's back.
    assert conn.in_transaction is True
    audit(conn, "docs_fts")
    assert conn.in_transaction is True
    # Once the caller commits, a fresh audit opens no lingering transaction.
    conn.commit()
    audit(conn, "docs_fts")
    assert conn.in_transaction is False


def test_repair_persists_to_disk(tmp_path):
    # The default sqlite3 module opens implicit transactions for DML; the
    # audit scratch work must not prevent repair from committing.
    db_path = tmp_path / "articles.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE docs(id INTEGER PRIMARY KEY, title, body)")
    conn.execute(
        "CREATE VIRTUAL TABLE docs_fts USING fts4("
        "title, body, content=docs)"
    )
    conn.execute("INSERT INTO docs VALUES (1, 'alpha apples', 'old body')")
    conn.execute("INSERT INTO docs_fts(docs_fts) VALUES('rebuild')")
    conn.execute("DELETE FROM docs WHERE id=1")
    conn.execute("INSERT INTO docs VALUES (1, 'gamma grapes', 'new body')")
    conn.commit()

    repair(conn, audit(conn, "docs_fts"))
    conn.close()

    reopened = sqlite3.connect(str(db_path))
    assert terms_for(reopened, "docs_fts", "alpha") == []
    assert terms_for(reopened, "docs_fts", "gamma") == [1]
    # Re-running the same batch on a fresh connection finds nothing to fix.
    recheck = audit(reopened, "docs_fts")
    assert recheck.mismatches == ()
    reopened.close()


def test_report_and_json_include_evidence(conn):
    result = audit(conn, "docs_fts")
    text = format_report(result)
    assert "docs_fts" in text
    assert "rowid 1" in text
    assert "alpha" in text

    payload = result_to_json(result)
    assert '"fts_table": "docs_fts"' in payload
    assert '"rowid": 1' in payload
    import json

    parsed = json.loads(payload)
    assert parsed["affected_rowids"] == [1, 2]
