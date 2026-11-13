"""Offline consistency checks for FTS4 external-content indexes.

The detail table is refreshed daily by source batch while a separate FTS4
external-content index ("content=<table>") is maintained alongside it.  When a
batch deletes a document and inserts a replacement reusing the same rowid
without re-indexing that rowid, the inverted index keeps the old document's
terms even though fetching the rowid returns the new title.

Everything here works from real inverted-index hits: each reported term is
confirmed by an actual ``MATCH`` against the FTS table and a matchinfo() hit
count, so repairs only ever touch rows for which evidence exists.
"""

import json
import re
import struct
from collections import Counter
from dataclasses import dataclass


class ConsistencyError(Exception):
    pass


@dataclass(frozen=True)
class ColumnMismatch:
    """Evidence for one (rowid, FTS column) divergence."""

    rowid: int
    column_index: int
    column_name: str
    # Full per-term hit counts currently found in the inverted index.
    indexed: Counter
    # Per-term hit counts implied by tokenizing the current content row.
    expected: Counter

    @property
    def stale(self):
        # Terms the inverted index still points at this rowid with, but which
        # the current content row no longer contains.
        return self.indexed - self.expected

    @property
    def missing(self):
        # Terms the current content row contains but the index does not know.
        return self.expected - self.indexed


@dataclass(frozen=True)
class AuditResult:
    spec: "TableSpec"
    mismatches: tuple
    # {rowid: {column_index: Counter(term -> hit count)}}
    indexed: dict
    expected: dict

    @property
    def affected_rowids(self):
        return sorted({m.rowid for m in self.mismatches})


@dataclass(frozen=True)
class TableSpec:
    fts_table: str
    content_table: str
    rowid_column: str
    columns: tuple
    tokenize_clause: str

    @property
    def column_names(self):
        return [name for name, _ in self.columns]


def _quote_ident(name):
    return '"' + name.replace('"', '""') + '"'


def _quote_term(term):
    return '"' + term.replace('"', '""') + '"'


def _extract_options(ddl):
    # Values may be bare identifiers, integers or single-quoted strings.
    options = {}
    for match in re.finditer(
        r"([A-Za-z_][A-Za-z0-9_]*)\s*=\s*('[^']*'|[A-Za-z0-9_]+)", ddl
    ):
        key, value = match.group(1), match.group(2)
        options[key.lower()] = value
    return options


def _extract_tokenize_clause(ddl):
    # Keep the full option (e.g. tokenize=unicode61 "remove_diacritics=0"),
    # since it may carry double-quoted tokenizer arguments.
    match = re.search(r"tokenize\s*=\s*([^,)]+)", ddl, re.IGNORECASE)
    if match is None:
        return ""
    return "tokenize={}".format(match.group(1).strip())


def _parse_spec(conn, fts_table, content_table=None, rowid_column=None):
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        [fts_table],
    ).fetchone()
    if row is None or row[0] is None:
        raise ConsistencyError(
            "not a table in the main schema: {}".format(fts_table)
        )
    ddl = row[0]
    if not re.search(r"\bfts4\s*\(", ddl, re.IGNORECASE):
        raise ConsistencyError("{} is not an FTS4 virtual table".format(fts_table))

    options = _extract_options(ddl)
    if content_table is None:
        if "content" not in options:
            raise ConsistencyError(
                "{} stores its own content; external-content drift cannot "
                "occur there".format(fts_table)
            )
        content_table = options["content"].strip("'")
    if content_table == "":
        raise ConsistencyError(
            "{} is contentless (content=''); its source text is unavailable "
            "for verification".format(fts_table)
        )

    if rowid_column is None:
        rowid_column = options.get("content_rowid", "rowid").strip("'")

    tokenize_clause = _extract_tokenize_clause(ddl)

    fts_columns = tuple(
        (info[1], info[0])
        for info in conn.execute(
            "PRAGMA table_info({})".format(_quote_ident(fts_table))
        )
    )
    if not fts_columns:
        raise ConsistencyError("FTS table {} has no columns".format(fts_table))
    content_columns = {
        info[1]
        for info in conn.execute(
            "PRAGMA table_info({})".format(_quote_ident(content_table))
        )
    }
    unknown = [name for name, _ in fts_columns if name not in content_columns]
    if unknown:
        raise ConsistencyError(
            "content table {} is missing FTS column(s): {}".format(
                content_table, ", ".join(unknown)
            )
        )
    return TableSpec(
        fts_table=fts_table,
        content_table=content_table,
        rowid_column=rowid_column,
        columns=fts_columns,
        tokenize_clause=tokenize_clause,
    )


