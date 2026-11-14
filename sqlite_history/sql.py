import re


# Database-level tables that coordinate commit ids across every tracked table.
COMMITS_TABLE = "_history_commits"
SEQ_TABLE = "_history_commit_seq"
STATE_TABLE = "_history_state"
COMMIT_COLUMN = "_commit"


def commit_infrastructure_sql():
    """Return SQL creating the cross-table commit coordination tables.

    ``_history_commits`` is the directory of transactions that reached
    COMMIT (a row inserted before commit disappears on ROLLBACK, so rolled
    back transactions never appear here).

    ``_history_commit_seq`` hands out monotonically increasing commit ids.
    It is updated inside ``BEGIN IMMEDIATE`` so SQLite's single-writer lock
    guarantees a unique id per writing transaction, even at the same
    millisecond.

    ``_history_state`` holds the commit id of the transaction currently
    running on this connection; history triggers read it.  It is NULL for
    writes that are not wrapped in a tracked transaction.
    """
    return """
CREATE TABLE IF NOT EXISTS {commits} (
    _commit INTEGER PRIMARY KEY,
    _committed_at INTEGER,
    _note TEXT
);
CREATE TABLE IF NOT EXISTS {seq} (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    next_commit INTEGER NOT NULL
);
INSERT OR IGNORE INTO {seq} (id, next_commit) VALUES (1, 1);
CREATE TABLE IF NOT EXISTS {state} (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    current_commit INTEGER
);
INSERT OR IGNORE INTO {state} (id, current_commit) VALUES (1, NULL);
""".format(
        commits=COMMITS_TABLE, seq=SEQ_TABLE, state=STATE_TABLE
    )


def history_table_sql(table, columns_and_types):
    """Return SQL for history table for a table and its columns."""
    if isinstance(columns_and_types, dict):
        columns_and_types = dict.items()
    column_names = ",\n".join(
        "    {name} {type}".format(name=escape_sqlite(name), type=type)
        for name, type in columns_and_types
    )
    return """
CREATE TABLE _{table}_history (
    _rowid INTEGER,
{column_names},
    _version INTEGER,
    _updated INTEGER,
    _mask INTEGER,
    _commit INTEGER
);
CREATE INDEX idx_{table}_history_rowid ON _{table}_history (_rowid);
""".format(
        table=table, column_names=column_names
    )


def _current_commit_sql():
    return "(SELECT current_commit FROM {state} WHERE id = 1)".format(
        state=STATE_TABLE
    )


def triggers_sql(table, columns):
    """Return SQL for triggers for a table and its columns."""
    column_names = ", ".join(escape_sqlite(column) for column in columns)
    new_column_values = ", ".join("new." + escape_sqlite(column) for column in columns)
    old_column_values = ", ".join("old." + escape_sqlite(column) for column in columns)
    current_commit = _current_commit_sql()
    # mask is a bit mask of all columns, so len(columns)
    mask = 2 ** len(columns) - 1
    insert_trigger = """
CREATE TRIGGER {table}_insert_history
AFTER INSERT ON {table}
BEGIN
    INSERT INTO _{table}_history (_rowid, {column_names}, _version, _updated, _mask, _commit)
    VALUES (new.rowid, {new_column_values}, 1, cast((julianday('now') - 2440587.5) * 86400 * 1000 as integer), {mask}, {current_commit});
END;
""".format(
        table=table,
        column_names=column_names,
        new_column_values=new_column_values,
        mask=mask,
        current_commit=current_commit,
    )
    update_columns = []
    for column in columns:
        update_columns.append(
            """
        CASE WHEN old.{column} != new.{column} then new.{column} else null end""".format(
                column=escape_sqlite(column)
            )
        )
    update_columns_sql = ", ".join(update_columns)
    mask_sql = " + ".join(
        """(CASE WHEN old.{column} != new.{column} then {base} else 0 end)""".format(
            column=escape_sqlite(column),
            base=2**idx,
        )
        for idx, column in enumerate(columns)
    )
    where_sql = " or ".join(
        "old.{column} != new.{column}".format(column=escape_sqlite(column))
        for column in columns
    )
    update_trigger = """
CREATE TRIGGER {table}_update_history
AFTER UPDATE ON {table}
FOR EACH ROW
BEGIN
    INSERT INTO _{table}_history (_rowid, {column_names}, _version, _updated, _mask, _commit)
    SELECT old.rowid, {update_columns_sql},
        (SELECT MAX(_version) FROM _{table}_history WHERE _rowid = old.rowid) + 1,
        cast((julianday('now') - 2440587.5) * 86400 * 1000 as integer),
        {mask_sql},
        {current_commit}
    WHERE {where_sql};
END;
""".format(
        table=table,
        column_names=column_names,
        update_columns_sql=update_columns_sql,
        mask_sql=mask_sql,
        where_sql=where_sql,
        current_commit=current_commit,
    )
    delete_trigger = """
CREATE TRIGGER {table}_delete_history
AFTER DELETE ON {table}
BEGIN
    INSERT INTO _{table}_history (_rowid, {column_names}, _version, _updated, _mask, _commit)
    VALUES (
        old.rowid,
        {old_column_values},
        (SELECT COALESCE(MAX(_version), 0) from _{table}_history WHERE _rowid = old.rowid) + 1,
        cast((julianday('now') - 2440587.5) * 86400 * 1000 as integer),
        -1,
        {current_commit}
    );
END;
""".format(
        table=table,
        column_names=column_names,
        old_column_values=old_column_values,
        current_commit=current_commit,
    )
    return insert_trigger + update_trigger + delete_trigger


