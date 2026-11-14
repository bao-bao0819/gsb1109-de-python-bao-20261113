import sqlite3
from datetime import datetime, timezone

import pytest
from sqlite_history.sql import history_table_sql
from sqlite_history.compact import (
    as_of_table,
    compact_history,
    month_start_ms_utc,
    plan_compaction,
    run_compact_cli,
    state_at_history_row,
    verify_compaction,
)


def day_ms(year, month, day):
    return int(
        datetime(year, month, day, tzinfo=timezone.utc).timestamp() * 1000
    )


CUTOFF = day_ms(2026, 11, 1)
PROTECTED = "2025-04-01"


def make_db(columns=(("id", "INTEGER"), ("name", "TEXT"), ("age", "INTEGER"))):
    db = sqlite3.connect(":memory:")
    table = "t"
    db.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, name TEXT, age INTEGER);")
    db.executescript(history_table_sql(table, columns))
    return db


def insert_history(db, rowid, values, version, updated_ms, mask):
    db.execute(
        "INSERT INTO _t_history (_rowid, id, name, age, _version, _updated, _mask)"
        " VALUES (?, ?, ?, ?, ?, ?, ?);",
        (rowid, *values, version, updated_ms, mask),
    )


def history_rows(db):
    return db.execute(
        "SELECT _rowid, id, name, age, _version, _updated, _mask"
        " FROM _t_history ORDER BY rowid;"
    ).fetchall()


def build_sparse_db():
    db = make_db()
    insert_history(db, 1, (1, "Alice", 20), 1, day_ms(2025, 1, 1), 7)
    insert_history(db, 1, (None, "Bob", None), 2, day_ms(2025, 2, 1), 2)
    insert_history(db, 1, (None, None, 30), 3, day_ms(2025, 3, 1), 4)
    insert_history(db, 1, (None, "Carol", None), 4, day_ms(2025, 4, 1), 2)
    insert_history(db, 1, (None, None, 31), 5, day_ms(2025, 5, 1), 4)
    insert_history(db, 1, (None, "Dave", None), 6, day_ms(2025, 6, 1), 2)
    insert_history(db, 1, (None, None, 40), 7, day_ms(2026, 11, 5), 4)
    db.commit()
    return db


def test_plan_does_not_modify_database():
    db = build_sparse_db()
    before = history_rows(db)
    stats = plan_compaction(db, "t", cutoff_ms=CUTOFF, protected_days={PROTECTED})
    assert stats.original_rows == 7
    assert history_rows(db) == before


def test_plan_counts_match_actual_compaction():
    planned = plan_compaction(
        build_sparse_db(), "t", cutoff_ms=CUTOFF, protected_days={PROTECTED}
    )
    db = build_sparse_db()
    actual = compact_history(db, "t", cutoff_ms=CUTOFF, protected_days={PROTECTED})
    assert planned.folded_rows == actual.folded_rows == 4
    assert planned.fold_groups == actual.fold_groups == 2
    assert planned.deleted_rows == actual.deleted_rows == 2
    assert planned.compacted_rows == actual.compacted_rows == 5


def test_folds_sparse_runs_and_keeps_pinned_rows():
    db = build_sparse_db()
    stats = compact_history(db, "t", cutoff_ms=CUTOFF, protected_days={PROTECTED})

    assert stats.compacted_rows == 5
    assert stats.folded_rows == 4
    rows = history_rows(db)

    # Baseline, protected row and post-cutoff row survive byte-for-byte.
    assert (1, 1, "Alice", 20, 1, day_ms(2025, 1, 1), 7) in rows
    assert (1, None, "Carol", None, 4, day_ms(2025, 4, 1), 2) in rows
    assert (1, None, None, 40, 7, day_ms(2026, 11, 5), 4) in rows

    # First run (v2+v3) folded onto v3's version/timestamp.
    assert (1, None, "Bob", 30, 3, day_ms(2025, 3, 1), 6) in rows
    # Second run (v5+v6) folded onto v6's version/timestamp.
    assert (1, None, "Dave", 31, 6, day_ms(2025, 6, 1), 6) in rows

    # Versions present are a subset of the originals (never renumbered).
    assert sorted(row[4] for row in rows) == [1, 3, 4, 6, 7]


