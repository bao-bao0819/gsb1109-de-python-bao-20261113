from .sql import escape_sqlite, table_columns_and_types


class _Unknown:
    """Sentinel for column values that have no recorded history."""

    def __repr__(self):
        return "UNKNOWN"

    def __bool__(self):
        return False


UNKNOWN = _Unknown()


def _lifecycle_events(db, table, rowid):
    """History events for rowid in its current lifecycle, oldest first.

    A delete (_mask = -1) ends a lifecycle: if the same rowid is later
    re-inserted it belongs to a new row, and events from before the delete
    must not be connected to it.
    """
    history_table = escape_sqlite("_{}_history".format(table))
    cursor = db.execute(
        "SELECT * FROM {} WHERE _rowid = ? ORDER BY rowid".format(history_table),
        (rowid,),
    )
    names = [d[0] for d in cursor.description]
    events = [dict(zip(names, row)) for row in cursor.fetchall()]
    if not events:
        raise ValueError(
            "No history recorded for rowid {!r} in table {!r}".format(rowid, table)
        )
    last_delete = None
    for idx, event in enumerate(events):
        if event["_mask"] == -1:
            last_delete = idx
    if last_delete is not None:
        events = events[last_delete + 1 :]
    return events


def row_state(db, table, rowid, version=None):
    """Reconstruct the state of a row at a version in its current lifecycle.

    Returns a dictionary with:

    - ``version``: the version number of the reconstructed state
    - ``updated``: the timestamp (ms since Unix epoch) of that version
    - ``changed``: the columns that version touched, mapped to their new
      values - a column mapping to ``None`` here was explicitly cleared,
      a column absent from here was not touched by that version
    - ``row``: the full reconstructed row. Columns with no recorded value
      in the current lifecycle are ``UNKNOWN`` (earlier values were never
      recorded), which is distinct from ``None`` (recorded as cleared).

    Returns ``None`` if the row's most recent event is a delete. Raises
    ``ValueError`` if there is no history for the rowid, or if ``version``
    is not part of the current lifecycle (for example it belongs to a
    previously deleted row that used the same rowid).
    """
    columns = [name for name, _ in table_columns_and_types(db, table)]
    events = _lifecycle_events(db, table, rowid)
    if not events:
        if version is None:
            return None
        raise ValueError(
            "Version {!r} is not part of the current lifecycle of rowid "
            "{!r} in table {!r}".format(version, rowid, table)
        )
    if version is not None:
        cut = None
        for idx, event in enumerate(events):
            if event["_version"] == version:
                cut = idx
        if cut is None:
            raise ValueError(
                "Version {!r} is not part of the current lifecycle of rowid "
                "{!r} in table {!r}".format(version, rowid, table)
            )
        events = events[: cut + 1]
    state = {column: UNKNOWN for column in columns}
    for event in events:
        mask = event["_mask"]
        for idx, column in enumerate(columns):
            # Columns added to the table after history was configured are
            # absent from the history table - their values were never
            # recorded, so they stay UNKNOWN.
            if column in event and mask & (1 << idx):
                state[column] = event[column]
    last = events[-1]
    changed = {
        column: last[column]
        for idx, column in enumerate(columns)
        if column in last and last["_mask"] & (1 << idx)
    }
    return {
        "version": last["_version"],
        "updated": last["_updated"],
        "changed": changed,
        "row": state,
    }
