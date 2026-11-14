"""Compaction (archival) for sqlite-history tables.

History semantics (see README):

* Each history row carries a bit mask ``_mask`` of the columns that actually
  changed. A column whose bit is absent is *unknown* in that row, even when the
  stored value happens to be NULL.
* ``_mask = -1`` is a tombstone: the tracked row was deleted.
* An insert reuses a ``_rowid`` after a delete (a resurrection), so the same
  ``_rowid`` can have several "generations" separated by tombstones; versions
  restart at 1 on insert.

Compaction rules:

* Rows on a protected day, rows at or after the cutoff, the first row of every
  generation and every tombstone are kept byte-for-byte.
* Any other old row belongs to a run between two kept rows. Each run is folded
  into one sparse row: the mask is the union of the run's masks and every
  column takes the last value explicitly set inside the run. Columns that were
  never explicitly set stay unknown (the later updates must not backfill them).
* ``_version`` and ``_updated`` are never rewritten: folded rows inherit the
  version/timestamp of the last real row they replace, so every retained
  version still answers exactly as before compaction.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

META_COLUMNS = ("_rowid", "_version", "_updated", "_mask")
TOMBSTONE_MASK = -1


def month_start_ms_utc(now: datetime | None = None) -> int:
    """Millisecond timestamp of the first day of the current month (UTC)."""
    now = now or datetime.now(timezone.utc)
    first = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return int(first.timestamp() * 1000)


def date_to_cutoff_ms(day: str) -> int:
    """Convert ``YYYY-MM-DD`` to a millisecond cutoff at 00:00 UTC."""
    parsed = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def _day_key(updated_ms: int) -> str:
    return datetime.fromtimestamp(updated_ms / 1000, tz=timezone.utc).date().isoformat()


def history_data_columns(db: sqlite3.Connection, history_table: str) -> list[str]:
    """Tracked data columns of a history table, in declared order."""
    cursor = db.execute(f"PRAGMA table_info([{history_table}]);")
    return [row[1] for row in cursor.fetchall() if row[1] not in META_COLUMNS]


def _load_rows(db: sqlite3.Connection, history_table: str, columns: list[str]):
    select_cols = ", ".join(["rowid", "_rowid", *columns, "_version", "_updated", "_mask"])
    cursor = db.execute(
        f"SELECT {select_cols} FROM [{history_table}] ORDER BY rowid;"
    )
    names = ["seq", "_rowid", *columns, "_version", "_updated", "_mask"]
    return [dict(zip(names, values)) for values in cursor.fetchall()]


def _split_generations(rows: list[dict]) -> list[list[dict]]:
    """Split one rowid's history into generations, tombstone ending a generation."""
    generations: list[list[dict]] = []
    current: list[dict] = []
    for row in rows:
        current.append(row)
        if row["_mask"] == TOMBSTONE_MASK:
            generations.append(current)
            current = []
    if current:
        generations.append(current)
    return generations


def _fold_run(run: list[dict], columns: list[str]) -> dict | None:
    """Fold a run of deletable rows into one sparse delta.

    Only columns whose mask bit appears somewhere in the run are emitted, so
    columns that remain unknown are never backfilled from later values.
    """
    if not run:
        return None
    last = run[-1]
    folded = {
        "seq": None,
        "_rowid": last["_rowid"],
        "_version": last["_version"],
        "_updated": last["_updated"],
        "_mask": 0,
    }
    for column in columns:
        folded[column] = None
    for index, column in enumerate(columns):
        bit = 1 << index
        explicit_value = None
        seen = False
        for row in run:
            if (row["_mask"] & bit) == bit:
                seen = True
                explicit_value = row[column]
        if seen:
            folded["_mask"] += bit
            folded[column] = explicit_value
    return folded


def _is_kept(row: dict, index_in_generation: int, cutoff_ms: int, protected_days: set[str]):
    if index_in_generation == 0:
        return True
    if row["_mask"] == TOMBSTONE_MASK:
        return True
    if row["_updated"] >= cutoff_ms:
        return True
    if _day_key(row["_updated"]) in protected_days:
        return True
    return False


