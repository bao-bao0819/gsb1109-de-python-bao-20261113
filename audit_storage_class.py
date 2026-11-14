#!/usr/bin/env python3
"""Executable storage-class audit for the sqlite-history triggers.

For every write it:
  1. applies the change to the tracked source table,
  2. reads what the insert/update/delete triggers actually stored,
  3. replays history rows (by their quote() literal, preserving class) into a
     fresh table,
  4. compares typeof()/quote() of source vs replay after each commit.

A LOSS line means SQLite judged the values numerically equal (or NULL was
involved), the update trigger's "old.v != new.v" gate fired false, and the
storage-class change never reached the history table.  Columns whose affinity
already collapses the classes are reported separately and are not losses.
"""

import sqlite3
import sys

from sqlite_history.sql import (
    history_table_sql,
    table_columns_and_types,
    triggers_sql,
)


def tracked_db(column_def="v"):
    db = sqlite3.connect(":memory:")
    db.execute(f"CREATE TABLE t (id INTEGER PRIMARY KEY, {column_def})")
    columns = table_columns_and_types(db, "t")
    db.executescript(history_table_sql("t", columns))
    db.executescript(triggers_sql("t", [name for name, _ in columns]))
    db.execute("CREATE TABLE replay (id INTEGER PRIMARY KEY, v)")
    return db


def snapshot(db, table):
    row = db.execute(
        f"SELECT typeof(v), quote(v) FROM {table} WHERE id = 1"
    ).fetchone()
    return f"{row[0]}:{row[1]}" if row else "<deleted>"


def new_history_rows(db, seen_versions):
    rows = db.execute(
        "SELECT _version, _mask, typeof(v), quote(v) FROM _t_history "
        "WHERE _rowid = 1 AND _version > ? ORDER BY _version",
        (seen_versions,),
    ).fetchall()
    for version, mask, typ, quoted in rows:
        seen_versions = version
        if mask == -1:
            db.execute("DELETE FROM replay WHERE id = 1")
        elif not db.execute("SELECT 1 FROM replay WHERE id = 1").fetchone():
            db.execute(f"INSERT INTO replay (id, v) VALUES (1, {quoted})")
        elif mask & 2:  # bit for column v (id is bit 1)
            db.execute(f"UPDATE replay SET v = {quoted} WHERE id = 1")
        yield version, mask, f"{typ}:{quoted}"


def run_walk(column_def, writes):
    db = tracked_db(column_def)
    seen_versions = 0
    losses = 0
    print(f"\n== source column: {column_def} ==")
    print(f"{'write':45} {'source':14} {'history row':22} {'replay':14} verdict")
    for write in writes:
        db.execute(write)
        emitted = list(new_history_rows(db, seen_versions))
        if emitted:
            seen_versions = emitted[-1][0]
            history_text = "; ".join(f"v{v} m{m} {state}" for v, m, state in emitted)
        else:
            history_text = "(none)"
        source, replay = snapshot(db, "t"), snapshot(db, "replay")
        verdict = "ok" if source == replay else "LOSS"
        losses += verdict != "ok"
        print(f"{write:45} {source:14} {history_text:22} {replay:14} {verdict}")
    return losses


def main():
    print("sqlite", sqlite3.sqlite_version)

    blob_writes = [
        "INSERT INTO t (id, v) VALUES (1, 1)",
        "UPDATE t SET v = 1.0 WHERE id = 1",
        "UPDATE t SET v = '1' WHERE id = 1",
        "UPDATE t SET v = X'31' WHERE id = 1",
        "UPDATE t SET v = 1 WHERE id = 1",
        "UPDATE t SET v = NULL WHERE id = 1",
        "UPDATE t SET v = 1 WHERE id = 1",
        "DELETE FROM t WHERE id = 1",
    ]
    losses = run_walk("v", blob_writes)  # no type name => BLOB affinity

    print("\n== affinity check: same 1 -> 1.0 write on typed columns ==")
    for column_def in ("v INTEGER", "v REAL", "v NUMERIC", "v TEXT"):
        db = tracked_db(column_def)
        db.execute("INSERT INTO t (id, v) VALUES (1, 1)")
        before = snapshot(db, "t")
        db.execute("UPDATE t SET v = 1.0 WHERE id = 1")
        after = snapshot(db, "t")
        rows = db.execute("SELECT COUNT(*) FROM _t_history").fetchone()[0]
        class_changed = before.split(":", 1)[0] != after.split(":", 1)[0]
        print(
            f"{column_def:10} before={before:10} after={after:10} "
            f"history_rows={rows} "
            f"{'CLASS CHANGED' if class_changed else 'affinity-normalised (not a loss)'}"
        )

    print("\n== shortest raw write sequence that loses a class change ==")
    print("  INSERT INTO t (id, v) VALUES (1, 1);")
    print("  UPDATE t SET v = 1.0 WHERE id = 1;")
    print("  -> source typeof(v)=real, history keeps only the version-1 integer")

    print(f"\nloss commits on BLOB-affinity column: {losses}")
    return 1 if losses else 0


if __name__ == "__main__":
    sys.exit(main())
