"""Audit of sqlite-history triggers against SQLite storage-class changes.

The metering device may hand an untyped (BLOB-affinity) column an integer 1,
a real 1.0, text '1' or a BLOB.  The statistics side distinguishes the sources
with typeof().  These tests write every change through the real triggers, read
the history table, then replay the history into a fresh table and compare the
materialised storage class (typeof) and value (quote) after every commit.

A loss is only counted when the *source* table changes storage class.  When a
column's affinity normalises 1 and 1.0 to the same class before the trigger
runs, there is no information for the history writer to lose.
"""

import sqlite3

import pytest

from sqlite_history import configure_history
from sqlite_history.sql import (
    history_table_sql,
    table_columns_and_types,
    triggers_sql,
)

# (python value, SQL literal that preserves its storage class)
INT = (1, "1")
REAL = (1.0, "1.0")
TEXT = ("1", "'1'")
BLOB = (b"1", "X'31'")


def build_db(column_def="v", use_configure=False):
    """Create a tracked table; column_def has no type name => BLOB affinity."""
    db = sqlite3.connect(":memory:")
    db.execute(f"CREATE TABLE t (id INTEGER PRIMARY KEY, {column_def})")
    if use_configure:
        configure_history(db, "t")
        return db
    columns = table_columns_and_types(db, "t")
    db.executescript(history_table_sql("t", columns))
    db.executescript(triggers_sql("t", [name for name, _ in columns]))
    return db


def source_state(db):
    """(typeof, quote) of the live source row, or None when absent."""
    row = db.execute("SELECT typeof(v), quote(v) FROM t WHERE id = 1").fetchone()
    return row


def history_rows(db):
    return db.execute(
        "SELECT _rowid, _version, _mask, typeof(v), quote(v) "
        "FROM _t_history ORDER BY _rowid, _version"
    ).fetchall()


def replay(db):
    """Replay history records into a fresh table, snapshotting after each one.

    Values are fed back via their quote() literal so the storage class that
    landed in the history table is preserved exactly.
    """
    db.execute("CREATE TABLE replay (id INTEGER PRIMARY KEY, v)")
    snapshots = []
    for rowid, _version, mask, _typ, quoted in history_rows(db):
        if mask == -1:
            db.execute(f"DELETE FROM replay WHERE id = {rowid}")
        elif not db.execute(
            "SELECT 1 FROM replay WHERE id = ?", (rowid,)
        ).fetchone():
            db.execute(f"INSERT INTO replay (id, v) VALUES ({rowid}, {quoted})")
        elif mask & 2:  # bit for column v
            db.execute(f"UPDATE replay SET v = {quoted} WHERE id = {rowid}")
        row = db.execute(
            "SELECT typeof(v), quote(v) FROM replay WHERE id = ?", (rowid,)
        ).fetchone()
        snapshots.append(row)
    return snapshots


def test_history_schema_keeps_every_storage_class():
    # The history column mirrors the source: BLOB affinity, so the schema
    # itself can hold integer/real/text/blob side by side. The trigger gate,
    # not the table definition, is the loss point.
    db = build_db()
    for literal in ("1", "1.0", "'1'", "X'31'"):
        db.execute(f"INSERT INTO _t_history (_rowid, v, _version, _mask) "
                   f"VALUES (99, {literal}, 1, 2)")
    classes = [
        r[0]
        for r in db.execute(
            "SELECT typeof(v) FROM _t_history WHERE _rowid = 99 ORDER BY _version"
        )
    ]
    assert classes == ["integer", "real", "text", "blob"]


def test_distinct_classes_are_recorded_and_replay_roundtrips():
    db = build_db()
    db.execute("INSERT INTO t (id, v) VALUES (1, 1)")
    expected = [source_state(db)]
    for _, literal in (TEXT, BLOB, INT):
        db.execute(f"UPDATE t SET v = {literal} WHERE id = 1")
        expected.append(source_state(db))

    assert [r[3:5] for r in history_rows(db)] == expected
    assert replay(db) == expected