@dataclass
class CompactionStats:
    table: str
    original_rows: int = 0
    compacted_rows: int = 0
    deleted_rows: int = 0
    folded_rows: int = 0
    fold_groups: int = 0
    protected_rows: int = 0
    cutoff_rows: int = 0
    generations: int = 0
    rowids: int = 0
    folded_examples: list[dict] = field(default_factory=list)

    @property
    def reduction_percent(self) -> float:
        if self.original_rows == 0:
            return 0.0
        return 100.0 * (self.original_rows - self.compacted_rows) / self.original_rows


def _count_runs(generation: list[dict], cutoff_ms: int, protected_days: set[str]) -> tuple[int, int]:
    """Return ``(folded_rows, fold_groups)`` for one generation."""
    folded_rows = 0
    fold_groups = 0
    in_run = False
    for index, row in enumerate(generation):
        deletable = not _is_kept(row, index, cutoff_ms, protected_days)
        if deletable:
            folded_rows += 1
            if not in_run:
                fold_groups += 1
                in_run = True
        else:
            in_run = False
    return folded_rows, fold_groups


def plan_compaction(
    db: sqlite3.Connection,
    table: str,
    *,
    cutoff_ms: int | None = None,
    protected_days: list[str] | set[str] | None = None,
) -> CompactionStats:
    """Return the compaction plan/stats without modifying the database."""
    cutoff_ms = month_start_ms_utc() if cutoff_ms is None else cutoff_ms
    protected_days = set(protected_days or ())
    history_table = f"_{table}_history"
    columns = history_data_columns(db, history_table)
    rows = _load_rows(db, history_table, columns)

    stats = CompactionStats(table=table, original_rows=len(rows))
    by_rowid: dict[int, list[dict]] = {}
    for row in rows:
        by_rowid.setdefault(row["_rowid"], []).append(row)
    stats.rowids = len(by_rowid)

    for rowid_rows in by_rowid.values():
        generations = _split_generations(rowid_rows)
        stats.generations += len(generations)
        for generation in generations:
            for index, row in enumerate(generation):
                if row["_mask"] == TOMBSTONE_MASK or index == 0:
                    continue
                if _day_key(row["_updated"]) in protected_days:
                    stats.protected_rows += 1
                if row["_updated"] >= cutoff_ms:
                    stats.cutoff_rows += 1
            folded_rows, fold_groups = _count_runs(
                generation, cutoff_ms, protected_days
            )
            stats.folded_rows += folded_rows
            stats.fold_groups += fold_groups
    # Each run of N deletable rows is replaced by one folded row, so the net
    # removal is folded_rows minus the number of fold groups.
    stats.deleted_rows = stats.folded_rows - stats.fold_groups
    stats.compacted_rows = stats.original_rows - stats.deleted_rows
    return stats


def compact_history(
    db: sqlite3.Connection,
    table: str,
    *,
    cutoff_ms: int | None = None,
    protected_days: list[str] | set[str] | None = None,
) -> CompactionStats:
    """Compact one ``_<table>_history`` table inside a transaction.

    Protected rows, post-cutoff rows, generation baselines and tombstones are
    preserved verbatim; everything older is folded into sparse deltas.
    """
    cutoff_ms = month_start_ms_utc() if cutoff_ms is None else cutoff_ms
    protected_days = set(protected_days or ())
    history_table = f"_{table}_history"
    columns = history_data_columns(db, history_table)
    rows = _load_rows(db, history_table, columns)

    by_rowid: dict[int, list[dict]] = {}
    for row in rows:
        by_rowid.setdefault(row["_rowid"], []).append(row)

    output_rows: list[dict] = []
    stats = CompactionStats(table=table, original_rows=len(rows))
    stats.rowids = len(by_rowid)

    for rowid_rows in by_rowid.values():
        generations = _split_generations(rowid_rows)
        stats.generations += len(generations)
        for generation in generations:
            index = 0
            while index < len(generation):
                row = generation[index]
                if _is_kept(row, index, cutoff_ms, protected_days):
                    if index > 0 and row["_mask"] != TOMBSTONE_MASK:
                        if _day_key(row["_updated"]) in protected_days:
                            stats.protected_rows += 1
                        if row["_updated"] >= cutoff_ms:
                            stats.cutoff_rows += 1
                    output_rows.append(row)
                    index += 1
                    continue
                run: list[dict] = []
                while index < len(generation) and not _is_kept(
                    generation[index], index, cutoff_ms, protected_days
                ):
                    run.append(generation[index])
                    index += 1
                folded = _fold_run(run, columns)
                stats.folded_rows += len(run)
                stats.fold_groups += 1
                if folded is not None:
                    output_rows.append(folded)
                    if len(stats.folded_examples) < 10:
                        stats.folded_examples.append(
                            {
                                "_rowid": folded["_rowid"],
                                "_version": folded["_version"],
                                "_updated": folded["_updated"],
                                "_mask": folded["_mask"],
                                "folded_versions": [item["_version"] for item in run],
                            }
                        )

    stats.deleted_rows = stats.folded_rows - stats.fold_groups
    stats.compacted_rows = len(output_rows)
    _rewrite_history_table(db, history_table, columns, output_rows)
    return stats


