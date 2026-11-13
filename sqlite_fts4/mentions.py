"""
Count distinct-document mentions of business concepts in an FTS4 index.

A concept (e.g. a product generation such as "甲二代") may have several
aliases, each only valid during a given time window.  The dictionary can also
state containment between concepts ("甲二代" contains "甲"): when a short and
a long term both match the same text span, only the containing concept is
credited.

Two levels of retained data are supported:

* :func:`count_from_fts` needs row-level access to the FTS4 table.  It returns
  exact distinct-document counts (a hit in both the title and the body of the
  same document counts once) and a first mention whose source can be verified
  down to column, offset and snippet.

* :func:`bounds_from_aggregates` is used when only aggregate statistics
  survived.  Overlap between aliases, title/body duplicates and containment
  can no longer be resolved, so it returns provable lower/upper bounds rather
  than a falsely precise number.
"""

from __future__ import annotations

import datetime as _datetime
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Any


# ---------------------------------------------------------------------------
# Dictionary
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Alias:
    """One surface form of a concept and the window in which it is valid."""

    text: str
    valid_from: Optional[_datetime.date] = None
    valid_to: Optional[_datetime.date] = None

    def valid_on(self, day: _datetime.date) -> bool:
        if self.valid_from is not None and day < self.valid_from:
            return False
        if self.valid_to is not None and day > self.valid_to:
            return False
        return True


@dataclass
class DictionaryEntry:
    canonical: str
    aliases: List[Alias] = field(default_factory=list)
    # Canonical names of concepts whose mentions fully contain this concept's
    # mentions; stored on the entry of the *sub* concept.
    contained_by: List[str] = field(default_factory=list)


class Dictionary:
    """Validated concept dictionary built from plain dict descriptions."""

    def __init__(self, entries: Sequence[DictionaryEntry]):
        self.entries: Dict[str, DictionaryEntry] = {}
        self._alias_owner: Dict[str, str] = {}
        for entry in entries:
            if not entry.aliases:
                raise ValueError(
                    "concept {!r} needs at least one alias".format(entry.canonical)
                )
            if entry.canonical in self.entries:
                raise ValueError(
                    "duplicate concept {!r}".format(entry.canonical)
                )
            for alias in entry.aliases:
                owner = self._alias_owner.get(alias.text)
                if owner is not None:
                    raise ValueError(
                        "alias {!r} is claimed by both {!r} and {!r}".format(
                            alias.text, owner, entry.canonical
                        )
                    )
                self._alias_owner[alias.text] = entry.canonical
            self.entries[entry.canonical] = entry
        for entry in entries:
            for super_name in entry.contained_by:
                if super_name not in self.entries:
                    raise ValueError(
                        "concept {!r} is contained by unknown concept {!r}".format(
                            entry.canonical, super_name
                        )
                    )
                if super_name == entry.canonical:
                    raise ValueError(
                        "concept {!r} cannot contain itself".format(entry.canonical)
                    )
        self._check_cycles()

    @classmethod
    def from_dicts(cls, raw: Iterable[Mapping[str, Any]]) -> "Dictionary":
        entries: List[DictionaryEntry] = []
        for item in raw:
            aliases = []
            for alias in item.get("aliases", []):
                if isinstance(alias, str):
                    aliases.append(Alias(text=alias))
                else:
                    aliases.append(
                        Alias(
                            text=alias["text"],
                            valid_from=_as_date(alias.get("valid_from")),
                            valid_to=_as_date(alias.get("valid_to")),
                        )
                    )
            entries.append(
                DictionaryEntry(
                    canonical=item["canonical"],
                    aliases=aliases,
                    contained_by=list(item.get("contained_by", [])),
                )
            )
        return cls(entries)

    def _check_cycles(self) -> None:
        visiting: set = set()
        done: set = set()

        def walk(name: str) -> None:
            if name in visiting:
                raise ValueError(
                    "containment cycle involving concept {!r}".format(name)
                )
            if name in done:
                return
            visiting.add(name)
            for super_name in self.entries[name].contained_by:
                walk(super_name)
            visiting.discard(name)
            done.add(name)

        for name in self.entries:
            walk(name)

    @property
    def alias_texts(self) -> List[str]:
        return list(self._alias_owner)

    def owner_of(self, alias_text: str) -> Optional[str]:
        return self._alias_owner.get(alias_text)

    def active_aliases(
        self, concept: str, day: _datetime.date
    ) -> List[Alias]:
        return [
            alias
            for alias in self.entries[concept].aliases
            if alias.valid_on(day)
        ]

    def super_closure(self) -> Dict[str, set]:
        """Every concept that (transitively) contains each concept."""
        closure: Dict[str, set] = {}
        for name in self.entries:
            seen: set = set()
            stack = list(self.entries[name].contained_by)
            while stack:
                current = stack.pop()
                if current in seen:
                    continue
                seen.add(current)
                stack.extend(self.entries[current].contained_by)
            closure[name] = seen
        return closure


