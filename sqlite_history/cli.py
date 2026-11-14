import argparse
import sqlite3
import os
import sys
from . import configure_history
from .merge import create_receipt, merge_databases


def configure_triggers(database_path, tables):
    db = sqlite3.connect(database_path)
    for table in tables:
        if table.startswith("_"):
            continue
        # Does a history table exist already?
        history_table_name = f"_{table}_history"
        cursor = db.execute(
            f"SELECT name FROM sqlite_master WHERE type='table' AND name='{history_table_name}';"
        )
        if cursor.fetchone():
            print(f"History table {history_table_name} already exists - skipping.")
            continue
        configure_history(db, table)


def all_regular_tables(database_path):
    """'Regular' excludes FTS and related tables."""
    conn = sqlite3.connect(database_path)
    cursor = conn.cursor()
    hidden_tables = [
        r[0]
        for r in (
            cursor.execute(
                """
            SELECT NAME FROM sqlite_master
            WHERE type = 'table'
            AND (
                sql LIKE '%VIRTUAL TABLE%USING FTS%'
            ) OR name IN ('sqlite_stat1', 'sqlite_stat2', 'sqlite_stat3', 'sqlite_stat4')
        """
            )
        ).fetchall()
    ]
    hidden_tables_copy = hidden_tables[:]
    regular_tables = []
    for row in cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table';"
    ).fetchall():
        table_name = row[0]
        should_be_hidden = any(
            table_name.startswith(hidden_table) for hidden_table in hidden_tables_copy
        )
        if not should_be_hidden:
            regular_tables.append(table_name)
    return regular_tables


def run_checkout(argv):
    parser = argparse.ArgumentParser(
        prog="sqlite-history checkout",
        description="Record a checkout receipt (shared starting point) for "
        "later offline merges.",
    )
    parser.add_argument("database_path", help="Path to the SQLite database file.")
    parser.add_argument(
        "tables",
        nargs="*",
        help="Tables to include (defaults to all history-tracked tables).",
    )
    args = parser.parse_args(argv)
    if not os.path.exists(args.database_path):
        parser.error("Database file does not exist.")
    db = sqlite3.connect(args.database_path)
    receipt_id = create_receipt(db, tables=args.tables or None)
    print(f"Created receipt {receipt_id}")


def run_merge(argv):
    parser = argparse.ArgumentParser(
        prog="sqlite-history merge",
        description="Merge a branch database back into HQ using a shared "
        "checkout receipt.",
    )
    parser.add_argument("hq_path", help="Path to the HQ (merge target) database.")
    parser.add_argument("branch_path", help="Path to the branch database.")
    parser.add_argument(
        "--receipt",
        type=int,
        required=True,
        help="Receipt ID recorded at checkout time.",
    )
    parser.add_argument(
        "--on-conflict",
        choices=["hq", "branch", "error"],
        default="hq",
        help="How to resolve same-field/different-value conflicts "
        "(default: hq). Deletes always win over edits.",
    )
    args = parser.parse_args(argv)
    for path in (args.hq_path, args.branch_path):
        if not os.path.exists(path):
            parser.error(f"Database file does not exist: {path}")
    hq_db = sqlite3.connect(args.hq_path)
    branch_db = sqlite3.connect(args.branch_path)
    report = merge_databases(
        hq_db, branch_db, args.receipt, on_conflict=args.on_conflict
    )
    print(
        "Merge {merge_id} (receipt {receipt_id}): "
        "{insert} inserted, {update} updated, {delete} deleted, "
        "{none} unchanged, {conflicts} conflicts, {incomplete} incomplete".format(
            merge_id=report.merge_id,
            receipt_id=report.receipt_id,
            insert=report.applied["insert"],
            update=report.applied["update"],
            delete=report.applied["delete"],
            none=report.applied["none"],
            conflicts=len(report.conflicts),
            incomplete=len(report.incomplete),
        )
    )


def run(test_args=None):
    argv = list(test_args) if test_args is not None else sys.argv[1:]
    if argv and argv[0] == "checkout":
        run_checkout(argv[1:])
        return
    if argv and argv[0] == "merge":
        run_merge(argv[1:])
        return

    parser = argparse.ArgumentParser(
        description="Configure sqlite-history triggers for one or more tables."
    )

    parser.add_argument("database_path", help="Path to the SQLite database file.")
    parser.add_argument(
        "tables", nargs="*", help="One or more table names to configure."
    )
    parser.add_argument(
        "--all", action="store_true", help="Configure for all tables in the database."
    )

    args = parser.parse_args(argv)

    if args.all:
        args.tables = all_regular_tables(args.database_path)
    elif len(args.tables) == 0:
        parser.error(
            "No tables provided. Please provide table names or use --all flag."
        )

    # Error if database_path doesn't exist
    if not os.path.exists(args.database_path):
        parser.error("Database file does not exist.")

    # Error if any of the tables don't exist
    all_table_names = all_regular_tables(args.database_path)
    missing_tables = [table for table in args.tables if table not in all_table_names]
    if missing_tables:
        parser.error("The following tables do not exist: " + ", ".join(missing_tables))

    configure_triggers(args.database_path, args.tables)
