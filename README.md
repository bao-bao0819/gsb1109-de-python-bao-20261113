# sqlite-history

[![PyPI](https://img.shields.io/pypi/v/sqlite-history.svg)](https://pypi.org/project/sqlite-history/)
[![Tests](https://github.com/simonw/sqlite-history/workflows/Test/badge.svg)](https://github.com/simonw/sqlite-history/actions?query=workflow%3ATest)
[![Changelog](https://img.shields.io/github/v/release/simonw/sqlite-history?include_prereleases&label=changelog)](https://github.com/simonw/sqlite-history/releases)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](https://github.com/simonw/sqlite-history/blob/main/LICENSE)

Track changes to SQLite tables using triggers

For more on this project: [sqlite-history: tracking changes to SQLite tables using triggers](https://simonwillison.net/2023/Apr/15/sqlite-history/)

## Installation

Install this library using `pip`:

    pip install sqlite-history

## Usage

This library can be used to configure triggers on a SQLite database such that any inserts, updates or deletes against a table will have their changes recorded in a separate table.

You can enable history tracking for a table using the `configure_history()` function:

    import sqlite_history
    import sqlite3

    conn = sqlite3.connect("data.db")
    conn.execute("CREATE TABLE table1 (id INTEGER PRIMARY KEY, name TEXT)")
    sqlite_history.configure_history(conn, "table1")

Or you can use the CLI interface, available via `python -m sqlite_history`:

    python -m sqlite_history data.db table1 [table2 table3 ...]

Use `--all` to configure it for all tables:

    python -m sqlite_history data.db --all

## Cross-table commit ids

When one business transaction writes to more than one table - for example
deducting stock from warehouse A and crediting it to warehouse B - the rows
it produces need a shared identifier so the two sides can be paired.
Timestamps cannot do this, because several transactions can be recorded in the
same millisecond.

Wrap the writes in `sqlite_history.tracked()`. Every history row the triggers
write while the block is open is stamped with the same integer commit id:

    import sqlite_history

    with sqlite_history.tracked(conn, note="transfer T-42") as tx:
        conn.execute("UPDATE warehouse_a SET qty = qty - 30 WHERE sku = ?", (sku,))
        conn.execute("UPDATE warehouse_b SET qty = qty + 30 WHERE sku = ?", (sku,))
    print(tx.commit_id)  # e.g. 7

The deduction row in `_warehouse_a_history` and the credit row in
`_warehouse_b_history` both carry `_commit = 7`, so they can be joined as one
logical posting without relying on timestamps. The id is allocated under
SQLite's write lock (`BEGIN IMMEDIATE`), so concurrent connections - even ones
committing in the same millisecond - always receive distinct ids.

Commit ids are recorded in a `_history_commits` directory table. A directory
row is inserted in the same transaction as the writes, so:

- If the block raises, the whole transaction is rolled back. The history rows
  the triggers wrote disappear together with the business rows, and no
  directory row is created. Rolled-back transfers therefore never appear in
  any committed view.
- Rows undone with `ROLLBACK TO SAVEPOINT` inside the transaction are removed
  by SQLite in the same way and never reach the committed history view.
- The id counter is updated inside the write transaction too, so a rollback
  rewinds it and the id may be reused by a later transaction. Nothing stamped
  with that id survives the rollback, so the reused id still identifies
  exactly one committed transaction.

Writes performed outside `tracked()` are still captured by the triggers, but
their `_commit` stays `NULL`; they cannot be ordered relative to numbered
commits.

## Reading a balance before or after a commit

`sqlite_history.snapshot_rows()` reconstructs the rows of a tracked table at a
commit boundary. Pass `mode="after"` for the balance immediately after a
commit was posted and `mode="before"` for the balance immediately before it:

    before = sqlite_history.snapshot_rows(conn, "warehouse_a", 7, mode="before")
    after = sqlite_history.snapshot_rows(conn, "warehouse_a", 7, mode="after")

A snapshot only includes history rows whose `_commit` is at or below the
boundary (`< commit` for "before", `<= commit` for "after"). Asking for a
commit that was rolled back (or never registered) raises `ValueError`, since no
committed view exists for it.

### Legacy rows without commit evidence

History rows whose `_commit` is `NULL` - rows backfilled from an existing
table, rows written by an older version of this library, or writes not wrapped
in `tracked()` - have no evidence placing them relative to numbered commits.
They are explicitly defined as **predating every commit-scoped snapshot**, so
they are always included and form the baseline the first numbered commit is
read against. Reconfiguring a table created by an older version migrates its
history table in place: the `_commit` column is added and existing rows keep
`NULL`.

## How this works

Given a table with the following schema:

<!-- [[[cog
import cog
create_table_sql = """
CREATE TABLE people (
    id INTEGER PRIMARY KEY,
    name TEXT,
    age INTEGER,
    weight REAL
);
""".strip()
cog.out(
    "```sql\n{}\n```".format(create_table_sql)
)
]]] -->
```sql
CREATE TABLE people (
    id INTEGER PRIMARY KEY,
    name TEXT,
    age INTEGER,
    weight REAL
);
```
<!-- [[[end]]] -->

This library will create a new table called `_people_history` with the following schema:

<!-- [[[cog
from sqlite_history import sql
import sqlite3
db = sqlite3.connect(":memory:")
db.execute(create_table_sql)
columns_and_types = sql.table_columns_and_types(db, "people")
history_schema = sql.history_table_sql("people", columns_and_types)
cog.out(
    "```sql\n{}\n```".format(history_schema.strip())
)
]]] -->
```sql
CREATE TABLE _people_history (
    _rowid INTEGER,
    id INTEGER,
    name TEXT,
    age INTEGER,
    weight REAL,
    _version INTEGER,
    _updated INTEGER,
    _mask INTEGER,
    _commit INTEGER
);
CREATE INDEX idx_people_history_rowid ON _people_history (_rowid);
```
<!-- [[[end]]] -->
The `_rowid` column references the `rowid` of the row in the original table that is being tracked. If a row has been updated multiple times there will be multiple rows with the same `_rowid` in this table.

The `id`, `name`, `age` and `weight` columns represent the new values assigned to the row when it was updated. These can also be `null`, which might represent no change or might represent the value being set to `null` (hence the `_mask` column).

The `_version` column is a monotonically increasing integer that is incremented each time a row is updated.

The `_updated` column is a timestamp showing when the change was recorded. This is stored in milliseconds since the Unix epoch - to convert that to a human-readable UTC date you can use `strftime('%Y-%m-%d %H:%M:%S', _updated / 1000, 'unixepoch')` in your SQL queries.

The `_mask` column is a bit mask that indicates which columns changed in an update. The bit mask is calculated by adding together the following values:

    1: id
    2: name
    4: age
    8: weight

Tables with different schemas will have different `_mask` values.

A `_mask` of `-1` indicates that the row was deleted.

The `_commit` column is the cross-table commit id of the transaction that wrote the history row. All history rows produced by the same `sqlite_history.tracked()` block - across every tracked table - share one value, which also appears in the `_history_commits` directory table. It is `NULL` for backfilled rows, rows from an older library version, and writes not wrapped in `tracked()`.

Three extra database-level tables coordinate the ids: `_history_commits` (the directory of committed transactions), `_history_commit_seq` (the monotonic id counter) and `_history_state` (the commit id active on the current connection, read by the triggers).

The following triggers are created to populate the `_people_history` table:
<!-- [[[cog
triggers_sql = sql.triggers_sql("people", [c[0] for c in columns_and_types])
cog.out(
    "```sql\n{}\n```".format(triggers_sql.strip())
)
]]] -->
```sql
CREATE TRIGGER people_insert_history
AFTER INSERT ON people
BEGIN
    INSERT INTO _people_history (_rowid, id, name, age, weight, _version, _updated, _mask, _commit)
    VALUES (new.rowid, new.id, new.name, new.age, new.weight, 1, cast((julianday('now') - 2440587.5) * 86400 * 1000 as integer), 15, (SELECT current_commit FROM _history_state WHERE id = 1));
END;

CREATE TRIGGER people_update_history
AFTER UPDATE ON people
FOR EACH ROW
BEGIN
    INSERT INTO _people_history (_rowid, id, name, age, weight, _version, _updated, _mask, _commit)
    SELECT old.rowid, 
        CASE WHEN old.id != new.id then new.id else null end, 
        CASE WHEN old.name != new.name then new.name else null end, 
        CASE WHEN old.age != new.age then new.age else null end, 
        CASE WHEN old.weight != new.weight then new.weight else null end,
        (SELECT MAX(_version) FROM _people_history WHERE _rowid = old.rowid) + 1,
        cast((julianday('now') - 2440587.5) * 86400 * 1000 as integer),
        (CASE WHEN old.id != new.id then 1 else 0 end) + (CASE WHEN old.name != new.name then 2 else 0 end) + (CASE WHEN old.age != new.age then 4 else 0 end) + (CASE WHEN old.weight != new.weight then 8 else 0 end),
        (SELECT current_commit FROM _history_state WHERE id = 1)
    WHERE old.id != new.id or old.name != new.name or old.age != new.age or old.weight != new.weight;
END;

CREATE TRIGGER people_delete_history
AFTER DELETE ON people
BEGIN
    INSERT INTO _people_history (_rowid, id, name, age, weight, _version, _updated, _mask, _commit)
    VALUES (
        old.rowid,
        old.id, old.name, old.age, old.weight,
        (SELECT COALESCE(MAX(_version), 0) from _people_history WHERE _rowid = old.rowid) + 1,
        cast((julianday('now') - 2440587.5) * 86400 * 1000 as integer),
        -1,
        (SELECT current_commit FROM _history_state WHERE id = 1)
    );
END;
```
<!-- [[[end]]] -->

## Development

To contribute to this library, first checkout the code. Then create a new virtual environment:

    cd sqlite-history
    python -m venv venv
    source venv/bin/activate

Now install the dependencies and test dependencies:

    pip install -e '.[test]'

To run the tests:

    pytest
