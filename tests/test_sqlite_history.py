import pytest
import sqlite_utils
from sqlite_history import UNKNOWN, row_state
from sqlite_history.sql import history_table_sql, triggers_sql
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


def configured_db():
    db = sqlite_utils.Database(memory=True)
    db["test"].create({"id": int, "name": str}, pk="id")
    db.executescript(history_table_sql("test", (("id", "integer"), ("name", "text"))))
    db.executescript(triggers_sql("test", ["id", "name"]))
    return db


def test_trigger_records_clear_to_null():
    db = configured_db()
    db["test"].insert({"id": 1, "name": "Alice"})
    db["test"].update(1, {"name": None})
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
            "_rowid": 1,
            "id": None,
            "name": None,
            "_version": 2,
            "_updated": ANY,
            "_mask": 2,
        },
    ]


def test_trigger_records_null_to_value():
    db = configured_db()
    db["test"].insert({"id": 1, "name": None})
    db["test"].update(1, {"name": "Alice"})
    assert list(db["_test_history"].rows)[-1] == {
        "_rowid": 1,
        "id": None,
        "name": "Alice",
        "_version": 2,
        "_updated": ANY,
        "_mask": 2,
    }


def test_trigger_no_change_no_record():
    db = configured_db()
    db["test"].insert({"id": 1, "name": None})
    # NULL -> NULL and same-value updates record nothing
    db["test"].update(1, {"name": None})
    db["test"].update(1, {"id": 1})
    assert [r["_version"] for r in db["_test_history"].rows] == [1]


def test_consecutive_clears_recorded_once():
    db = configured_db()
    db["test"].insert({"id": 1, "name": "Alice"})
    db["test"].update(1, {"name": None})
    db["test"].update(1, {"name": None})
    assert [r["_version"] for r in db["_test_history"].rows] == [1, 2]
    state = row_state(db, "test", 1)
    assert state["changed"] == {"name": None}
    assert state["row"] == {"id": 1, "name": None}


def test_delete_reinsert_same_rowid_starts_new_lifecycle():
    db = configured_db()
    db["test"].insert({"id": 1, "name": "Alice"})
    db["test"].update(1, {"name": "Alicia"})
    db["test"].delete(1)
    db["test"].insert({"id": 1, "name": "Bob"})
    db["test"].update(1, {"name": "Bobby"})
    assert [(r["_version"], r["_mask"]) for r in db["_test_history"].rows] == [
        (1, 3),
        (2, 2),
        (3, -1),
        (1, 3),
        (2, 2),
    ]
    # Tracing back from the new row must not reach the previous customer
    state = row_state(db, "test", 1)
    assert state["row"] == {"id": 1, "name": "Bobby"}
    assert row_state(db, "test", 1, version=1)["row"] == {"id": 1, "name": "Bob"}
    with pytest.raises(ValueError):
        row_state(db, "test", 1, version=3)


def test_row_state_distinguishes_untouched_cleared_unknown():
    db = configured_db()
    db["test"].insert({"id": 1, "name": "Alice"})
    db["test"].update(1, {"name": None})
    # Column added after history was configured: never recorded
    db.execute("ALTER TABLE test ADD COLUMN address TEXT")
    db["test"].update(1, {"address": "Main Street"})
    state = row_state(db, "test", 1)
    assert state["changed"] == {"name": None}
    assert state["row"] == {"id": 1, "name": None, "address": UNKNOWN}
    assert state["row"]["address"] is not None
    # At version 1 name was still set and untouched by that version
    v1 = row_state(db, "test", 1, version=1)
    assert v1["changed"] == {"id": 1, "name": "Alice"}
    assert v1["row"]["name"] == "Alice"


def test_row_state_deleted_row():
    db = configured_db()
    db["test"].insert({"id": 1, "name": "Alice"})
    db["test"].delete(1)
    assert row_state(db, "test", 1) is None
    with pytest.raises(ValueError):
        row_state(db, "test", 1, version=1)
    with pytest.raises(ValueError):
        row_state(db, "test", 99)


def test_row_state_accepts_old_format_history():
    # Simulate a history table populated by the old triggers, where
    # clearing a column to null was recorded as "not changed" (mask bit
    # 0, null value) or dropped entirely. Readback must trust the mask
    # and not fabricate the lost clears.
    db = sqlite_utils.Database(memory=True)
    db["test"].create({"id": int, "name": str}, pk="id")
    db.executescript(history_table_sql("test", (("id", "integer"), ("name", "text"))))
    db["_test_history"].insert(
        {"_rowid": 1, "id": 1, "name": "Alice", "_version": 1, "_updated": 1, "_mask": 3}
    )
    # Old trigger row: id changed, name was actually cleared but the
    # evidence was lost - recorded as bit 0 with a null value
    db["_test_history"].insert(
        {"_rowid": 1, "id": 2, "name": None, "_version": 2, "_updated": 2, "_mask": 1}
    )
    state = row_state(db, "test", 1)
    assert state["changed"] == {"id": 2}
    # name reads back as the last value the mask vouches for
    assert state["row"] == {"id": 2, "name": "Alice"}
