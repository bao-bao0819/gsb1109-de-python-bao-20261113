from .sql import (
    backfill_batch_sql,
    backfill_sql,
    backfill_state_table_sql,
    history_table_sql,
    triggers_sql,
    table_columns_and_types,
)


def configure_history(db, table, batch_size=None):
    """Configure history triggers for a table in a database.

    If batch_size is None, existing rows are backfilled in a single
    transaction. Otherwise the backfill runs in batches of batch_size
    rows, each committed separately so other connections can keep
    writing between batches - see backfill().
    """
    # Get the table schema
    columns_and_types = table_columns_and_types(db, table)
    columns = [r[0] for r in columns_and_types]
    with db:
        db.executescript(history_table_sql(table, columns_and_types))
        # Create the triggers
        db.executescript(triggers_sql(table, columns))
        if batch_size is None:
            # Backfill the history table in one go
            db.execute(backfill_sql(table, columns))
    if batch_size is not None:
        backfill(db, table, batch_size)


def backfill_status(db, table):
    """Return backfill progress for a table as a dict with last_rowid,
    max_rowid and done keys, or None if no backfill has been recorded."""
    exists = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = '_history_backfill'"
    ).fetchone()
    if not exists:
        return None
    row = db.execute(
        "SELECT last_rowid, max_rowid, done FROM _history_backfill WHERE table_name = ?",
        (table,),
    ).fetchone()
    if row is None:
        return None
    return {"last_rowid": row[0], "max_rowid": row[1], "done": bool(row[2])}


def backfill_batch(db, table, batch_size=1000):
    """Backfill a single batch of rows and commit, recording progress in
    _history_backfill so an interrupted backfill can resume on any
    connection. Returns the number of rows considered in this batch.

    Rows that already have history records (captured by the triggers
    before this batch reached them) are skipped, so the version chain
    for each rowid stays complete without duplicates or gaps.
    """
    columns = [r[0] for r in table_columns_and_types(db, table)]
    db.execute(backfill_state_table_sql())
    row = db.execute(
        "SELECT last_rowid, max_rowid, done FROM _history_backfill WHERE table_name = ?",
        (table,),
    ).fetchone()
    if row is not None and row[2]:
        return 0
    if row is None:
        last_rowid = None
        # Rows beyond this high-water mark are captured by the triggers
        max_rowid = db.execute(
            "SELECT MAX(rowid) FROM {table}".format(table=table)
        ).fetchone()[0]
    else:
        last_rowid, max_rowid = row[0], row[1]
    with db:
        if max_rowid is None:
            # Table was empty when the backfill started
            batch = []
        else:
            batch = db.execute(
                "SELECT rowid FROM {table} "
                "WHERE (?1 IS NULL OR rowid > ?1) AND rowid <= ?2 "
                "ORDER BY rowid LIMIT ?3".format(table=table),
                (last_rowid, max_rowid, batch_size),
            ).fetchall()
        if batch:
            batch_max_rowid = batch[-1][0]
            db.execute(
                backfill_batch_sql(table, columns), (last_rowid, batch_max_rowid)
            )
            done = 1 if len(batch) < batch_size or batch_max_rowid >= max_rowid else 0
        else:
            batch_max_rowid = last_rowid
            done = 1
        db.execute(
            "INSERT OR REPLACE INTO _history_backfill "
            "(table_name, last_rowid, max_rowid, done) VALUES (?, ?, ?, ?)",
            (table, batch_max_rowid, max_rowid, done),
        )
    return len(batch)


def backfill(db, table, batch_size=1000):
    """Run batched backfill to completion, resuming from any progress
    recorded in _history_backfill by a previous (interrupted) run.
    Returns the total number of rows considered by this call."""
    total = 0
    while True:
        count = backfill_batch(db, table, batch_size)
        if count == 0:
            break
        total += count
        if count < batch_size:
            break
    return total