def test_integer_to_real_equal_value_changes_class_but_drops_history():
    db = build_db()
    db.execute("INSERT INTO t (id, v) VALUES (1, 1)")
    after_insert = source_state(db)
    assert after_insert == ("integer", "1")

    db.execute("UPDATE t SET v = 1.0 WHERE id = 1")
    after_update = source_state(db)

    # The source really did switch storage class while staying numerically equal.
    assert after_update == ("real", "1.0")
    assert db.execute("SELECT 1 = 1.0").fetchone()[0] == 1
    assert db.execute("SELECT typeof(1) = typeof(1.0)").fetchone()[0] == 0

    # The update trigger's "old.v != new.v" gate evaluated false, so no
    # history row and no new version were produced for this commit.
    rows = history_rows(db)
    assert len(rows) == 1
    assert rows[0][1:5] == (1, 3, "integer", "1")

    # Replay therefore reconstructs the old integer, not the live real.
    replayed = replay(db)
    assert replayed == [("integer", "1")]
    assert replayed[-1] != after_update


def test_real_to_integer_equally_dropped():
    db = build_db()
    db.execute("INSERT INTO t (id, v) VALUES (1, 1.0)")
    db.execute("UPDATE t SET v = 1 WHERE id = 1")
    assert source_state(db) == ("integer", "1")
    assert len(history_rows(db)) == 1
    assert history_rows(db)[0][3:5] == ("real", "1.0")


def test_upsert_on_conflict_takes_same_silent_path():
    db = build_db()
    db.execute("INSERT INTO t (id, v) VALUES (1, 1)")
    db.execute(
        "INSERT INTO t (id, v) VALUES (1, 1.0) "
        "ON CONFLICT(id) DO UPDATE SET v = excluded.v"
    )
    assert source_state(db) == ("real", "1.0")
    assert [r[1] for r in history_rows(db)] == [1]


def test_configure_history_end_to_end_reproduces_the_loss():
    # Same behaviour through the public API, including backfill.
    db = build_db(use_configure=True)
    db.execute("INSERT INTO t (id, v) VALUES (1, 1)")
    db.execute("UPDATE t SET v = 1.0 WHERE id = 1")
    assert source_state(db) == ("real", "1.0")
    assert all(r[3] != "real" for r in history_rows(db))


def test_null_transitions_are_silently_dropped_too():
    # Same != gate: any comparison against NULL is NULL, so neither direction
    # of the value <-> NULL change is recorded (documented adjacent defect).
    db = build_db()
    db.execute("INSERT INTO t (id, v) VALUES (1, 1)")
    db.execute("UPDATE t SET v = NULL WHERE id = 1")
    assert source_state(db) == ("null", "NULL")
    db.execute("UPDATE t SET v = 1 WHERE id = 1")
    assert source_state(db) == ("integer", "1")
    assert len(history_rows(db)) == 1


def test_delete_trigger_captures_storage_class_without_gate():
    db = build_db()
    db.execute("INSERT INTO t (id, v) VALUES (1, X'31')")
    db.execute("DELETE FROM t WHERE id = 1")
    rows = history_rows(db)
    assert rows[-1][2:] == (-1, "blob", "X'31'")


@pytest.mark.parametrize(
    "column_def,expected_class",
    [
        ("v INTEGER", "integer"),
        ("v REAL", "real"),
        ("v NUMERIC", "integer"),
        ("v TEXT", "text"),
    ],
)
def test_affinity_normalised_class_switch_is_not_history_loss(column_def, expected_class):
    db = build_db(column_def)
    db.execute("INSERT INTO t (id, v) VALUES (1, 1)")
    db.execute("UPDATE t SET v = 1.0 WHERE id = 1")

    # The source column holds the same class before and after the update:
    # affinity erased the integer/real distinction, so nothing was lost.
    assert source_state(db)[0] == expected_class
    classes_seen = {r[3] for r in history_rows(db) if r[3] is not None}
    assert classes_seen == {expected_class}


def test_shortest_raw_write_sequence_is_two_statements():
    # Minimal repro of "numerically equal but storage class changed, history
    # blind to it". A pre-existing integer row is required, hence two writes.
    sequence = [
        "INSERT INTO t (id, v) VALUES (1, 1)",
        "UPDATE t SET v = 1.0 WHERE id = 1",
    ]
    db = build_db()
    db.execute(sequence[0])
    db.execute(sequence[1])

    assert source_state(db) == ("real", "1.0")
    assert len(history_rows(db)) == 1
    with pytest.raises(AssertionError):
        # The single-row history cannot reproduce the live source class.
        assert replay(db)[-1] == source_state(db)