def _matchinfo_hits(raw_matchinfo, column_count):
    # matchinfo format 'pcx': p, c, then 3 unsigned ints per phrase/column.
    values = struct.unpack("@" + "I" * (len(raw_matchinfo) // 4), raw_matchinfo)
    phrase_count, seen_columns = values[0], values[1]
    if seen_columns != column_count:
        raise ConsistencyError("matchinfo column count mismatch")
    hits = {}
    index = 2
    for _ in range(phrase_count):
        for column_index in range(column_count):
            hits_this_row = values[index]
            index += 3
            if hits_this_row:
                hits[column_index] = hits_this_row
    return hits


def _snapshot_from(aux_table, fts_table, column_count, conn):
    """Per-rowid, per-column term counts taken from real MATCH hits."""
    terms = [
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT term FROM {} WHERE col='*'".format(
                _quote_ident(aux_table)
            )
        )
    ]
    snapshot = {}
    for term in terms:
        query = (
            "SELECT rowid, matchinfo({t}, 'pcx') FROM {t} WHERE {t} MATCH ?"
        ).format(t=_quote_ident(fts_table))
        for rowid, raw in conn.execute(query, [_quote_term(term)]):
            for column_index, hits in _matchinfo_hits(raw, column_count).items():
                snapshot.setdefault(rowid, {}).setdefault(
                    column_index, Counter()
                )[term] = hits
    return snapshot


def _probe_expected(conn, spec, probe_table, probe_aux):
    # A probe FTS4 table using the same tokenizer tells us how the current
    # content text is actually tokenized.
    column_sql = ", ".join(_quote_ident(name) for name, _ in spec.columns)
    tokenize = ", {}".format(spec.tokenize_clause) if spec.tokenize_clause else ""
    conn.execute(
        "CREATE VIRTUAL TABLE {p} USING fts4({cols}{tok})".format(
            p=_quote_ident(probe_table), cols=column_sql, tok=tokenize
        )
    )
    conn.execute(
        "INSERT INTO {p}(rowid, {cols}) SELECT {rid}, {cols} FROM {ct}".format(
            p=_quote_ident(probe_table),
            cols=column_sql,
            rid=_quote_ident(spec.rowid_column),
            ct=_quote_ident(spec.content_table),
        )
    )
    conn.execute(
        "CREATE VIRTUAL TABLE {a} USING fts4aux({p})".format(
            a=_quote_ident(probe_aux), p=_quote_ident(probe_table)
        )
    )
    return _snapshot_from(probe_aux, probe_table, len(spec.columns), conn)


def audit(conn, fts_table, content_table=None, rowid_column=None):
    """Collect evidence for a stale external-content FTS4 index.

    The connection is used read-only apart from scratch virtual tables, which
    are dropped before returning.
    """
    spec = _parse_spec(conn, fts_table, content_table, rowid_column)
    # Scratch DML may open an implicit transaction on default-isolation
    # connections.  Remember whether the caller already had one open so we do
    # not either commit their pending writes or leave our scratch transaction
    # lying around (which would make repair believe it must not commit).
    caller_owned_transaction = conn.in_transaction
    probe_table = "_fts4_audit_probe_{}".format(fts_table)
    probe_aux = "_fts4_audit_probeaux_{}".format(fts_table)
    index_aux = "_fts4_audit_aux_{}".format(fts_table)
    for scratch in (index_aux, probe_aux, probe_table):
        conn.execute("DROP TABLE IF EXISTS {}".format(_quote_ident(scratch)))
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE {a} USING fts4aux({t})".format(
                a=_quote_ident(index_aux), t=_quote_ident(fts_table)
            )
        )
        indexed = _snapshot_from(
            index_aux, fts_table, len(spec.columns), conn
        )
        expected = _probe_expected(conn, spec, probe_table, probe_aux)
    finally:
        for scratch in (probe_aux, probe_table, index_aux):
            conn.execute("DROP TABLE IF EXISTS {}".format(_quote_ident(scratch)))
        if not caller_owned_transaction and conn.in_transaction:
            # Only our net-zero scratch work can be in this transaction.
            conn.commit()

    mismatches = []
    for rowid in sorted(set(indexed) | set(expected)):
        for column_index, (column_name, _) in enumerate(spec.columns):
            actual = indexed.get(rowid, {}).get(column_index, Counter())
            wanted = expected.get(rowid, {}).get(column_index, Counter())
            if actual != wanted:
                mismatches.append(
                    ColumnMismatch(
                        rowid=rowid,
                        column_index=column_index,
                        column_name=column_name,
                        indexed=actual,
                        expected=wanted,
                    )
                )
    return AuditResult(
        spec=spec,
        mismatches=tuple(mismatches),
        indexed=indexed,
        expected=expected,
    )


def _reconstructed_text(term_counts):
    # FTS4 deletion only counts token occurrences; order does not matter.
    return " ".join(
        term
        for term, count in sorted(term_counts.items())
        for _ in range(count)
    )