def _as_date(value: Any) -> Optional[_datetime.date]:
    if value is None or isinstance(value, _datetime.date):
        return value
    return _datetime.date.fromisoformat(str(value))


# ---------------------------------------------------------------------------
# Result objects
# ---------------------------------------------------------------------------


@dataclass
class DocMeta:
    rowid: int
    date: Optional[_datetime.date]
    doc_id: Optional[str] = None


@dataclass(frozen=True)
class Hit:
    rowid: int
    column: str
    column_index: int
    byte_offset: int
    byte_length: int
    alias: str


@dataclass
class FirstMention:
    doc_id: Optional[str]
    rowid: int
    date: Optional[_datetime.date]
    column: str
    alias: str
    char_offset: int
    snippet: str


@dataclass
class Suppression:
    rowid: int
    doc_id: Optional[str]
    sub_concept: str
    super_concept: str
    alias: str


@dataclass
class ConceptReport:
    concept: str
    exact: Optional[int]
    lower_bound: int
    upper_bound: int
    exact_available: bool
    first_mention: Optional[FirstMention] = None
    doc_ids: List[Any] = field(default_factory=list)
    suppressed: List[Suppression] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# FTS4 helpers
# ---------------------------------------------------------------------------


def _quote_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _phrase_query(term: str) -> str:
    return '"' + term.replace('"', '""') + '"'


def _column_names(conn, fts_table: str) -> List[str]:
    table = _quote_ident(fts_table)
    return [
        row[1]
        for row in conn.execute("PRAGMA table_info({})".format(table)).fetchall()
    ]


def _parse_offsets(
    raw: str, columns: Sequence[str], rowid: int, alias: str
) -> List[Hit]:
    values = [int(part) for part in raw.split()]
    hits: List[Hit] = []
    for i in range(0, len(values), 4):
        column_index, _term_index, byte_offset, byte_length = values[i : i + 4]
        hits.append(
            Hit(
                rowid=rowid,
                column=columns[column_index],
                column_index=column_index,
                byte_offset=byte_offset,
                byte_length=byte_length,
                alias=alias,
            )
        )
    return hits


def _byte_to_char(text: str, byte_offset: int) -> int:
    return len(text.encode("utf-8")[:byte_offset].decode("utf-8"))


def _make_snippet(text: str, alias: str, byte_offset: int) -> Tuple[str, int]:
    char_offset = _byte_to_char(text, byte_offset)
    start = max(0, char_offset - 20)
    end = min(len(text), char_offset + len(alias) + 20)
    prefix = "…" if start else ""
    suffix = "…" if end < len(text) else ""
    snippet = prefix + text[start:end] + suffix
    return snippet, char_offset


def _contains(outer: Hit, inner: Hit) -> bool:
    return (
        outer.column_index == inner.column_index
        and outer.byte_offset <= inner.byte_offset
        and outer.byte_offset + outer.byte_length
        >= inner.byte_offset + inner.byte_length
    )


