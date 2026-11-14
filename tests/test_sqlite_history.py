import sqlite_history
from sqlite_history import configure_history, decode_mask, schema_versions
import sqlite_utils
from sqlite_history.sql import history_table_sql, triggers_sql
from sqlite_history.cli import run
from unittest.mock import ANY
import pytest


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
        "    _schema_version INTEGER\n"
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
            "_schema_version": 1,
        },
        {
            "_rowid": 2,
            "id": 2,
            "name": "Bob",
            "_version": 1,
            "_updated": ANY,
            "_mask": 3,
            "_schema_version": 1,
        },
        {
            "_rowid": 1,
            "id": None,
            "name": "Alice Smith",
            "_version": 2,
            "_updated": ANY,
            "_mask": 2,
            "_schema_version": 1,
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
            "_schema_version": 1,
        },
        {
            "_rowid": 2,
            "id": 2,
            "name": "Bob",
            "_version": 1,
            "_updated": ANY,
            "_mask": 3,
            "_schema_version": 1,
        },
        {
            "_rowid": 1,
            "id": None,
            "name": "Alice Smith",
            "_version": 2,
            "_updated": ANY,
            "_mask": 2,
            "_schema_version": 1,
        },
        {
            "_rowid": 1,
            "id": 1,
            "name": "Alice Smith",
            "_version": 3,
            "_updated": ANY,
            "_mask": -1,
            "_schema_version": 1,
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
        "_test_history_schema",
    }


def test_configure_history_registers_schema_version():
    db = sqlite_utils.Database(memory=True)
    db["people"].create({"id": int, "name": str}, pk="id")
    db["people"].insert({"id": 1, "name": "Alice"})
    configure_history(db, "people")
    assert schema_versions(db, "people") == {1: [("id", "INTEGER"), ("name", "TEXT")]}
    assert db.execute("SELECT _schema_version FROM _people_history").fetchall() == [(1,)]


def test_configure_history_is_idempotent():
    db = sqlite_utils.Database(memory=True)
    db["t"].create({"id": int, "name": str}, pk="id")
    db["t"].insert({"id": 1, "name": "Alice"})
    configure_history(db, "t")
    configure_history(db, "t")
    assert schema_versions(db, "t") == {1: [("id", "INTEGER"), ("name", "TEXT")]}
    # Backfill only happened once, triggers still record new writes
    assert db.execute("SELECT COUNT(*) FROM _t_history").fetchone()[0] == 1
    db["t"].insert({"id": 2, "name": "Bob"})
    assert db.execute("SELECT COUNT(*) FROM _t_history").fetchone()[0] == 2


def test_schema_evolution_split_column_and_reorder():
    db = sqlite_utils.Database(memory=True)
    db["shipments"].create({"id": int, "name": str, "weight": float}, pk="id")
    configure_history(db, "shipments")
    db["shipments"].insert({"id": 1, "name": "crate", "weight": 10.5})
    db["shipments"].update(1, {"weight": 11.0})
    # Business schema change: reorder columns and split weight into
    # gross_weight and net_weight
    db.execute(
        "CREATE TABLE shipments_new (id INTEGER PRIMARY KEY, gross_weight REAL, net_weight REAL, name TEXT)"
    )
    db.execute(
        "INSERT INTO shipments_new (id, gross_weight, name) SELECT id, weight, name FROM shipments"
    )
    db.execute("DROP TABLE shipments")
    db.execute("ALTER TABLE shipments_new RENAME TO shipments")
    configure_history(db, "shipments")
    assert schema_versions(db, "shipments") == {
        1: [("id", "INTEGER"), ("name", "TEXT"), ("weight", "REAL")],
        2: [
            ("id", "INTEGER"),
            ("gross_weight", "REAL"),
            ("net_weight", "REAL"),
            ("name", "TEXT"),
        ],
    }
    # The history table keeps the old column and gains the new ones
    assert [
        row[1] for row in db.execute("PRAGMA table_info(_shipments_history)").fetchall()
    ] == [
        "_rowid",
        "id",
        "name",
        "weight",
        "_version",
        "_updated",
        "_mask",
        "_schema_version",
        "gross_weight",
        "net_weight",
    ]
    db["shipments"].insert(
        {"id": 2, "gross_weight": 12.0, "net_weight": 10.0, "name": "box"}
    )
    db["shipments"].update(2, {"net_weight": 9.5})
    rows = list(db["_shipments_history"].rows)
    assert rows == [
        # Old rows keep the old fact in weight - gross_weight/net_weight
        # are left NULL, not filled with two identical numbers
        {
            "_rowid": 1,
            "id": 1,
            "name": "crate",
            "weight": 10.5,
            "_version": 1,
            "_updated": ANY,
            "_mask": 7,
            "_schema_version": 1,
            "gross_weight": None,
            "net_weight": None,
        },
        {
            "_rowid": 1,
            "id": None,
            "name": None,
            "weight": 11.0,
            "_version": 2,
            "_updated": ANY,
            "_mask": 4,
            "_schema_version": 1,
            "gross_weight": None,
            "net_weight": None,
        },
        {
            "_rowid": 2,
            "id": 2,
            "name": "box",
            "weight": None,
            "_version": 1,
            "_updated": ANY,
            "_mask": 15,
            "_schema_version": 2,
            "gross_weight": 12.0,
            "net_weight": 10.0,
        },
        {
            "_rowid": 2,
            "id": None,
            "name": None,
            "weight": None,
            "_version": 2,
            "_updated": ANY,
            "_mask": 4,
            "_schema_version": 2,
            "gross_weight": None,
            "net_weight": 9.5,
        },
    ]
    # The same _mask value (4) decodes against the column layout that was
    # in effect when each row was recorded
    versions = schema_versions(db, "shipments")
    assert decode_mask(
        rows[1]["_mask"], [name for name, _ in versions[rows[1]["_schema_version"]]]
    ) == ["weight"]
    assert decode_mask(
        rows[3]["_mask"], [name for name, _ in versions[rows[3]["_schema_version"]]]
    ) == ["net_weight"]