def repair(conn, result):
    """Re-index only the rowids backed by mismatch evidence.

    Modern SQLite builds reject the FTS4 'delete'/'insert' special commands,
    so the old postings are removed with a plain DELETE.  External-content
    FTS4 deletes re-read the content table at delete time, which is why the
    content row is temporarily restored to the stale indexed text first.
    The work is committed on success and rolled back on any failure.
    """
    spec = result.spec
    affected = result.affected_rowids
    if not affected:
        return []

    column_names = spec.column_names
    fts_quoted = _quote_ident(spec.fts_table)
    content_quoted = _quote_ident(spec.content_table)
    rowid_quoted = _quote_ident(spec.rowid_column)
    column_sql = ", ".join(_quote_ident(name) for name in column_names)
    placeholders = ", ".join("?" for _ in column_names)
    select_sql = "SELECT {cols} FROM {ct} WHERE {rid} = ?".format(
        cols=column_sql, ct=content_quoted, rid=rowid_quoted
    )

    repaired = []
    owns_transaction = not conn.in_transaction
    if owns_transaction:
        conn.execute("BEGIN")
    try:
        for rowid in affected:
            current = conn.execute(select_sql, [rowid]).fetchone()
            # Full indexed multiset for every column, including columns whose
            # terms happen to be unchanged, so shared terms are not double
            # counted when the row is re-indexed.
            stale_texts = [
                _reconstructed_text(
                    result.indexed.get(rowid, {}).get(index, Counter())
                )
                for index in range(len(column_names))
            ]
            if current is None:
                # Orphan posting: the content row itself is gone.  Recreate a
                # throwback row long enough to unwind the old postings.
                conn.execute(
                    "INSERT INTO {ct}({rid}, {cols}) VALUES(?, {ph})".format(
                        ct=content_quoted,
                        rid=rowid_quoted,
                        cols=column_sql,
                        ph=placeholders,
                    ),
                    [rowid] + stale_texts,
                )
                conn.execute(
                    "DELETE FROM {ft} WHERE rowid = ?".format(ft=fts_quoted),
                    [rowid],
                )
                conn.execute(
                    "DELETE FROM {ct} WHERE {rid} = ?".format(
                        ct=content_quoted, rid=rowid_quoted
                    ),
                    [rowid],
                )
            else:
                set_sql = ", ".join(
                    "{} = ?".format(_quote_ident(name)) for name in column_names
                )
                conn.execute(
                    "UPDATE {ct} SET {sets} WHERE {rid} = ?".format(
                        ct=content_quoted, sets=set_sql, rid=rowid_quoted
                    ),
                    stale_texts + [rowid],
                )
                conn.execute(
                    "DELETE FROM {ft} WHERE rowid = ?".format(ft=fts_quoted),
                    [rowid],
                )
                conn.execute(
                    "UPDATE {ct} SET {sets} WHERE {rid} = ?".format(
                        ct=content_quoted, sets=set_sql, rid=rowid_quoted
                    ),
                    list(current) + [rowid],
                )
                conn.execute(
                    "INSERT INTO {ft}(rowid, {cols}) VALUES(?, {ph})".format(
                        ft=fts_quoted, cols=column_sql, ph=placeholders
                    ),
                    [rowid] + list(current),
                )
            repaired.append(rowid)
        if owns_transaction:
            conn.commit()
    except Exception:
        if owns_transaction:
            conn.rollback()
        raise
    return repaired


def format_report(result):
    spec = result.spec
    lines = [
        "FTS table: {}".format(spec.fts_table),
        "content table: {} (rowid: {})".format(
            spec.content_table, spec.rowid_column
        ),
        "affected rowids: {}".format(result.affected_rowids or "none"),
    ]
    for mismatch in result.mismatches:
        lines.append(
            "rowid {rid} column {col} ({name}):".format(
                rid=mismatch.rowid,
                col=mismatch.column_index,
                name=mismatch.column_name,
            )
        )
        if mismatch.stale:
            lines.append(
                "  stale terms (indexed but absent from content): {}".format(
                    dict(mismatch.stale)
                )
            )
        if mismatch.missing:
            lines.append(
                "  missing terms (content has them, index does not): {}".format(
                    dict(mismatch.missing)
                )
            )
    return "\n".join(lines)


def result_to_json(result):
    return json.dumps(
        {
            "fts_table": result.spec.fts_table,
            "content_table": result.spec.content_table,
            "rowid_column": result.spec.rowid_column,
            "affected_rowids": result.affected_rowids,
            "mismatches": [
                {
                    "rowid": m.rowid,
                    "column_index": m.column_index,
                    "column_name": m.column_name,
                    "stale": dict(m.stale),
                    "missing": dict(m.missing),
                }
                for m in result.mismatches
            ],
        },
        indent=2,
        sort_keys=True,
    )