def test_readback_matches_at_protected_and_post_cutoff_points():
    original = build_sparse_db()
    compacted = build_sparse_db()
    compact_history(compacted, "t", cutoff_ms=CUTOFF, protected_days={PROTECTED})

    result = verify_compaction(
        original, compacted, "t", cutoff_ms=CUTOFF, protected_days={PROTECTED}
    )
    assert result["ok"], result["failures"]
    assert result["protected_checked"] == 1
    assert result["readback_timestamps_checked"] == 1

    # Snapshot at the protected point: the folded v3 row supplies age=30,
    # the protected row supplies name=Carol.
    snapshot = as_of_table(compacted, "t", day_ms(2025, 4, 1))
    assert snapshot[1] == {
        "id": (True, 1),
        "name": (True, "Carol"),
        "age": (True, 30),
    }
    # Snapshot after the latest update.
    snapshot = as_of_table(compacted, "t", day_ms(2026, 11, 5))
    assert snapshot[1]["name"] == (True, "Dave")
    assert snapshot[1]["age"] == (True, 40)


def test_unknown_column_is_not_backfilled():
    # Two-column table: the baseline insert only sets id; name stays unknown
    # until a later update. A protected point in between must read unknown.
    db = make_db(columns=(("id", "INTEGER"), ("name", "TEXT")))
    db.execute("DROP TABLE _t_history;")
    db.executescript(
        history_table_sql("t", (("id", "INTEGER"), ("name", "TEXT")))
    )
    db.execute(
        "INSERT INTO _t_history (_rowid, id, name, _version, _updated, _mask)"
        " VALUES (1, 1, NULL, 1, ?, 1);",
        (day_ms(2025, 1, 1),),
    )
    db.execute(
        "INSERT INTO _t_history (_rowid, id, name, _version, _updated, _mask)"
        " VALUES (1, NULL, 'Eve', 2, ?, 2);",
        (day_ms(2025, 6, 1),),
    )
    db.execute(
        "INSERT INTO _t_history (_rowid, id, name, _version, _updated, _mask)"
        " VALUES (1, NULL, 'Fay', 3, ?, 2);",
        (day_ms(2025, 7, 1),),
    )
    db.execute(
        "INSERT INTO _t_history (_rowid, id, name, _version, _updated, _mask)"
        " VALUES (1, NULL, 'Gina', 4, ?, 2);",
        (day_ms(2026, 11, 5),),
    )
    db.commit()

    # Before any name update, name must read unknown.
    early = as_of_table(db, "t", day_ms(2025, 3, 1))
    assert early[1] == {"id": (True, 1), "name": (False, None)}

    compact_history(
        db, "t", cutoff_ms=CUTOFF, protected_days={"2025-07-01"}
    )
    early_after = as_of_table(db, "t", day_ms(2025, 3, 1))
    assert early_after[1] == {"id": (True, 1), "name": (False, None)}

    state = state_at_history_row(
        db, "t", rowid=1, version=3, updated_ms=day_ms(2025, 7, 1)
    )
    assert state["name"] == (True, "Fay")
    assert state["id"] == (True, 1)


def test_explicit_null_survives_folding():
    db = make_db()
    insert_history(db, 1, (1, "Alice", 20), 1, day_ms(2025, 1, 1), 7)
    # Explicit NULL for name (bit present, value NULL), then an age-only update.
    insert_history(db, 1, (None, None, None), 2, day_ms(2025, 2, 1), 2)
    insert_history(db, 1, (None, None, 30), 3, day_ms(2025, 3, 1), 4)
    insert_history(db, 1, (None, "Zoe", None), 4, day_ms(2026, 11, 5), 2)
    db.commit()

    compact_history(db, "t", cutoff_ms=CUTOFF)
    folded = db.execute(
        "SELECT name, age, _mask FROM _t_history WHERE _version = 3;"
    ).fetchone()
    # mask=6 means both columns were explicitly touched; name stays NULL.
    assert folded == (None, 30, 6)

    snapshot = as_of_table(db, "t", day_ms(2025, 3, 1))
    assert snapshot[1]["name"] == (True, None)


