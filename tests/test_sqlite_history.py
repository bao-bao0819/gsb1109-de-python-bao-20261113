import sqlite3

import pytest
import sqlite_utils
from sqlite_history.sql import (
    commit_infrastructure_sql,
    history_table_sql,
    triggers_sql,
)
from sqlite_history.cli import run
from unittest.mock import ANY

import sqlite_history


def test_create_history_table():
    db = sqlite_utils.Database(memory=True)
    db["test"].create({"id": int, "name": str}, pk="id")
    db.executescript(history_table_sql("test", (("id", "integer"), ("name", "text"))))
    assert db["_test_history"].schema == (
        "CREATE TABLE _test_history (\n"
        "    _rowid INTEGER,\n"
        "    id integer,\n"
        "    name text,\n"
        "    _version INTEGER,\n"
        "    _updated INTEGER,\n"
        "    _mask INTEGER,\n"
        "    _commit INTEGER\n"
        ")"
    )


def test_triggers():
    db = sqlite_utils.Database(memory=True)
    db["test"].create({"id": int, "name": str, "order": int}, pk="id")
    db.executescript(commit_infrastructure_sql())
    db.executescript(history_table_sql("test", (("id", "integer"), ("name", "text"))))
    db.executescript(triggers_sql("test", ["id", "name"]))
    db["test"].insert({"id": 1, "name": "Alice"})
    db["test"].insert({"id": 2, "name": "Bob"})
    db["test"].update(1, {"name": "Alice Smith"})
    assert list(db["test"].rows) == [
        {"id": 1, "name": "Alice Smith", "order": None},
        {"id": 2, "name": "Bob", "order": None},
    ]
    assert list(db["_test_history"].rows) == [
        {
            "_rowid": 1,
            "id": 1,
            "name": "Alice",
            "_version": 1,
            "_updated": ANY,
            "_mask": 3,
            "_commit": None,
        },
        {
            "_rowid": 2,
            "id": 2,
            "name": "Bob",
            "_version": 1,
            "_updated": ANY,
            "_mask": 3,
            "_commit": None,
        },
        {
            "_rowid": 1,
            "id": None,
            "name": "Alice Smith",
            "_version": 2,
            "_updated": ANY,
            "_mask": 2,
            "_commit": None,
        },
    ]


def test_triggers_delete():
    db = sqlite_utils.Database(memory=True)
    db["test"].create({"id": int, "name": str}, pk="id")
    db.executescript(commit_infrastructure_sql())
    db.executescript(history_table_sql("test", (("id", "integer"), ("name", "text"))))
    db.executescript(triggers_sql("test", ["id", "name"]))
    db["test"].insert({"id": 1, "name": "Alice"})
    db["test"].insert({"id": 2, "name": "Bob"})
    db["test"].update(1, {"name": "Alice Smith"})
    db["test"].delete(1)
    assert list(db["test"].rows) == [
        {"id": 2, "name": "Bob"},
    ]
    assert list(db["_test_history"].rows) == [
        {
            "_rowid": 1,
            "id": 1,
            "name": "Alice",
            "_version": 1,
            "_updated": ANY,
            "_mask": 3,
            "_commit": None,
        },
        {
            "_rowid": 2,
            "id": 2,
            "name": "Bob",
            "_version": 1,
            "_updated": ANY,
            "_mask": 3,
            "_commit": None,
        },
        {
            "_rowid": 1,
            "id": None,
            "name": "Alice Smith",
            "_version": 2,
            "_updated": ANY,
            "_mask": 2,
            "_commit": None,
        },
        {
            "_rowid": 1,
            "id": 1,
            "name": "Alice Smith",
            "_version": 3,
            "_updated": ANY,
            "_mask": -1,
            "_commit": None,
        },
    ]


def test_cli(tmpdir):
    test = tmpdir / "test.db"
    db = sqlite_utils.Database(str(test))
    db["test"].insert({"id": 1, "name": "Alice", "order": 1})
    db["test"].enable_fts(("name",), create_triggers=True)
    assert set(db.table_names()) == {
        "test",
        "test_fts",
        "test_fts_docsize",
        "test_fts_idx",
        "test_fts_data",
        "test_fts_config",
    }
    run([str(test), "--all"])
    assert set(db.table_names()) == {
        "test",
        "test_fts",
        "test_fts_docsize",
        "test_fts_idx",
        "test_fts_data",
        "test_fts_config",
        "_test_history",
        "_history_commits",
        "_history_commit_seq",
        "_history_state",
    }


def _warehouse_db():
    db = sqlite_utils.Database(memory=True)
    db["warehouse_a"].create({"sku": str, "qty": int}, pk="sku")
    db["warehouse_b"].create({"sku": str, "qty": int}, pk="sku")
    db["warehouse_a"].insert({"sku": "X1", "qty": 100})
    db["warehouse_b"].insert({"sku": "X1", "qty": 0})
    sqlite_history.configure_history(db.conn, "warehouse_a")
    sqlite_history.configure_history(db.conn, "warehouse_b")
    return db