def _rewrite_history_table(
    db: sqlite3.Connection,
    history_table: str,
    columns: list[str],
    output_rows: list[dict],
) -> None:
    table_sql = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?;",
        (history_table,),
    ).fetchone()[0]
    index_sqls = [
        row[0]
        for row in db.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL;",
            (history_table,),
        ).fetchall()
    ]
    temp_table = f"{history_table}__compact_tmp"
    temp_sql = table_sql.replace(history_table, temp_table)
    insert_cols = ", ".join(["_rowid", *columns, "_version", "_updated", "_mask"])
    placeholders = ", ".join("?" for _ in range(len(columns) + 4))
    with db:
        db.execute(f"DROP TABLE IF EXISTS [{temp_table}];")
        db.execute(temp_sql)
        db.executemany(
            f"INSERT INTO [{temp_table}] ({insert_cols}) VALUES ({placeholders});",
            [
                tuple(row[column] for column in ["_rowid", *columns, "_version", "_updated", "_mask"])
                for row in output_rows
            ],
        )
        db.execute(f"DROP TABLE [{history_table}];")
        db.execute(f"ALTER TABLE [{temp_table}] RENAME TO [{history_table}];")
        for index_sql in index_sqls:
            db.execute(index_sql)


def materialize_state(rows: list[dict], columns: list[str]) -> dict[str, tuple[bool, object]]:
    """Walk history rows and return ``column -> (known, value)`` at the end.

    ``known=False`` means the column was never explicitly set in this
    generation (e.g. added by a later ALTER TABLE); its value must stay NULL
    and must not be inferred from later updates.
    """
    state = {column: (False, None) for column in columns}
    for row in rows:
        if row["_mask"] == TOMBSTONE_MASK:
            continue
        for index, column in enumerate(columns):
            bit = 1 << index
            if (row["_mask"] & bit) == bit:
                state[column] = (True, row[column])
    return state


def as_of_table(
    db: sqlite3.Connection,
    table: str,
    timestamp_ms: int,
) -> dict[int, dict[str, tuple[bool, object]]]:
    """Reconstruct every live row as it was at ``timestamp_ms``.

    Tombstones remove a rowid until a later generation resurrects it.
    """
    history_table = f"_{table}_history"
    columns = history_data_columns(db, history_table)
    rows = _load_rows(db, history_table, columns)

    by_rowid: dict[int, list[dict]] = {}
    for row in rows:
        by_rowid.setdefault(row["_rowid"], []).append(row)

    snapshot: dict[int, dict[str, tuple[bool, object]]] = {}
    for rowid, rowid_rows in by_rowid.items():
        visible: list[dict] = []
        alive = False
        for row in rowid_rows:
            if row["_updated"] > timestamp_ms:
                break
            if row["_mask"] == TOMBSTONE_MASK:
                alive = False
                visible = []
                continue
            alive = True
            visible.append(row)
        if alive and visible:
            snapshot[rowid] = materialize_state(visible, columns)
    return snapshot


