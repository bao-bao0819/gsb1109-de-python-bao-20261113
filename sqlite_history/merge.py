"""Three-way merge of two sqlite-history tracked databases.

Scenario: a branch office checks out a snapshot of a table from HQ,
works offline for a while (both sides recording history via triggers),
then comes back to reconcile.

The checkout receipt records, per table and rowid, the history version at
checkout time. That shared starting point lets us fold each side's history
chain into a per-field state and perform a field-level three-way merge:

- Fields changed on only one side merge cleanly.
- A field changed to the same value on both sides keeps one business value
  but records two provenance sources.
- A delete on one side versus new edits on the other is a conflict that is
  resolved deterministically (the delete wins) - never by merge order.
- A side that only brings back a partial version chain is flagged as
  incomplete evidence; its business values are not applied and no history
  is fabricated from current values.
- Re-merging the same receipt is idempotent: no new business updates and
  no duplicate provenance rows are generated.
"""

import time

from .sql import escape_sqlite, table_columns_and_types

RECEIPTS_TABLE = "_checkout_receipts"
MERGE_LOG_TABLE = "_merge_log"
SOURCES_TABLE = "_merge_sources"
CONFLICTS_TABLE = "_merge_conflicts"
INCOMPLETE_TABLE = "_merge_incomplete"

DELETE_CONFLICT_COLUMN = "__delete_vs_update__"

_DDL = [
    """
CREATE TABLE IF NOT EXISTS _checkout_receipts (
    receipt_id INTEGER NOT NULL,
    table_name TEXT NOT NULL,
    _rowid INTEGER NOT NULL,
    _version INTEGER NOT NULL,
    _created INTEGER NOT NULL,
    PRIMARY KEY (receipt_id, table_name, _rowid)
)
""",
    """
CREATE TABLE IF NOT EXISTS _merge_log (
    merge_id INTEGER PRIMARY KEY,
    receipt_id INTEGER NOT NULL,
    merged_at INTEGER NOT NULL
)
""",
    """
CREATE TABLE IF NOT EXISTS _merge_sources (
    receipt_id INTEGER NOT NULL,
    table_name TEXT NOT NULL,
    _rowid INTEGER NOT NULL,
    column_name TEXT NOT NULL,
    source TEXT NOT NULL,
    value,
    merge_id INTEGER NOT NULL,
    PRIMARY KEY (receipt_id, table_name, _rowid, column_name, source)
)
""",
    """
CREATE TABLE IF NOT EXISTS _merge_conflicts (
    receipt_id INTEGER NOT NULL,
    table_name TEXT NOT NULL,
    _rowid INTEGER NOT NULL,
    column_name TEXT NOT NULL,
    hq_value,
    branch_value,
    resolved_value,
    resolution TEXT NOT NULL,
    merge_id INTEGER NOT NULL,
    PRIMARY KEY (receipt_id, table_name, _rowid, column_name)
)
""",
    """
CREATE TABLE IF NOT EXISTS _merge_incomplete (
    receipt_id INTEGER NOT NULL,
    table_name TEXT NOT NULL,
    _rowid INTEGER NOT NULL,
    side TEXT NOT NULL,
    detail TEXT NOT NULL,
    merge_id INTEGER NOT NULL,
    PRIMARY KEY (receipt_id, table_name, _rowid, side)
)
""",
]


class MergeConflictError(Exception):
    """Raised when on_conflict="error" and a real conflict is found."""


class MergeReport:
    def __init__(self, merge_id, receipt_id):
        self.merge_id = merge_id
        self.receipt_id = receipt_id
        self.applied = {"insert": 0, "update": 0, "delete": 0, "none": 0}
        self.conflicts = []
        self.incomplete = []

    def __repr__(self):
        return (
            f"<MergeReport merge_id={self.merge_id} receipt_id={self.receipt_id} "
            f"applied={self.applied} conflicts={len(self.conflicts)} "
            f"incomplete={len(self.incomplete)}>"
        )


def _now_ms():
    return int(time.time() * 1000)


def _ensure_merge_tables(db):
    for statement in _DDL:
        db.execute(statement)


def history_tables(db):
    """Names of base tables that have a corresponding _<table>_history table."""
    names = [
        row[0]
        for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    ]
    tables = set(names)
    result = []
    for name in names:
        if name.startswith("_") and name.endswith("_history"):
            base = name[1 : -len("_history")]
            if base in tables:
                result.append(base)
    return sorted(result)


