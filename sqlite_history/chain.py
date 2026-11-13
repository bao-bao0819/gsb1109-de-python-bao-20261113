from dataclasses import dataclass, field
from typing import Any, List, Optional

from .sql import escape_sqlite

DELETE_MASK = -1
META_COLUMNS = ("_rowid", "_version", "_updated", "_mask")


@dataclass
class ChainEvent:
    """One history version that supports a traced column value."""

    version: int
    value: Any
    changed: bool
    mask: int
    updated: Optional[int]
    deleted: bool = False


@dataclass
class ColumnValueChain:
    """Ordered events supporting a column value at a given row version."""

    table: str
    rowid: int
    column: str
    version: int
    complete: bool
    events: List[ChainEvent] = field(default_factory=list)

    @property
    def origin(self) -> Optional[ChainEvent]:
        """Event that established the queried value.

        The most recent event that explicitly wrote the column (or the
        deletion event). Everything between it and the queried version
        merely carried the value forward.
        """
        for event in reversed(self.events):
            if event.changed or event.deleted:
                return event
        return None


def _history_table_exists(db, history_table: str) -> bool:
    row = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (history_table,),
    ).fetchone()
    return row is not None


def _tracked_columns(db, history_table: str) -> List[str]:
    rows = db.execute(
        "PRAGMA table_info({})".format(escape_sqlite(history_table))
    ).fetchall()
    return [row[1] for row in rows if row[1] not in META_COLUMNS]


def column_value_chain(db, table: str, rowid: int, column: str, version: int):
    """Return the event chain supporting a column value for one row version.

    Walks the ``_<table>_history`` rows of *rowid* backwards from *version*,
    stopping at the first real gap (a missing version, e.g. a database that
    only keeps part of its history). Events that did not touch *column* are
    kept in the chain but flagged as carried forward, so every version from
    the chain's start up to *version* can be checked in order.

    Set ``ColumnValueChain.complete`` to ``False`` when the chain cannot
    reach version 1 because retained history starts later or has a hole.
    """
    history_table = "_{}_history".format(table)
    if not _history_table_exists(db, history_table):
        raise ValueError(
            "History table {} does not exist".format(history_table)
        )
    tracked_columns = _tracked_columns(db, history_table)
    if column not in tracked_columns:
        raise ValueError(
            "Column {} is not tracked by {}".format(column, history_table)
        )
    column_bit = 1 << tracked_columns.index(column)
    column_sql = escape_sqlite(column)

    rows = db.execute(
        """
        SELECT {column_sql}, _version, _updated, _mask
        FROM {history_table}
        WHERE _rowid = ? AND _version <= ?
        ORDER BY _version
        """.format(
            column_sql=column_sql,
            history_table=escape_sqlite(history_table),
        ),
        (rowid, version),
    ).fetchall()
    if not rows or rows[-1][1] != version:
        raise ValueError(
            "No history event for rowid {} at version {}".format(
                rowid, version
            )
        )

    reversed_events: List[ChainEvent] = []
    gap = False
    for index in range(len(rows) - 1, -1, -1):
        stored_value, event_version, updated, mask = rows[index]
        if index < len(rows) - 1:
            following_version = rows[index + 1][1]
            if event_version + 1 != following_version:
                gap = True
                break
        deleted = mask == DELETE_MASK
        changed = not deleted and bool(mask & column_bit)
        reversed_events.append(
            ChainEvent(
                version=event_version,
                value=stored_value if (changed or deleted) else None,
                changed=changed,
                mask=mask,
                updated=updated,
                deleted=deleted,
            )
        )

    events = list(reversed(reversed_events))
    if events and events[0].version != 1:
        gap = True

    first_explicit = next(
        (
            index
            for index, event in enumerate(events)
            if event.changed or event.deleted
        ),
        None,
    )
    if first_explicit is None:
        return ColumnValueChain(
            table=table,
            rowid=rowid,
            column=column,
            version=version,
            complete=False,
            events=[],
        )
    if first_explicit > 0:
        gap = True
    events = events[first_explicit:]

    current_value = None
    for event in events:
        if event.changed or event.deleted:
            current_value = event.value
        else:
            event.value = current_value

    return ColumnValueChain(
        table=table,
        rowid=rowid,
        column=column,
        version=version,
        complete=not gap,
        events=events,
    )