def state_at_history_row(
    db: sqlite3.Connection,
    table: str,
    rowid: int,
    version: int,
    updated_ms: int,
) -> dict[str, tuple[bool, object]]:
    """Materialized column state when the matching history row was current.

    The target is located by business key ``(rowid, version, updated)`` rather
    than physical rowid, so it still resolves after the table has been rewritten
    by compaction. Tombstones reset the running generation.
    """
    history_table = f"_{table}_history"
    columns = history_data_columns(db, history_table)
    rows = _load_rows(db, history_table, columns)
    generation: list[dict] = []
    for row in rows:
        if row["_rowid"] != rowid:
            continue
        if row["_mask"] == TOMBSTONE_MASK:
            generation = []
            if row["_version"] == version and row["_updated"] == updated_ms:
                break
            continue
        generation.append(row)
        if row["_version"] == version and row["_updated"] == updated_ms:
            break
    return materialize_state(generation, columns)


def verify_compaction(
    original: sqlite3.Connection,
    compacted: sqlite3.Connection,
    table: str,
    *,
    cutoff_ms: int,
    protected_days: list[str] | set[str] | None = None,
) -> dict:
    """Assert the compacted table answers identically at every required point.

    Checks performed:

    * Every protected row exists byte-for-byte and its materialized columns
      (including unknown-vs-NULL distinctions) match the original.
    * Every original row at/after the cutoff exists byte-for-byte and whole-table
      as-of reads match for each of its timestamps (arbitrary version readback).
    * Tombstones and resurrected rowids keep the same lifecycle.
    """
    protected_days = set(protected_days or ())
    history_table = f"_{table}_history"
    columns = history_data_columns(original, history_table)
    old_rows = _load_rows(original, history_table, columns)
    new_rows = _load_rows(compacted, history_table, columns)

    def row_tuple(row: dict):
        return tuple(row[column] for column in ["_rowid", *columns, "_version", "_updated", "_mask"])

    new_tuples = {row_tuple(row) for row in new_rows}
    failures: list[str] = []

    protected = [
        row for row in old_rows if _day_key(row["_updated"]) in protected_days
    ]
    post_cutoff = [row for row in old_rows if row["_updated"] >= cutoff_ms]

    for label, pinned in (("protected", protected), ("post-cutoff", post_cutoff)):
        for row in pinned:
            if row_tuple(row) not in new_tuples:
                failures.append(
                    f"{label} row missing or altered: rowid={row['_rowid']} "
                    f"version={row['_version']} updated={row['_updated']} mask={row['_mask']}"
                )

    for row in protected:
        before = state_at_history_row(
            original, table, row["_rowid"], row["_version"], row["_updated"]
        )
        after = state_at_history_row(
            compacted, table, row["_rowid"], row["_version"], row["_updated"]
        )
        if before != after:
            failures.append(
                f"state mismatch at protected point rowid={row['_rowid']} "
                f"version={row['_version']}: {before} != {after}"
            )

    checked_timestamps = sorted({row["_updated"] for row in post_cutoff})
    for timestamp_ms in checked_timestamps:
        before = as_of_table(original, table, timestamp_ms)
        after = as_of_table(compacted, table, timestamp_ms)
        if before != after:
            failures.append(
                f"as-of table mismatch at {timestamp_ms}: {before} != {after}"
            )

    old_tombstones = {(row["_rowid"], row["_version"], row["_updated"])
                      for row in old_rows if row["_mask"] == TOMBSTONE_MASK}
    new_tombstones = {(row["_rowid"], row["_version"], row["_updated"])
                      for row in new_rows if row["_mask"] == TOMBSTONE_MASK}
    if old_tombstones != new_tombstones:
        failures.append(
            f"tombstone lifecycle changed: {old_tombstones} != {new_tombstones}"
        )

    return {
        "table": table,
        "ok": not failures,
        "failures": failures,
        "protected_checked": len(protected),
        "readback_timestamps_checked": len(checked_timestamps),
        "tombstones_checked": len(old_tombstones),
        "original_rows": len(old_rows),
        "compacted_rows": len(new_rows),
    }