def create_receipt(db, tables=None):
    """Record the current history versions as a checkout receipt.

    Returns the new receipt_id. The receipt is stored inside the database
    itself, so copying the database file carries the receipt along.
    """
    if tables is None:
        tables = history_tables(db)
    _ensure_merge_tables(db)
    receipt_id = db.execute(
        "SELECT COALESCE(MAX(receipt_id), 0) + 1 FROM _checkout_receipts"
    ).fetchone()[0]
    now = _now_ms()
    for table in tables:
        history_table = escape_sqlite(f"_{table}_history")
        for rowid, version in db.execute(
            f"SELECT _rowid, MAX(_version) FROM {history_table} GROUP BY _rowid"
        ).fetchall():
            db.execute(
                "INSERT INTO _checkout_receipts "
                "(receipt_id, table_name, _rowid, _version, _created) "
                "VALUES (?, ?, ?, ?, ?)",
                [receipt_id, table, rowid, version, now],
            )
    db.commit()
    return receipt_id


def load_receipt(db, receipt_id):
    """Load a receipt as {table_name: {rowid: version}}."""
    receipt = {}
    for table, rowid, version in db.execute(
        "SELECT table_name, _rowid, _version FROM _checkout_receipts "
        "WHERE receipt_id = ?",
        [receipt_id],
    ).fetchall():
        receipt.setdefault(table, {})[rowid] = version
    return receipt


def _load_history(db, table, columns):
    """Return {rowid: {version: {"_mask": mask, column: value, ...}}}."""
    column_names = ", ".join(escape_sqlite(c) for c in columns)
    history_table = escape_sqlite(f"_{table}_history")
    cursor = db.execute(
        f"SELECT _rowid, _version, _mask, {column_names} FROM {history_table}"
    )
    history = {}
    for row in cursor.fetchall():
        rowid, version, mask = row[0], row[1], row[2]
        entry = {"_mask": mask}
        for index, column in enumerate(columns):
            entry[column] = row[3 + index]
        history.setdefault(rowid, {})[version] = entry
    return history


def _fold(versioned_rows, columns, upto=None):
    """Fold history rows (version -> entry) into (exists, state).

    If upto is given, only rows with version <= upto are folded.
    """
    exists = False
    state = dict.fromkeys(columns)
    for version in sorted(versioned_rows):
        if upto is not None and version > upto:
            continue
        entry = versioned_rows[version]
        mask = entry["_mask"]
        if mask == -1:
            exists = False
            state = dict.fromkeys(columns)
        else:
            exists = True
            for index, column in enumerate(columns):
                if mask & (1 << index):
                    state[column] = entry[column]
    return exists, state


def _chain_gap(versions, base_version, require_from_one):
    """Return a description of the first gap in the version chain, or None."""
    versions = sorted(versions)
    if not versions:
        if base_version > 0:
            return f"no history rows at all, receipt expects version {base_version}"
        return None
    if require_from_one:
        expected = list(range(1, versions[-1] + 1))
        if versions != expected:
            return f"history chain not contiguous from 1: found {versions}"
        return None
    if base_version > 0 and base_version not in versions:
        return f"missing receipt-point version {base_version}"
    chain = [v for v in versions if v > base_version]
    expected = list(range(base_version + 1, base_version + 1 + len(chain)))
    if chain != expected:
        return (
            f"gap above receipt point {base_version}: "
            f"expected {expected}, found {chain}"
        )
    return None