def test_cross_table_transfer_shares_one_commit_id():
    db = _warehouse_db()
    with sqlite_history.tracked(db.conn, note="transfer 1") as tx:
        db.conn.execute("UPDATE warehouse_a SET qty = qty - 30 WHERE sku = 'X1';")
        db.conn.execute("UPDATE warehouse_b SET qty = qty + 30 WHERE sku = 'X1';")
    commit_id = tx.commit_id
    assert commit_id == 1
    # Both history rows of the transfer carry the same cross-table id,
    # despite no pairing key or distinct timestamps being used.
    a_rows = list(db["_warehouse_a_history"].rows)
    b_rows = list(db["_warehouse_b_history"].rows)
    assert a_rows[-1]["_commit"] == commit_id
    assert b_rows[-1]["_commit"] == commit_id
    assert db["_history_commits"].get(commit_id)["_note"] == "transfer 1"
    assert sqlite_history.current_commit(db.conn) is None
    # The deduction and the credit are joinable as one logical posting.
    paired = list(
        db.conn.execute(
            """
            SELECT 'a' AS side, _mask, _commit FROM _warehouse_a_history
            WHERE _commit = ?
            UNION ALL
            SELECT 'b', _mask, _commit FROM _warehouse_b_history
            WHERE _commit = ?;
            """,
            (commit_id, commit_id),
        )
    )
    assert {row[0] for row in paired} == {"a", "b"}


def test_snapshot_before_and_after_commit():
    db = _warehouse_db()
    with sqlite_history.tracked(db.conn) as tx:
        db.conn.execute("UPDATE warehouse_a SET qty = qty - 30 WHERE sku = 'X1';")
        db.conn.execute("UPDATE warehouse_b SET qty = qty + 30 WHERE sku = 'X1';")
    commit_id = tx.commit_id
    before = {row["sku"]: row["qty"] for row in
              sqlite_history.snapshot_rows(db.conn, "warehouse_a", commit_id, mode="before")}
    after = {row["sku"]: row["qty"] for row in
             sqlite_history.snapshot_rows(db.conn, "warehouse_a", commit_id, mode="after")}
    before_b = {row["sku"]: row["qty"] for row in
                sqlite_history.snapshot_rows(db.conn, "warehouse_b", commit_id, mode="before")}
    after_b = {row["sku"]: row["qty"] for row in
               sqlite_history.snapshot_rows(db.conn, "warehouse_b", commit_id, mode="after")}
    assert before == {"X1": 100}
    assert after == {"X1": 70}
    assert before_b == {"X1": 0}
    assert after_b == {"X1": 30}


def test_rolled_back_transfer_leaves_no_committed_trace():
    db = _warehouse_db()
    with pytest.raises(RuntimeError):
        with sqlite_history.tracked(db.conn):
            db.conn.execute("UPDATE warehouse_a SET qty = qty - 30 WHERE sku = 'X1';")
            db.conn.execute("UPDATE warehouse_b SET qty = qty + 30 WHERE sku = 'X1';")
            raise RuntimeError("boom")
    # Live tables are untouched.
    assert list(db["warehouse_a"].rows) == [{"sku": "X1", "qty": 100}]
    assert list(db["warehouse_b"].rows) == [{"sku": "X1", "qty": 0}]
    # The history rows written inside the rolled-back transaction vanished
    # together with it; only the backfilled rows remain.
    assert all(row["_commit"] is None for row in db["_warehouse_a_history"].rows)
    assert all(row["_commit"] is None for row in db["_warehouse_b_history"].rows)
    assert db["_history_commits"].count == 0
    # The rolled-back id cannot be read as a committed snapshot.
    with pytest.raises(ValueError):
        sqlite_history.snapshot_rows(db.conn, "warehouse_a", 1)
    # The counter rewinds with the rollback, so the next transaction
    # reuses the id - safe because nothing stamped with it survived.
    with sqlite_history.tracked(db.conn) as tx:
        db.conn.execute("UPDATE warehouse_a SET qty = qty - 5 WHERE sku = 'X1';")
    assert tx.commit_id == 1
    assert sqlite_history.commit_exists(db.conn, 1)
    assert sqlite_history.snapshot_rows(
        db.conn, "warehouse_a", 1, mode="after"
    ) == [{"_rowid": 1, "sku": "X1", "qty": 95}]