def _format_stats(stats: CompactionStats) -> str:
    return (
        f"  rows: {stats.original_rows} -> {stats.compacted_rows} "
        f"(-{stats.deleted_rows}, {stats.reduction_percent:.1f}% smaller)\n"
        f"  rowids/generations: {stats.rowids}/{stats.generations}\n"
        f"  protected rows kept: {stats.protected_rows}\n"
        f"  current-month rows kept: {stats.cutoff_rows}\n"
        f"  folded into sparse deltas: {stats.folded_rows} rows in "
        f"{stats.fold_groups} group(s) (net removal {stats.deleted_rows})"
    )


def run_compact_cli(test_args: list[str] | None = None) -> int:
    """CLI: python -m sqlite_history.compact data.db table [--protect YYYY-MM-DD ...]"""
    parser = argparse.ArgumentParser(
        prog="python -m sqlite_history.compact",
        description=(
            "Compact sqlite-history tables: fold old sparse updates while "
            "preserving protected days, tombstones, resurrections and all "
            "readback from the first day of the current month."
        ),
    )
    parser.add_argument("database_path", help="Path to the SQLite database file.")
    parser.add_argument("tables", nargs="+", help="Tracked table names to compact.")
    parser.add_argument(
        "--protect",
        action="append",
        default=[],
        metavar="YYYY-MM-DD",
        help="Protected day (UTC); history rows on this day are kept verbatim. Repeatable.",
    )
    parser.add_argument(
        "--cutoff",
        metavar="YYYY-MM-DD",
        help="Keep every row at/after this day 00:00 UTC (default: first of current month).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the plan without changing the database.",
    )
    parser.add_argument(
        "--backup",
        action="store_true",
        help="Copy the database to <database>.pre-compact before rewriting.",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Open the backup copy and verify readback equivalence after compaction.",
    )
    args = parser.parse_args(test_args)

    cutoff_ms = date_to_cutoff_ms(args.cutoff) if args.cutoff else month_start_ms_utc()
    protected_days = set(args.protect)

    db = sqlite3.connect(args.database_path)
    reports = []
    try:
        for table in args.tables:
            history_table = f"_{table}_history"
            exists = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?;",
                (history_table,),
            ).fetchone()
            if not exists:
                parser.error(f"History table {history_table} does not exist.")
            stats = plan_compaction(
                db,
                table,
                cutoff_ms=cutoff_ms,
                protected_days=protected_days,
            )
            reports.append((table, stats))

        print(f"Cutoff (keep everything at/after): {datetime.fromtimestamp(cutoff_ms / 1000, tz=timezone.utc).isoformat()}")
        print(f"Protected days (UTC): {', '.join(sorted(protected_days)) or '(none)'}")
        for table, stats in reports:
            print(f"[{table}] compaction plan")
            print(_format_stats(stats))

        if args.dry_run:
            return 0

        backup_path: str | None = None
        if args.backup or args.verify:
            backup_path = str(Path(args.database_path).with_suffix(".pre-compact.db"))
            db.commit()
            shutil.copy2(args.database_path, backup_path)
            print(f"Backup written: {backup_path}")

        for table, _ in reports:
            stats = compact_history(
                db,
                table,
                cutoff_ms=cutoff_ms,
                protected_days=protected_days,
            )
            print(f"[{table}] compacted")
            print(_format_stats(stats))
        db.commit()

        if args.verify:
            original = sqlite3.connect(backup_path)
            try:
                for table, _ in reports:
                    result = verify_compaction(
                        original,
                        db,
                        table,
                        cutoff_ms=cutoff_ms,
                        protected_days=protected_days,
                    )
                    status = "OK" if result["ok"] else "FAILED"
                    print(
                        f"[{table}] verify {status}: protected={result['protected_checked']} "
                        f"readback_ts={result['readback_timestamps_checked']} "
                        f"tombstones={result['tombstones_checked']}"
                    )
                    for failure in result["failures"]:
                        print(f"  ! {failure}")
                    if not result["ok"]:
                        return 1
            finally:
                original.close()
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(run_compact_cli())