def fetch_hits(
    conn,
    fts_table: str,
    terms: Sequence[str],
    columns: Optional[Sequence[str]] = None,
) -> Tuple[Dict[str, List[Hit]], List[str]]:
    """Run one phrase MATCH per term and parse FTS4 offsets().

    Returns the hits keyed by term and the list of terms that occur in the
    raw text but cannot be found through the FTS index (typically CJK that the
    active tokenizer never split into searchable tokens).
    """

    all_columns = _column_names(conn, fts_table)
    if columns is None:
        columns = all_columns
    allowed = {all_columns.index(name) for name in columns}
    table = _quote_ident(fts_table)
    hits_by_term: Dict[str, List[Hit]] = {}
    unsearchable: List[str] = []
    like_columns = " || ".join(_quote_ident(name) for name in all_columns)
    for term in terms:
        rows = conn.execute(
            "SELECT rowid, offsets({tbl}) FROM {tbl} WHERE {tbl} MATCH ?".format(
                tbl=table
            ),
            [_phrase_query(term)],
        ).fetchall()
        hits = [
            hit
            for rowid, raw in rows
            for hit in _parse_offsets(raw, all_columns, rowid, term)
            if hit.column_index in allowed
        ]
        hits_by_term[term] = hits
        if not hits:
            found_raw = conn.execute(
                "SELECT 1 FROM {tbl} WHERE {cols} LIKE ? LIMIT 1".format(
                    tbl=table, cols=like_columns
                ),
                ["%" + term + "%"],
            ).fetchone()
            if found_raw:
                unsearchable.append(term)
    return hits_by_term, unsearchable


def load_doc_meta(
    conn,
    table: str,
    date_column: str,
    id_column: Optional[str] = None,
    where: str = "",
    params: Sequence[Any] = (),
) -> List[DocMeta]:
    """Load (rowid, date[, doc_id]) metadata from a regular SQL table."""

    quoted_table = _quote_ident(table)
    quoted_date = _quote_ident(date_column)
    if id_column is None:
        sql = "SELECT rowid, {d} FROM {t}".format(d=quoted_date, t=quoted_table)
        id_index = None
    else:
        quoted_id = _quote_ident(id_column)
        sql = "SELECT rowid, {d}, {i} FROM {t}".format(
            d=quoted_date, i=quoted_id, t=quoted_table
        )
        id_index = 2
    if where:
        sql += " WHERE " + where
    docs: List[DocMeta] = []
    for row in conn.execute(sql, list(params)).fetchall():
        docs.append(
            DocMeta(
                rowid=row[0],
                date=_as_date(row[1]),
                doc_id=None if id_index is None else row[id_index],
            )
        )
    return docs


# ---------------------------------------------------------------------------
# Exact counting from row-level FTS data
# ---------------------------------------------------------------------------


def count_from_fts(
    conn,
    fts_table: str,
    dictionary: Dictionary,
    docs: Sequence[DocMeta],
    columns: Optional[Sequence[str]] = None,
) -> Dict[str, ConceptReport]:
    """Exact distinct-document mention counts backed by FTS4 offsets()."""

    all_columns = _column_names(conn, fts_table)
    if columns is None:
        columns = all_columns
    hits_by_term, unsearchable = fetch_hits(
        conn, fts_table, dictionary.alias_texts, columns=columns
    )
    doc_by_rowid = {doc.rowid: doc for doc in docs}

    # concept -> rowid -> valid hits in that document
    candidates: Dict[str, Dict[int, List[Hit]]] = {
        concept: {} for concept in dictionary.entries
    }
    undated_by_concept: Dict[str, set] = {
        concept: set() for concept in dictionary.entries
    }

    for concept, entry in dictionary.entries.items():
        for alias in entry.aliases:
            for hit in hits_by_term.get(alias.text, []):
                doc = doc_by_rowid.get(hit.rowid)
                if doc is None or doc.date is None:
                    undated_by_concept[concept].add(hit.rowid)
                    continue
                if not alias.valid_on(doc.date):
                    continue
                candidates[concept].setdefault(hit.rowid, []).append(hit)

    suppressions: List[Suppression] = _apply_containment(
        dictionary, candidates, doc_by_rowid
    )

    reports: Dict[str, ConceptReport] = {}
    for concept in dictionary.entries:
        rowids = sorted(candidates[concept])
        notes: List[str] = []
        undated_rows = undated_by_concept[concept]
        if undated_rows:
            notes.append(
                "{} document(s) skipped: publication date unavailable, alias "
                "validity cannot be checked".format(len(undated_rows))
            )
        blocked = [
            term for term in unsearchable if dictionary.owner_of(term) == concept
        ]
        if blocked:
            notes.append(
                "alias(es) {} occur in raw text but are not searchable in the "
                "FTS index (tokenizer limitation); their mentions are not "
                "counted".format(", ".join(sorted(blocked)))
            )
        concept_suppressions = [
            item for item in suppressions if item.sub_concept == concept
        ]
        first = _first_mention(
            conn, fts_table, all_columns, concept, candidates, doc_by_rowid
        )
        reports[concept] = ConceptReport(
            concept=concept,
            exact=len(rowids),
            lower_bound=len(rowids),
            upper_bound=len(rowids),
            exact_available=True,
            first_mention=first,
            doc_ids=[
                doc_by_rowid[rowid].doc_id or rowid for rowid in rowids
            ],
            suppressed=concept_suppressions,
            notes=notes,
        )
    return reports