def backfill_sql(table, columns):
    column_names = ", ".join(escape_sqlite(column) for column in columns)
    sql = """
INSERT INTO _{table}_history (_rowid, {column_names}, _version, _updated, _mask, _commit)
SELECT rowid, {column_names}, 1, cast((julianday('now') - 2440587.5) * 86400 * 1000 as integer), {mask}, NULL
FROM {table};
""".format(
        table=table, column_names=column_names, mask=2 ** len(columns) - 1
    )
    return sql


def snapshot_sql(table, columns, commit, mode="after"):
    """Return (sql, params) reconstructing tracked rows at a given commit.

    ``mode="after"`` (the default) returns the balance immediately after
    commit ``commit`` was posted; ``mode="before"`` returns the balance
    immediately before it was posted.

    Visibility rules:

    * History rows carrying a commit id are included when their id is at or
      below the snapshot boundary (``< commit`` for "before", ``<= commit``
      for "after").  Rows of rolled back transactions and savepoint-undo
      ranges are physically absent from the history table, so they never
      need filtering here.
    * History rows whose ``_commit`` is NULL - backfilled rows, rows from
      before the commit infrastructure existed, or writes not wrapped in a
      tracked transaction - have no commit ordering evidence and are
      treated as predating every snapshot.
    """
    if mode not in ("before", "after"):
        raise ValueError("mode must be 'before' or 'after', got {!r}".format(mode))
    boundary = int(commit) - (1 if mode == "before" else 0)
    history = "_{}_history".format(table)
    # For each column, take the value from the highest-version visible row
    # in which that column's mask bit was set.  Rows ending in a delete
    # (_mask = -1) are removed by the HAVING clause.
    column_selects = ",\n    ".join(
        """(SELECT v2.{column} FROM visible v2
        WHERE v2._rowid = v._rowid
            AND (v2._mask & {bit}) <> 0
        ORDER BY v2._version DESC LIMIT 1) AS {column}""".format(
            column=escape_sqlite(column), bit=2**idx
        )
        for idx, column in enumerate(columns)
    )
    sql = """
WITH visible AS (
    SELECT * FROM {history}
    WHERE {commit_column} IS NULL OR {commit_column} <= ?
)
SELECT
    v._rowid,
    {column_selects}
FROM visible v
GROUP BY v._rowid
HAVING (
    SELECT v3._mask FROM visible v3
    WHERE v3._rowid = v._rowid
    ORDER BY v3._version DESC LIMIT 1
) <> -1
ORDER BY v._rowid;
""".format(
        history=history,
        commit_column=COMMIT_COLUMN,
        column_selects=column_selects,
    )
    return sql, (boundary,)


def table_columns_and_types(db, table):
    cursor = db.execute(f"PRAGMA table_info([{table}]);")
    columns_and_types = []
    for row in cursor.fetchall():
        columns_and_types.append((row[1], row[2]))
    return columns_and_types


def table_exists(db, table):
    row = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?;", (table,)
    ).fetchone()
    return row is not None


def history_columns(db, table):
    cursor = db.execute(f"PRAGMA table_info(_{table}_history);")
    return [row[1] for row in cursor.fetchall()]


def history_trigger_names(db, table):
    # Triggers are attached to the tracked table, not the history table.
    cursor = db.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name = ?;",
        (table,),
    )
    prefix = f"{table}_"
    suffix = "_history"
    return [
        row[0]
        for row in cursor.fetchall()
        if row[0].startswith(prefix) and row[0].endswith(suffix)
    ]


# From https://www.sqlite.org/lang_keywords.html
reserved_words = set(
    (
        "abort action add after all alter analyze and as asc attach autoincrement "
        "before begin between by cascade case cast check collate column commit "
        "conflict constraint create cross current_date current_time "
        "current_timestamp database default deferrable deferred delete desc detach "
        "distinct drop each else end escape except exclusive exists explain fail "
        "for foreign from full glob group having if ignore immediate in index "
        "indexed initially inner insert instead intersect into is isnull join key "
        "left like limit match natural no not notnull null of offset on or order "
        "outer plan pragma primary query raise recursive references regexp reindex "
        "release rename replace restrict right rollback row savepoint select set "
        "table temp temporary then to transaction trigger union unique update using "
        "vacuum values view virtual when where with without"
    ).split()
)

_boring_keyword_re = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def escape_sqlite(s):
    if _boring_keyword_re.match(s) and (s.lower() not in reserved_words):
        return s
    else:
        return f"[{s}]"