def _fetch_row(db, table, columns, rowid):
    column_names = ", ".join(escape_sqlite(c) for c in columns)
    cursor = db.execute(
        f"SELECT {column_names} FROM {escape_sqlite(table)} WHERE rowid = ?",
        [rowid],
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return dict(zip(columns, row))


def _rowid_alias_column(db, table):
    """Name of the INTEGER PRIMARY KEY column that aliases rowid, if any."""
    for row in db.execute(f"PRAGMA table_info({escape_sqlite(table)})").fetchall():
        # (cid, name, type, notnull, dflt_value, pk)
        if row[5] and row[2].upper() == "INTEGER":
            return row[1]
    return None


def _apply_merged(db, table, columns, rowid, merged_exists, merged_state):
    """Apply the merged state to the HQ business table.

    Returns one of "insert", "update", "delete", "none". No-op when the
    business table already matches, which is what makes re-merging the
    same receipt idempotent.
    """
    current = _fetch_row(db, table, columns, rowid)
    if not merged_exists:
        if current is not None:
            db.execute(
                f"DELETE FROM {escape_sqlite(table)} WHERE rowid = ?", [rowid]
            )
            return "delete"
        return "none"
    if current is None:
        column_names = ", ".join(escape_sqlite(c) for c in columns)
        placeholders = ", ".join("?" for _ in range(len(columns) + 1))
        if _rowid_alias_column(db, table) is not None:
            # The INTEGER PRIMARY KEY column already aliases rowid - the
            # merged state carries the right value, no explicit rowid needed.
            db.execute(
                f"INSERT INTO {escape_sqlite(table)} ({column_names}) "
                f"VALUES ({', '.join('?' for _ in columns)})",
                [merged_state[c] for c in columns],
            )
        else:
            db.execute(
                f"INSERT INTO {escape_sqlite(table)} (rowid, {column_names}) "
                f"VALUES ({placeholders})",
                [rowid] + [merged_state[c] for c in columns],
            )
        return "insert"
    changed = [c for c in columns if current[c] != merged_state[c]]
    if not changed:
        return "none"
    assignments = ", ".join(f"{escape_sqlite(c)} = ?" for c in changed)
    db.execute(
        f"UPDATE {escape_sqlite(table)} SET {assignments} WHERE rowid = ?",
        [merged_state[c] for c in changed] + [rowid],
    )
    return "update"


def _merge_row(columns, base_exists, base_state, hq_exists, hq_state, br_exists, br_state, on_conflict, conflict_sink):
    """Three-way merge of one row. Returns (merged_exists, merged_state, sources).

    sources maps column name -> set of contributing sides ("hq" / "branch").
    conflict_sink is a callable (column, hq_value, branch_value, resolved, resolution).
    """
    hq_deleted = base_exists and not hq_exists
    br_deleted = base_exists and not br_exists

    if hq_deleted or br_deleted:
        other_changed = False
        if hq_deleted and br_exists:
            other_changed = (
                not base_exists
                or any(br_state[c] != base_state[c] for c in columns)
            )
        elif br_deleted and hq_exists:
            other_changed = (
                not base_exists
                or any(hq_state[c] != base_state[c] for c in columns)
            )
        if hq_deleted and br_deleted:
            pass
        elif other_changed:
            # Delete on one side, fresh edits on the other: the delete wins
            # deterministically, regardless of the order sides are examined.
            conflict_sink(
                DELETE_CONFLICT_COLUMN,
                "<deleted>" if hq_deleted else "<updated>",
                "<deleted>" if br_deleted else "<updated>",
                "<deleted>",
                "delete-wins",
            )
        return False, dict.fromkeys(columns), {}

    merged_state = {}
    sources = {}
    for column in columns:
        # A column counts as changed on a side when that side's folded value
        # differs from the checkout baseline. When the row did not exist at
        # checkout the baseline is all-None, so any inserted value counts.
        hq_changed = hq_exists and hq_state[column] != base_state[column]
        br_changed = br_exists and br_state[column] != base_state[column]
        if hq_changed and br_changed:
            if hq_state[column] == br_state[column]:
                merged_state[column] = hq_state[column]
                sources[column] = {"hq", "branch"}
            else:
                if on_conflict == "error":
                    raise MergeConflictError(
                        f"Conflicting values for column {column!r}: "
                        f"hq={hq_state[column]!r} branch={br_state[column]!r}"
                    )
                winner = on_conflict  # "hq" or "branch"
                resolved = hq_state[column] if winner == "hq" else br_state[column]
                conflict_sink(
                    column, hq_state[column], br_state[column], resolved, f"{winner}-wins"
                )
                merged_state[column] = resolved
                sources[column] = {winner}
        elif hq_changed:
            merged_state[column] = hq_state[column]
            sources[column] = {"hq"}
        elif br_changed:
            merged_state[column] = br_state[column]
            sources[column] = {"branch"}
        else:
            merged_state[column] = base_state[column] if base_exists else None
    return True, merged_state, sources


def merge_databases(hq_db, branch_db, receipt_id, tables=None, on_conflict="hq"):
    """Merge a branch database back into HQ using a shared checkout receipt.

    hq_db and branch_db are database connections (sqlite3.Connection or
    sqlite_utils.Database). The merge is applied to hq_db. Returns a
    MergeReport.
    """
    if on_conflict not in ("hq", "branch", "error"):
        raise ValueError(f"Invalid on_conflict value: {on_conflict!r}")
    receipt = load_receipt(hq_db, receipt_id)
    if not receipt:
        raise ValueError(f"Receipt {receipt_id} not found in HQ database")
    branch_receipt = load_receipt(branch_db, receipt_id)
    if branch_receipt != receipt:
        raise ValueError(
            "Branch database does not hold a matching checkout receipt - "
            "the shared starting point is missing"
        )
    if tables is None:
        tables = sorted(set(history_tables(hq_db)) | set(receipt.keys()))

    _ensure_merge_tables(hq_db)
    hq_db.commit()
    hq_db.execute("BEGIN")
    try:
        merge_id = hq_db.execute(
            "INSERT INTO _merge_log (receipt_id, merged_at) VALUES (?, ?)",
            [receipt_id, _now_ms()],
        ).lastrowid
        report = MergeReport(merge_id, receipt_id)

        for table in tables:
            columns = [c for c, _ in table_columns_and_types(hq_db, table)]
            hq_history = _load_history(hq_db, table, columns)
            br_history = _load_history(branch_db, table, columns)
            receipt_rows = receipt.get(table, {})
            rowids = sorted(
                set(receipt_rows) | set(hq_history) | set(br_history)
            )
            for rowid in rowids:
                base_version = receipt_rows.get(rowid, 0)
                hq_versions = hq_history.get(rowid, {})
                br_versions = br_history.get(rowid, {})

                # Evidence completeness: HQ must hold the full chain from 1,
                # the branch must hold the receipt point and a contiguous
                # chain above it. Partial chains are flagged, not merged.
                hq_db.execute(
                    "DELETE FROM _merge_incomplete WHERE receipt_id = ? "
                    "AND table_name = ? AND _rowid = ?",
                    [receipt_id, table, rowid],
                )
                gaps = []
                if hq_versions or base_version:
                    gap = _chain_gap(hq_versions, base_version, require_from_one=True)
                    if gap:
                        gaps.append(("hq", gap))
                if br_versions or base_version:
                    gap = _chain_gap(br_versions, base_version, require_from_one=False)
                    if gap:
                        gaps.append(("branch", gap))
                if gaps:
                    for side, detail in gaps:
                        hq_db.execute(
                            "INSERT OR REPLACE INTO _merge_incomplete "
                            "(receipt_id, table_name, _rowid, side, detail, merge_id) "
                            "VALUES (?, ?, ?, ?, ?, ?)",
                            [receipt_id, table, rowid, side, detail, merge_id],
                        )
                        report.incomplete.append(
                            {"table": table, "rowid": rowid, "side": side, "detail": detail}
                        )
                    continue

                base_exists, base_state = _fold(hq_versions, columns, upto=base_version)
                hq_exists, hq_state = _fold(hq_versions, columns)
                br_exists, br_state = _fold(br_versions, columns)

                # Changes HQ previously absorbed from this receipt's branch
                # must not be re-attributed to HQ on a re-merge: if HQ's
                # current value matches a recorded branch source value, treat
                # the field as unchanged on the HQ side.
                prior_branch_values = {
                    row[0]: row[1]
                    for row in hq_db.execute(
                        "SELECT column_name, value FROM _merge_sources "
                        "WHERE receipt_id = ? AND table_name = ? AND _rowid = ? "
                        "AND source = 'branch'",
                        [receipt_id, table, rowid],
                    ).fetchall()
                }
                hq_compare_state = dict(hq_state)
                for column, prior_value in prior_branch_values.items():
                    if (
                        column in hq_compare_state
                        and hq_compare_state[column] == prior_value
                        and hq_compare_state[column] != base_state[column]
                    ):
                        hq_compare_state[column] = base_state[column]

                conflicts_here = []

                def conflict_sink(column, hq_value, branch_value, resolved, resolution):
                    conflicts_here.append(
                        (column, hq_value, branch_value, resolved, resolution)
                    )

                merged_exists, merged_state, sources = _merge_row(
                    columns,
                    base_exists,
                    base_state,
                    hq_exists,
                    hq_compare_state,
                    br_exists,
                    br_state,
                    on_conflict,
                    conflict_sink,
                )

                action = _apply_merged(
                    hq_db, table, columns, rowid, merged_exists, merged_state
                )
                report.applied[action] += 1

                for column, sides in sources.items():
                    for side in sorted(sides):
                        hq_db.execute(
                            "INSERT OR REPLACE INTO _merge_sources "
                            "(receipt_id, table_name, _rowid, column_name, source, "
                            "value, merge_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                            [
                                receipt_id,
                                table,
                                rowid,
                                column,
                                side,
                                merged_state[column],
                                merge_id,
                            ],
                        )
                for column, hq_value, branch_value, resolved, resolution in conflicts_here:
                    hq_db.execute(
                        "INSERT OR REPLACE INTO _merge_conflicts "
                        "(receipt_id, table_name, _rowid, column_name, hq_value, "
                        "branch_value, resolved_value, resolution, merge_id) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        [
                            receipt_id,
                            table,
                            rowid,
                            column,
                            hq_value,
                            branch_value,
                            resolved,
                            resolution,
                            merge_id,
                        ],
                    )
                    report.conflicts.append(
                        {
                            "table": table,
                            "rowid": rowid,
                            "column": column,
                            "hq_value": hq_value,
                            "branch_value": branch_value,
                            "resolved_value": resolved,
                            "resolution": resolution,
                        }
                    )
        hq_db.commit()
    except Exception:
        hq_db.rollback()
        raise
    return report
