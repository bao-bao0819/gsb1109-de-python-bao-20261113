import sqlite3

from .sql import (
    COMMITS_TABLE,
    SEQ_TABLE,
    STATE_TABLE,
    COMMIT_COLUMN,
    backfill_sql,
    commit_infrastructure_sql,
    history_columns,
    history_table_sql,
    history_trigger_names,
    snapshot_sql,
    table_columns_and_types,
    table_exists,
    triggers_sql,
)


def _ensure_commit_infrastructure(db):
    db.executescript(commit_infrastructure_sql())


def configure_history(db, table):
    """Configure history triggers for a table in a database.

    Also creates the database-level commit coordination tables so that
    history rows written by different tables inside one tracked
    transaction share the same ``_commit`` id.

    An existing history table created by an older version of this library
    is upgraded in place: the ``_commit`` column is added (existing rows
    keep NULL, marking them as predating every commit-scoped snapshot) and
    the triggers are recreated so new rows record the current commit id.
    """
    columns_and_types = table_columns_and_types(db, table)
    columns = [r[0] for r in columns_and_types]
    with db:
        _ensure_commit_infrastructure(db)
        history_table = f"_{table}_history"
        if table_exists(db, history_table):
            if COMMIT_COLUMN not in history_columns(db, table):
                db.execute(
                    f"ALTER TABLE {history_table} ADD COLUMN {COMMIT_COLUMN} INTEGER;"
                )
            for trigger in history_trigger_names(db, table):
                db.execute(f"DROP TRIGGER {trigger};")
        else:
            db.executescript(history_table_sql(table, columns_and_types))
        db.executescript(triggers_sql(table, columns))
        if not db.execute(
            f"SELECT 1 FROM {history_table} LIMIT 1;"
        ).fetchone():
            db.execute(backfill_sql(table, columns))


class TrackedTransaction:
    """Context manager giving every write in a transaction one commit id.

    All history rows produced by triggers on any tracked table while the
    block is open are stamped with the same integer commit id, making the
    cross-table entries of one business transaction (for example a stock
    deduction in one warehouse table and the matching credit in another)
    identifiable as a pair without relying on timestamps.

    On normal exit the transaction is committed and a row recording the
    commit id is written atomically with it.  On an exception the whole
    transaction is rolled back; the history rows it produced disappear
    with it and no commit directory row is created.

    Because the sequence counter is updated inside the same write
    transaction, a rollback also rewinds the counter, so a rolled-back
    transaction's id may be handed out again to a later transaction.
    Nothing stamped with that id survives the rollback, so the reused id
    uniquely identifies the later committed transaction.  Allocation
    order is therefore identical to commit order (SQLite's single write
    lock is held from taking the id until COMMIT), which makes snapshot
    boundaries based on the id stable and repeatable.

    Savepoint-undo works the same way: rows undone by
    ``ROLLBACK TO SAVEPOINT`` never reach the committed history view.

    Writes performed outside a ``TrackedTransaction`` are still recorded by
    the triggers, but their ``_commit`` stays NULL, so they cannot be
    ordered relative to numbered commits.

    The block manages the transaction itself (``BEGIN IMMEDIATE`` ...
    COMMIT/ROLLBACK), so the connection must not already have an open
    transaction when it is entered.
    """

    def __init__(self, db, note=None):
        self.db = db
        self.note = note
        self.commit_id = None

    def __enter__(self):
        # BEGIN IMMEDIATE acquires SQLite's write lock up front, so two
        # connections taking a commit id cannot interleave and receive the
        # same id even when they start in the same millisecond.
        self.db.execute("BEGIN IMMEDIATE;")
        row = self.db.execute(
            f"SELECT next_commit FROM {SEQ_TABLE};"
        ).fetchone()
        self.commit_id = row[0]
        self.db.execute(
            f"UPDATE {SEQ_TABLE} SET next_commit = ? WHERE id = 1;",
            (self.commit_id + 1,),
        )
        self.db.execute(
            f"UPDATE {STATE_TABLE} SET current_commit = ? WHERE id = 1;",
            (self.commit_id,),
        )
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if exc_type is None:
            self.db.execute(
                f"INSERT INTO {COMMITS_TABLE} (_commit, _committed_at, _note) "
                "VALUES (?, cast((julianday('now') - 2440587.5) * 86400 * 1000 as integer), ?);",
                (self.commit_id, self.note),
            )
            self.db.execute(
                f"UPDATE {STATE_TABLE} SET current_commit = NULL WHERE id = 1;"
            )
            self.db.commit()
        else:
            self.db.rollback()
        return False


def tracked(db, note=None):
    """Return a context manager for one cross-table commit.

    Typical transfer between two tracked tables::

        with sqlite_history.tracked(conn, note="transfer T-42") as tx:
            conn.execute("UPDATE warehouse_a SET qty = qty - 5 WHERE sku = ?", (sku,))
            conn.execute("UPDATE warehouse_b SET qty = qty + 5 WHERE sku = ?", (sku,))
        print(tx.commit_id)
    """
    return TrackedTransaction(db, note=note)


def current_commit(db):
    """Return the commit id active on this connection, or None."""
    row = db.execute(
        f"SELECT current_commit FROM {STATE_TABLE} WHERE id = 1;"
    ).fetchone()
    return None if row is None else row[0]


def commit_exists(db, commit):
    """Return True if commit id ``commit`` was committed (not rolled back)."""
    row = db.execute(
        f"SELECT 1 FROM {COMMITS_TABLE} WHERE _commit = ?;", (int(commit),)
    ).fetchone()
    return row is not None


def snapshot_rows(db, table, commit, mode="after"):
    """Return tracked rows as they were immediately before/after a commit.

    ``mode="after"`` includes every history entry of commit ``commit`` and
    all earlier commits; ``mode="before"`` stops just short of it, so both
    sides of the posting are fully reconstructable.

    History rows with a NULL ``_commit`` (backfilled rows, pre-upgrade
    records, or untracked writes) have no commit ordering evidence and are
    treated as predating every snapshot.

    Raises ``ValueError`` if the commit id is not present in the commit
    directory - for example because that transaction was rolled back, so
    no committed view exists for it.
    """
    commit = int(commit)
    if not commit_exists(db, commit):
        raise ValueError(
            f"commit {commit} is not a committed transaction (rolled back or never registered)"
        )
    columns = [name for name, _ in table_columns_and_types(db, table)]
    sql, params = snapshot_sql(table, columns, commit, mode=mode)
    cursor = db.execute(sql, params)
    names = [description[0] for description in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def connect(database):
    """Open a sqlite3 connection with history-friendly row access."""
    db = sqlite3.connect(database)
    db.row_factory = sqlite3.Row
    return db


__all__ = [
    "configure_history",
    "tracked",
    "TrackedTransaction",
    "current_commit",
    "commit_exists",
    "snapshot_rows",
    "snapshot_sql",
    "connect",
]