def _apply_containment(
    dictionary: Dictionary,
    candidates: Dict[str, Dict[int, List[Hit]]],
    doc_by_rowid: Mapping[int, DocMeta],
) -> List[Suppression]:
    # Containers first, so by the time a sub concept is processed every
    # containing concept already carries its final set of hits.
    order = _containment_order(dictionary)
    closure = dictionary.super_closure()
    suppressions: List[Suppression] = []
    for sub_concept in order:
        supers = [name for name in order if name in closure[sub_concept]]
        for super_concept in supers:
            shared = set(candidates[sub_concept]) & set(candidates[super_concept])
            for rowid in sorted(shared):
                super_hits = candidates[super_concept][rowid]
                sub_hits = candidates[sub_concept][rowid]
                contained = [
                    hit
                    for hit in sub_hits
                    if any(_contains(outer, hit) for outer in super_hits)
                ]
                if not contained:
                    continue
                if len(contained) == len(sub_hits):
                    # Every occurrence of the short concept sits inside a long
                    # one: the document is credited to the long concept only.
                    del candidates[sub_concept][rowid]
                    meta = doc_by_rowid[rowid]
                    suppressions.append(
                        Suppression(
                            rowid=rowid,
                            doc_id=meta.doc_id,
                            sub_concept=sub_concept,
                            super_concept=super_concept,
                            alias=contained[0].alias,
                        )
                    )
                else:
                    # Standalone occurrences remain; drop only nested hits so
                    # they are not used as first-mention evidence.
                    candidates[sub_concept][rowid] = [
                        hit for hit in sub_hits if hit not in contained
                    ]
    return suppressions


def _containment_order(dictionary: Dictionary) -> List[str]:
    # Kahn's algorithm on the "contained_by" edges; containers sort before the
    # concepts they contain.  The cycle check in Dictionary guarantees success.
    indegree = {name: 0 for name in dictionary.entries}
    dependents: Dict[str, List[str]] = {name: [] for name in dictionary.entries}
    for name, entry in dictionary.entries.items():
        indegree[name] = len(set(entry.contained_by))
        for super_name in entry.contained_by:
            dependents[super_name].append(name)
    ready = sorted(name for name, degree in indegree.items() if degree == 0)
    order: List[str] = []
    while ready:
        current = ready.pop(0)
        order.append(current)
        for dependent in dependents[current]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)
    return order


def _first_mention(
    conn,
    fts_table: str,
    columns: Sequence[str],
    concept: str,
    candidates: Dict[str, Dict[int, List[Hit]]],
    doc_by_rowid: Mapping[int, DocMeta],
) -> Optional[FirstMention]:
    doc_rows = candidates.get(concept, {})
    if not doc_rows:
        return None
    table = _quote_ident(fts_table)
    best: Optional[Tuple[_datetime.date, int, int, Hit]] = None
    for rowid, hits in doc_rows.items():
        day = doc_by_rowid[rowid].date
        for hit in hits:
            key = (day, rowid, hit.column_index, hit.byte_offset)
            if best is None or key < (best[0], best[1], best[2], best[3]):
                best = (day, rowid, hit.column_index, hit)
    assert best is not None
    day, rowid, column_index, hit = best
    meta = doc_by_rowid[rowid]
    text = conn.execute(
        "SELECT {col} FROM {tbl} WHERE rowid = ?".format(
            col=_quote_ident(columns[column_index]), tbl=table
        ),
        [rowid],
    ).fetchone()[0]
    snippet, char_offset = _make_snippet(text or "", hit.alias, hit.byte_offset)
    return FirstMention(
        doc_id=meta.doc_id,
        rowid=rowid,
        date=day,
        column=columns[column_index],
        alias=hit.alias,
        char_offset=char_offset,
        snippet=snippet,
    )


# ---------------------------------------------------------------------------
# Bounds from aggregate statistics
# ---------------------------------------------------------------------------