def test_savepoint_undo_never_reaches_committed_view():
    db = _warehouse_db()
    with sqlite_history.tracked(db.conn) as tx:
        db.conn.execute("SAVEPOINT sp1;")
        db.conn.execute("UPDATE warehouse_a SET qty = qty - 40 WHERE sku = 'X1';")
        db.conn.execute("ROLLBACK TO SAVEPOINT sp1;")
        db.conn.execute("RELEASE SAVEPOINT sp1;")
        db.conn.execute("UPDATE warehouse_b SET qty = qty + 10 WHERE sku = 'X1';")
    commit_id = tx.commit_id
    # No history row for the undone deduction; the committed transfer only
    # touches warehouse_b.
    a_history = [row for row in db["_warehouse_a_history"].rows
                 if row["_commit"] == commit_id]
    b_history = [row for row in db["_warehouse_b_history"].rows
                 if row["_commit"] == commit_id]
    assert a_history == []
    assert len(b_history) == 1 and b_history[0]["_mask"] == 2
    snapshot = {row["sku"]: row["qty"] for row in
                sqlite_history.snapshot_rows(db.conn, "warehouse_a", commit_id)}
    assert snapshot == {"X1": 100}


def test_legacy_rows_with_null_commit_predate_every_snapshot():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE stock (sku TEXT PRIMARY KEY, qty INTEGER);")
    # Simulate a history table produced by the old, commit-unaware version.
    db.executescript(
        """
        CREATE TABLE _stock_history (
            _rowid INTEGER, sku TEXT, qty INTEGER,
            _version INTEGER, _updated INTEGER, _mask INTEGER
        );
        INSERT INTO stock VALUES ('X1', 50);
        INSERT INTO _stock_history VALUES (1, 'X1', 50, 1, 0, 3);
        """
    )
    # Reconfiguring migrates the legacy table in place.
    sqlite_history.configure_history(db, "stock")
    columns = [row[1] for row in db.execute("PRAGMA table_info(_stock_history);")]
    assert "_commit" in columns
    legacy = db.execute("SELECT _commit FROM _stock_history;").fetchall()
    assert all(row[0] is None for row in legacy)
    # A numbered commit on top is fully visible...
    with sqlite_history.tracked(db) as tx:
        db.execute("UPDATE stock SET qty = 25 WHERE sku = 'X1';")
    commit_id = tx.commit_id
    after = {row["sku"]: row["qty"]
             for row in sqlite_history.snapshot_rows(db, "stock", commit_id)}
    assert after == {"X1": 25}
    # ...and the NULL-commit legacy row is treated as predating every
    # snapshot, so the pre-posting balance reconstructs from it.
    before = {row["sku"]: row["qty"]
              for row in sqlite_history.snapshot_rows(db, "stock", commit_id, mode="before")}
    assert before == {"X1": 50}


def test_concurrent_connections_get_distinct_commit_ids(tmp_path):
    import threading

    path = str(tmp_path / "warehouse.db")
    db1 = sqlite3.connect(path, timeout=10)
    db1.execute("CREATE TABLE stock (sku TEXT PRIMARY KEY, qty INTEGER);")
    db1.execute("INSERT INTO stock VALUES ('X1', 100);")
    sqlite_history.configure_history(db1, "stock")
    db1.commit()
    db1.close()

    first_holds_lock = threading.Event()
    second_queued = threading.Event()
    ids = []

    def writer_a():
        conn = sqlite3.connect(path, timeout=10)
        with sqlite_history.tracked(conn, note="first") as tx:
            conn.execute("UPDATE stock SET qty = qty - 1 WHERE sku = 'X1';")
            ids.append(tx.commit_id)
            first_holds_lock.set()
            second_queued.wait(5)
            # Give connection B time to queue its blocked BEGIN IMMEDIATE
            # before we release the write lock.
            threading.Event().wait(0.5)
        conn.close()

    def writer_b():
        conn = sqlite3.connect(path, timeout=10)
        first_holds_lock.wait(5)
        second_queued.set()
        threading.Event().wait(0.2)
        # Blocks on the write lock until writer_a commits, then must take
        # the next id rather than collide.
        with sqlite_history.tracked(conn, note="second") as tx:
            conn.execute("UPDATE stock SET qty = qty - 1 WHERE sku = 'X1';")
            ids.append(tx.commit_id)
        conn.close()

    thread_a = threading.Thread(target=writer_a)
    thread_b = threading.Thread(target=writer_b)
    thread_a.start()
    thread_b.start()
    thread_a.join(10)
    thread_b.join(10)
    assert ids == [1, 2]
    reader = sqlite3.connect(path)
    rows = reader.execute(
        "SELECT DISTINCT _commit FROM _stock_history WHERE _commit IS NOT NULL ORDER BY 1;"
    ).fetchall()
    assert [row[0] for row in rows] == [1, 2]
    reader.close()


def test_untracked_writes_keep_null_commit():
    db = _warehouse_db()
    db.conn.execute("UPDATE warehouse_a SET qty = qty - 7 WHERE sku = 'X1';")
    rows = list(db["_warehouse_a_history"].rows)
    assert rows[-1]["_commit"] is None
    assert db["_history_commits"].count == 0
