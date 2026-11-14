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
    _mask INTEGER
);
CREATE INDEX idx_people_history_rowid ON _people_history (_rowid);
```
<!-- [[[end]]] -->
The `_rowid` column references the `rowid` of the row in the original table that is being tracked. If a row has been updated multiple times there will be multiple rows with the same `_rowid` in this table.

The `id`, `name`, `age` and `weight` columns represent the new values assigned to the row when it was updated. A `null` in one of these columns means the column was not touched by that update - unless the column's bit is set in `_mask`, in which case the column was explicitly cleared to `null`.

The `_version` column is an integer that starts at 1 when a row is inserted and is incremented each time the row is updated. If a row is deleted and the same `rowid` is later re-inserted, the new row starts a new lifecycle at version 1 - its versions are not connected to the previous row that used that `rowid`.

The `_updated` column is a timestamp showing when the change was recorded. This is stored in milliseconds since the Unix epoch - to convert that to a human-readable UTC date you can use `strftime('%Y-%m-%d %H:%M:%S', _updated / 1000, 'unixepoch')` in your SQL queries.

The `_mask` column is a bit mask that indicates which columns changed in an update. The bit mask is calculated by adding together the following values:

    1: id
    2: name
    4: age
    8: weight

Tables with different schemas will have different `_mask` values.

A `_mask` of `-1` indicates that the row was deleted.

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
    INSERT INTO _people_history (_rowid, id, name, age, weight, _version, _updated, _mask)
    VALUES (new.rowid, new.id, new.name, new.age, new.weight, 1, cast((julianday('now') - 2440587.5) * 86400 * 1000 as integer), 15);
END;

CREATE TRIGGER people_update_history
AFTER UPDATE ON people
FOR EACH ROW
BEGIN
    INSERT INTO _people_history (_rowid, id, name, age, weight, _version, _updated, _mask)
    SELECT old.rowid, 
        CASE WHEN old.id IS NOT new.id then new.id else null end, 
        CASE WHEN old.name IS NOT new.name then new.name else null end, 
        CASE WHEN old.age IS NOT new.age then new.age else null end, 
        CASE WHEN old.weight IS NOT new.weight then new.weight else null end,
        (SELECT COALESCE(MAX(_version), 0) FROM _people_history WHERE _rowid = old.rowid AND rowid > COALESCE((SELECT MAX(rowid) FROM _people_history WHERE _rowid = old.rowid AND _mask = -1), 0)) + 1,
        cast((julianday('now') - 2440587.5) * 86400 * 1000 as integer),
        (CASE WHEN old.id IS NOT new.id then 1 else 0 end) + (CASE WHEN old.name IS NOT new.name then 2 else 0 end) + (CASE WHEN old.age IS NOT new.age then 4 else 0 end) + (CASE WHEN old.weight IS NOT new.weight then 8 else 0 end)
    WHERE old.id IS NOT new.id or old.name IS NOT new.name or old.age IS NOT new.age or old.weight IS NOT new.weight;
END;

CREATE TRIGGER people_delete_history
AFTER DELETE ON people
BEGIN
    INSERT INTO _people_history (_rowid, id, name, age, weight, _version, _updated, _mask)
    VALUES (
        old.rowid,
        old.id, old.name, old.age, old.weight,
        (SELECT COALESCE(MAX(_version), 0) FROM _people_history WHERE _rowid = old.rowid AND rowid > COALESCE((SELECT MAX(rowid) FROM _people_history WHERE _rowid = old.rowid AND _mask = -1), 0)) + 1,
        cast((julianday('now') - 2440587.5) * 86400 * 1000 as integer),
        -1
    );
END;
```
<!-- [[[end]]] -->

## Reading history back

Use `row_state()` to reconstruct the state of a row at a version in its current lifecycle:

    import sqlite_history

    state = sqlite_history.row_state(conn, "people", 1, version=2)

It returns a dictionary with:

- `version` and `updated`: the version number and timestamp of the reconstructed state
- `changed`: just the columns that version touched, mapped to their new values. A column mapping to `None` was explicitly cleared by that version; a column absent from `changed` was not touched by it
- `row`: the full reconstructed row. Columns set to `None` were recorded as cleared; columns set to `sqlite_history.UNKNOWN` have no recorded value in the current lifecycle (for example columns added to the table after history was configured)

Omit `version` to get the latest state. If the row's most recent event is a delete the function returns `None`. Reconstruction never crosses a delete: if a `rowid` was re-inserted after being deleted, only the new row's lifecycle is visible, and asking for a version from the previous lifecycle raises `ValueError`.

History recorded by older versions of the triggers is read back as recorded - values those triggers failed to capture are not fabricated.

## Development

To contribute to this library, first checkout the code. Then create a new virtual environment:

    cd sqlite-history
    python -m venv venv
    source venv/bin/activate

Now install the dependencies and test dependencies:

    pip install -e '.[test]'

To run the tests:

    pytest