def bounds_from_aggregates(
    dictionary: Dictionary,
    alias_doc_counts: Optional[Mapping[Tuple[str, str], int]] = None,
    concept_totals: Optional[Mapping[str, int]] = None,
    corpus_size: Optional[int] = None,
    first_refs: Optional[Mapping[Any, Mapping[str, Any]]] = None,
) -> Dict[str, ConceptReport]:
    """Provable mention bounds when only aggregate statistics were retained.

    ``alias_doc_counts`` maps ``(concept, alias_text)`` to a retained distinct
    document count.  ``concept_totals`` maps a concept to a retained total
    without alias breakdown.  ``first_refs`` may be keyed by either
    ``(concept, alias_text)`` or by concept and should contain at least
    ``date``; a ``doc_id`` makes the reference independently verifiable.
    """

    alias_doc_counts = dict(alias_doc_counts or {})
    concept_totals = dict(concept_totals or {})
    first_refs = dict(first_refs or {})
    reports: Dict[str, ConceptReport] = {}

    for concept, entry in dictionary.entries.items():
        notes: List[str] = [
            "aggregate-only data: overlap between aliases and title/body "
            "duplicates cannot be resolved, so an exact distinct-document "
            "count is not provable"
        ]
        per_alias = [
            (alias.text, alias_doc_counts[(concept, alias.text)])
            for alias in entry.aliases
            if (concept, alias.text) in alias_doc_counts
        ]
        missing = [
            alias.text
            for alias in entry.aliases
            if (concept, alias.text) not in alias_doc_counts
        ]
        if missing:
            notes.append(
                "no retained count for alias(es): {}".format(", ".join(missing))
            )

        if per_alias:
            lower = max(count for _, count in per_alias)
            upper = sum(count for _, count in per_alias)
            if corpus_size is not None:
                upper = min(upper, corpus_size)
        elif concept in concept_totals:
            lower = 0
            upper = concept_totals[concept]
            notes.append(
                "retained total of {} cannot be verified against source "
                "documents; treated as an upper bound only".format(
                    concept_totals[concept]
                )
            )
        else:
            lower = 0
            upper = 0
            notes.append("no retained statistics for this concept")

        total = concept_totals.get(concept)
        if total is not None and per_alias and not (lower <= total <= upper):
            notes.append(
                "retained concept total {} is inconsistent with alias-level "
                "bounds [{}, {}]; input statistics contradict each other".format(
                    total, lower, upper
                )
            )

        if entry.contained_by:
            notes.append(
                "containment rule (absorbed into {}) cannot be applied to "
                "aggregates; bounds are not reduced for containment".format(
                    ", ".join(entry.contained_by)
                )
            )

        first_mention, first_note = _first_ref_from_aggregates(
            concept, dictionary, first_refs
        )
        if first_note:
            notes.append(first_note)

        reports[concept] = ConceptReport(
            concept=concept,
            exact=None,
            lower_bound=lower,
            upper_bound=upper,
            exact_available=False,
            first_mention=first_mention,
            notes=notes,
        )
    return reports


def _first_ref_from_aggregates(
    concept: str,
    dictionary: Dictionary,
    first_refs: Mapping[Any, Mapping[str, Any]],
) -> Tuple[Optional[FirstMention], Optional[str]]:
    candidates = []
    for alias in dictionary.entries[concept].aliases:
        ref = first_refs.get((concept, alias.text))
        if ref is not None:
            candidates.append((alias, ref))
    concept_ref = first_refs.get(concept)
    if not candidates and concept_ref is None:
        return None, None
    if candidates:
        alias, ref = min(
            candidates,
            key=lambda item: item[1].get("date") or _datetime.date.max,
        )
    else:
        alias = None
        ref = concept_ref
    verifiable = ref.get("doc_id") is not None
    mention = FirstMention(
        doc_id=ref.get("doc_id") if verifiable else None,
        rowid=ref.get("rowid", -1),
        date=_as_date(ref.get("date")),
        column=ref.get("column", ""),
        alias=alias.text if alias is not None else ref.get("alias", ""),
        char_offset=ref.get("char_offset", -1),
        snippet=ref.get("snippet", ""),
    )
    note = None
    if not verifiable:
        note = (
            "a retained earliest date ({}) has no document identifier, so the "
            "first-mention source cannot be independently checked".format(
                mention.date
            )
        )
    return mention, note
