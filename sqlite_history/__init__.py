from .sql import (
    META_COLUMNS,
    backfill_sql,
    decode_mask,
    drop_trigger_statements,
    escape_sqlite,
    history_table_statements,
    history_table_sql,
    schema_table_sql,
    schema_versions,
    table_columns_and_types,
    trigger_statements,
    triggers_sql,
)


def configure_history(db, table):
    """Configure history triggers for a table in a database.

    Creates the history table, schema registry and triggers if they do not
    exist yet. If the table schema changed since history was configured, the
    history table is migrated in a single transaction: new columns are added
    (existing columns are never dropped or rewritten), a new schema version
    recording the current column layout is registered, and the triggers are
    rebuilt against it. Existing history rows keep the column layout - and
    the ``_mask`` bitmap meaning - of the schema version they were written
    with, recorded in their ``_schema_version`` column.
    """
    conn = getattr(db, "conn", db)
    columns_and_types = table_columns_and_types(conn, table)
    columns = [row[0] for row in columns_and_types]
    history_table = "_{}_history".format(table)
    own_transaction = not getattr(conn, "in_transaction", False)
    if own_transaction:
        conn.execute("BEGIN")
    try:
        _configure_history(conn, table, history_table, columns_and_types, columns)
        if own_transaction:
            conn.execute("COMMIT")
    except Exception:
        if own_transaction:
            conn.execute("ROLLBACK")
        raise


def _configure_history(conn, table, history_table, columns_and_types, columns):
    conn.execute(schema_table_sql(table))
    if not _table_exists(conn, history_table):
        for statement in history_table_statements(table, columns_and_types):
            conn.execute(statement)
        _record_schema(conn, table, 1, columns_and_types)
        for statement in trigger_statements(table, columns, schema_version=1):
            conn.execute(statement)
        conn.execute(backfill_sql(table, columns, schema_version=1))
        return
    # History table already exists - migrate it if the schema has changed
    history_columns = [
        (name, type_)
        for name, type_ in table_columns_and_types(conn, history_table)
        if name not in META_COLUMNS
    ]
    versions = schema_versions(conn, table)
    if not versions:
        # History table predates schema tracking - register its current
        # layout as version 1, since that is what existing rows were
        # written against
        _record_schema(conn, table, 1, history_columns)
        versions = schema_versions(conn, table)
    if "_schema_version" not in [
        name for name, _ in table_columns_and_types(conn, history_table)
    ]:
        conn.execute(
            "ALTER TABLE [{}] ADD COLUMN _schema_version INTEGER".format(history_table)
        )
    # Rows written before this point belong to the registered version whose
    # layout matches the current history table columns - if no version
    # matches, register the current layout as a new version so every row
    # ends up with exactly one confirmable column interpretation
    history_column_names = [name for name, _ in history_columns]
    matching = [
        version
        for version, layout in versions.items()
        if [name for name, _ in layout] == history_column_names
    ]
    if matching:
        stamp = max(matching)
    else:
        stamp = max(versions) + 1
        _record_schema(conn, table, stamp, history_columns)
        versions = schema_versions(conn, table)
    conn.execute(
        "UPDATE [{}] SET _schema_version = ? WHERE _schema_version IS NULL".format(
            history_table
        ),
        [stamp],
    )
    latest_version = max(versions)
    latest_layout = [name for name, _ in versions[latest_version]]
    if latest_layout != columns:
        # The tracked table's schema changed. Add any new columns to the
        # history table (old columns are kept so existing rows keep their
        # original facts - values are never fabricated for columns that did
        # not exist when a row was recorded) and register a new version.
        new_version = latest_version + 1
        for name, type_ in columns_and_types:
            if name not in history_column_names:
                conn.execute(
                    "ALTER TABLE [{}] ADD COLUMN {} {}".format(
                        history_table, escape_sqlite(name), type_
                    )
                )
        _record_schema(conn, table, new_version, columns_and_types)
        latest_version = new_version
        trigger_columns = columns
    else:
        trigger_columns = latest_layout
    # Rebuild the triggers against the latest column layout
    for statement in drop_trigger_statements(table):
        conn.execute(statement)
    for statement in trigger_statements(
        table, trigger_columns, schema_version=latest_version
    ):
        conn.execute(statement)


def _table_exists(conn, name):
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", [name]
        ).fetchone()
        is not None
    )


def _record_schema(conn, table, version, columns_and_types):
    for position, (name, type_) in enumerate(columns_and_types):
        conn.execute(
            "INSERT OR REPLACE INTO _{table}_history_schema "
            "(_schema_version, _position, name, type) VALUES (?, ?, ?, ?)".format(
                table=table
            ),
            [version, position, name, type_],
        )