def test_delete_and_resurrection_keep_lifecycle():
    db = make_db()
    # Generation 1: insert, update, delete.
    insert_history(db, 9, (9, "Alice", 20), 1, day_ms(2025, 1, 1), 7)
    insert_history(db, 9, (None, "Bob", None), 2, day_ms(2025, 2, 1), 2)
    insert_history(db, 9, (9, "Bob", 20), 3, day_ms(2025, 3, 1), -1)
    # Generation 2: same rowid resurrected, versions restart at 1.
    insert_history(db, 9, (9, "Cara", 40), 1, day_ms(2025, 5, 1), 7)
    insert_history(db, 9, (None, "Dan", None), 2, day_ms(2025, 6, 1), 2)
    insert_history(db, 9, (None, "Erin", None), 3, day_ms(2025, 7, 1), 2)
    insert_history(db, 9, (None, None, 41), 4, day_ms(2026, 11, 5), 4)
    db.commit()

    compact_history(
        db, "t", cutoff_ms=CUTOFF, protected_days={"2025-07-01"}
    )
    rows = history_rows(db)

    # Tombstone retained verbatim and folding never crosses it.
    assert any(row[6] == -1 and row[4] == 3 for row in rows)
    # Both generation baselines retained.
    assert sum(1 for row in rows if row[4] == 1 and row[6] == 7) == 2

    # Deleted window reads as no live row.
    assert as_of_table(db, "t", day_ms(2025, 4, 1)) == {}
    # Resurrected generation reads its own baseline.
    revived = as_of_table(db, "t", day_ms(2025, 5, 2))
    assert revived[9] == {
        "id": (True, 9),
        "name": (True, "Cara"),
        "age": (True, 40),
    }
    latest = as_of_table(db, "t", day_ms(2026, 11, 5))
    assert latest[9]["name"] == (True, "Erin")
    assert latest[9]["age"] == (True, 41)


def test_cli_end_to_end_with_backup_and_verify(tmp_path):
    db_path = tmp_path / "events.db"
    db = sqlite3.connect(db_path)
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT, age INTEGER);")
    db.executescript(
        history_table_sql("t", (("id", "INTEGER"), ("name", "TEXT"), ("age", "INTEGER")))
    )
    insert_history(db, 1, (1, "Alice", 20), 1, day_ms(2025, 1, 1), 7)
    insert_history(db, 1, (None, "Bob", None), 2, day_ms(2025, 2, 1), 2)
    insert_history(db, 1, (None, None, 30), 3, day_ms(2025, 3, 1), 4)
    insert_history(db, 1, (None, "Carol", None), 4, day_ms(2025, 4, 1), 2)
    insert_history(db, 1, (None, None, 40), 5, day_ms(2026, 11, 5), 4)
    db.commit()
    db.close()

    exit_code = run_compact_cli(
        [
            str(db_path),
            "t",
            "--cutoff",
            "2026-11-01",
            "--protect",
            PROTECTED,
            "--backup",
            "--verify",
        ]
    )
    assert exit_code == 0
    assert (tmp_path / "events.pre-compact.db").exists()

    compacted = sqlite3.connect(db_path)
    count = compacted.execute("SELECT COUNT(*) FROM _t_history;").fetchone()[0]
    assert count == 4
    compacted.close()


def test_cli_dry_run_keeps_database_intact(tmp_path):
    db_path = tmp_path / "events.db"
    db = sqlite3.connect(db_path)
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT, age INTEGER);")
    db.executescript(
        history_table_sql("t", (("id", "INTEGER"), ("name", "TEXT"), ("age", "INTEGER")))
    )
    insert_history(db, 1, (1, "Alice", 20), 1, day_ms(2025, 1, 1), 7)
    insert_history(db, 1, (None, "Bob", None), 2, day_ms(2025, 2, 1), 2)
    db.commit()
    db.close()

    exit_code = run_compact_cli(
        [str(db_path), "t", "--cutoff", "2026-11-01", "--dry-run"]
    )
    assert exit_code == 0

    check = sqlite3.connect(db_path)
    assert check.execute("SELECT COUNT(*) FROM _t_history;").fetchone()[0] == 2
    check.close()


def test_default_cutoff_is_first_of_current_month_utc():
    cutoff = month_start_ms_utc()
    parsed = datetime.fromtimestamp(cutoff / 1000, tz=timezone.utc)
    assert parsed.day == 1
    assert parsed.hour == parsed.minute == parsed.second == 0