def test_legacy_history_table_is_migrated():
    db = sqlite_utils.Database(memory=True)
    db["legacy"].create({"id": int, "name": str}, pk="id")
    # Simulate a database configured before schema tracking existed:
    # no registry table and no _schema_version column
    db.execute(
        "CREATE TABLE _legacy_history (_rowid INTEGER, id INTEGER, name TEXT, "
        "_version INTEGER, _updated INTEGER, _mask INTEGER)"
    )
    db.execute("INSERT INTO _legacy_history VALUES (1, 1, 'Alice', 1, 0, 3)")
    configure_history(db, "legacy")
    # The existing layout is registered as version 1 and old rows are stamped
    assert schema_versions(db, "legacy") == {1: [("id", "INTEGER"), ("name", "TEXT")]}
    assert db.execute(
        "SELECT _schema_version FROM _legacy_history WHERE _rowid = 1"
    ).fetchone() == (1,)
    # New writes carry the same schema version
    db["legacy"].insert({"id": 2, "name": "Bob"})
    assert db.execute(
        "SELECT _schema_version, _mask FROM _legacy_history WHERE _rowid = 2"
    ).fetchone() == (1, 3)


def test_migration_is_atomic(monkeypatch):
    db = sqlite_utils.Database(memory=True)
    db["cargo"].create({"id": int, "weight": float}, pk="id")
    configure_history(db, "cargo")
    db["cargo"].insert({"id": 1, "weight": 1.5})
    db.execute("ALTER TABLE cargo ADD COLUMN net_weight REAL")

    def fail(*args, **kwargs):
        raise RuntimeError("simulated crash mid-migration")

    monkeypatch.setattr(sqlite_history, "_record_schema", fail)
    with pytest.raises(RuntimeError):
        configure_history(db, "cargo")
    # Nothing from the aborted migration was committed, so every accepted
    # row still has exactly one confirmable column interpretation
    assert [
        row[1] for row in db.execute("PRAGMA table_info(_cargo_history)").fetchall()
    ] == ["_rowid", "id", "weight", "_version", "_updated", "_mask", "_schema_version"]
    assert schema_versions(db, "cargo") == {1: [("id", "INTEGER"), ("weight", "REAL")]}
    assert db.execute("SELECT _schema_version FROM _cargo_history").fetchall() == [(1,)]
    # The old triggers still work
    db["cargo"].insert({"id": 2, "weight": 2.0})
    assert db.execute(
        "SELECT _schema_version, _mask FROM _cargo_history WHERE _rowid = 2"
    ).fetchone() == (1, 3)
    # Re-running after the "crash" completes the migration cleanly
    monkeypatch.undo()
    configure_history(db, "cargo")
    assert schema_versions(db, "cargo") == {
        1: [("id", "INTEGER"), ("weight", "REAL")],
        2: [("id", "INTEGER"), ("weight", "REAL"), ("net_weight", "REAL")],
    }
    db["cargo"].insert({"id": 3, "weight": 3.0, "net_weight": 2.5})
    assert db.execute(
        "SELECT _schema_version, _mask FROM _cargo_history WHERE _rowid = 3"
    ).fetchone() == (2, 7)
