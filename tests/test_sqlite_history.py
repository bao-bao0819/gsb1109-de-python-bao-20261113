import sqlite_utils
import sqlite3
from sqlite_history import (
    backfill,
    backfill_batch,
    backfill_status,
    configure_history,
)
from sqlite_history.sql import (
    history_table_sql,
    table_columns_and_types,
    triggers_sql,
)
from sqlite_history.cli import run
from unittest.mock import ANY


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
        "    _mask INTEGER\n"
        ")"
    )


def test_triggers():
    db = sqlite_utils.Database(memory=True)
    db["test"].create({"id": int, "name": str, "order": int}, pk="id")
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
        },
        {
            "_rowid": 2,
            "id": 2,
            "name": "Bob",
            "_version": 1,
            "_updated": ANY,
            "_mask": 3,
        },
        {
            "_rowid": 1,
            "id": None,
            "name": "Alice Smith",
            "_version": 2,
            "_updated": ANY,
            "_mask": 2,
        },
    ]


def test_triggers_delete():
    db = sqlite_utils.Database(memory=True)
    db["test"].create({"id": int, "name": str}, pk="id")
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
        },
        {
            "_rowid": 2,
            "id": 2,
            "name": "Bob",
            "_version": 1,
            "_updated": ANY,
            "_mask": 3,
        },
        {
            "_rowid": 1,
            "id": None,
            "name": "Alice Smith",
            "_version": 2,
            "_updated": ANY,
            "_mask": 2,
        },
        {
            "_rowid": 1,
            "id": 1,
            "name": "Alice Smith",
            "_version": 3,
            "_updated": ANY,
            "_mask": -1,
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
    }


def make_orders(db, count=6):
    db.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, status TEXT)")
    for i in range(1, count + 1):
        db.execute("INSERT INTO orders (id, status) VALUES (?, 'new')", (i,))
    db.commit()


def install_triggers(db, table="orders"):
    columns_and_types = table_columns_and_types(db, table)
    db.executescript(history_table_sql(table, columns_and_types))
    db.executescript(triggers_sql(table, [c[0] for c in columns_and_types]))


def history_rows(db, table="orders"):
    return db.execute(
        f"SELECT _rowid, id, status, _version, _mask FROM _{table}_history "
        "ORDER BY _rowid, _version"
    ).fetchall()


def test_batched_backfill(tmpdir):
    path = str(tmpdir / "data.db")
    db = sqlite3.connect(path)
    make_orders(db, 10)
    configure_history(db, "orders", batch_size=4)
    rows = history_rows(db)
    assert rows == [(i, i, "new", 1, 3) for i in range(1, 11)]
    status = backfill_status(db, "orders")
    assert status == {"last_rowid": 10, "max_rowid": 10, "done": True}
    # Running again is a no-op
    assert backfill(db, "orders", batch_size=4) == 0
    assert len(history_rows(db)) == 10
    db.close()


def test_batched_backfill_empty_table(tmpdir):
    db = sqlite3.connect(str(tmpdir / "data.db"))
    db.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, status TEXT)")
    configure_history(db, "orders", batch_size=2)
    assert backfill_status(db, "orders")["done"]
    db.close()


def test_backfill_resume_after_concurrent_writes(tmpdir):
    path = str(tmpdir / "data.db")
    db = sqlite3.connect(path)
    make_orders(db, 6)
    install_triggers(db)
    # First batch only snapshots rows 1 and 2, then "crashes"
    assert backfill_batch(db, "orders", batch_size=2) == 2
    assert backfill_status(db, "orders") == {
        "last_rowid": 2,
        "max_rowid": 6,
        "done": False,
    }
    # Business writes keep flowing on another connection
    db2 = sqlite3.connect(path)
    db2.execute("UPDATE orders SET status = 'paid' WHERE id = 3")
    db2.execute("UPDATE orders SET status = 'shipped' WHERE id = 3")
    db2.execute("DELETE FROM orders WHERE id = 4")
    db2.execute("INSERT INTO orders (id, status) VALUES (7, 'new')")
    db2.commit()
    db2.close()
    db.close()
    # Resume on a fresh connection
    db3 = sqlite3.connect(path)
    backfill(db3, "orders", batch_size=2)
    assert backfill_status(db3, "orders")["done"]
    rows = history_rows(db3)
    # Rows 1, 2, 5, 6: full snapshots from the backfill
    # Row 3: only the two trigger records - no fabricated scan-time snapshot
    # Row 4: only the delete record with the old values
    # Row 7: inserted after the backfill started, captured by the insert trigger
    assert rows == [
        (1, 1, "new", 1, 3),
        (2, 2, "new", 1, 3),
        (3, None, "paid", 1, 2),
        (3, None, "shipped", 2, 2),
        (4, 4, "new", 1, -1),
        (5, 5, "new", 1, 3),
        (6, 6, "new", 1, 3),
        (7, 7, "new", 1, 3),
    ]
    # Versions per rowid are contiguous starting at 1
    versions = {}
    for rowid, _, _, version, _ in rows:
        versions.setdefault(rowid, []).append(version)
    for rowid_versions in versions.values():
        assert rowid_versions == list(range(1, len(rowid_versions) + 1))
    db3.close()


def test_update_before_backfill_gets_version_1():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, status TEXT)")
    db.execute("INSERT INTO orders (id, status) VALUES (1, 'new')")
    install_triggers(db)
    # Update lands before any backfill - version must be 1, not NULL
    db.execute("UPDATE orders SET status = 'paid' WHERE id = 1")
    assert history_rows(db) == [(1, None, "paid", 1, 2)]
    db.close()


def test_reinserted_rowid_continues_version_chain():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, status TEXT)")
    install_triggers(db)
    db.execute("INSERT INTO orders (id, status) VALUES (1, 'new')")
    db.execute("DELETE FROM orders WHERE id = 1")
    # rowid 1 is reused - the new insert must not claim version 1 again
    db.execute("INSERT INTO orders (id, status) VALUES (1, 'replacement')")
    assert history_rows(db) == [
        (1, 1, "new", 1, 3),
        (1, 1, "new", 2, -1),
        (1, 1, "replacement", 3, 3),
    ]
    db.close()


def test_cli_batch_size_and_resume(tmpdir):
    path = str(tmpdir / "test.db")
    db = sqlite3.connect(path)
    make_orders(db, 5)
    db.close()
    run([path, "orders", "--batch-size", "2"])
    db = sqlite3.connect(path)
    assert backfill_status(db, "orders")["done"]
    assert len(history_rows(db)) == 5
    db.close()
