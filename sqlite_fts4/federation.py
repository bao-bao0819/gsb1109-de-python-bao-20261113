"""Federated BM25 ranking over several local FTS4 libraries.

Each library keeps its own FTS4 table, so a per-library ``rank_bm25()`` first
place is not comparable: document frequency, average field length and the
total document count all differ between libraries.  This module recomputes
Okapi BM25 against the *merged* corpus statistics:

* total document count ``N`` is the sum of the per-library row counts,
* each field's total token length is summed across libraries,
* each term's document frequency is summed per field across libraries.

A ``column_map`` attached to every library maps its local column names to the
canonical column names shared by the federation.  The canonical column order
used in every result is taken from those mappings.

The small libraries can also be copied into one synthetic FTS4 database and
queried directly.  Both paths call the same scoring helper with the same
statistics, so ranks and tie ordering are identical.
"""

import math
import re
import sqlite3
import struct
from dataclasses import dataclass, field


K1 = 1.2
B = 0.75


# ---------------------------------------------------------------------------
# FTS4 shadow table helpers
# ---------------------------------------------------------------------------

def _decode_varints(buf):
    """Decode FTS4 varints (1-9 bytes, least-significant 7 bits first)."""
    values = []
    i = 0
    length = len(buf)
    while i < length:
        value = 0
        shift = 0
        for byte_index in range(9):
            byte = buf[i]
            i += 1
            if byte_index == 8:
                value |= byte << shift
                break
            value |= (byte & 0x7F) << shift
            if not byte & 0x80:
                break
            shift += 7
        values.append(value)
    return values


def _decode_matchinfo(buf):
    return struct.unpack("@" + ("I" * (len(buf) // 4)), buf)


def _quote_identifier(identifier):
    return '"{}"'.format(identifier.replace('"', '""'))


def _shadow_table(table, suffix):
    # The whole shadow table name must be quoted as one identifier, otherwise
    # "docs"_stat is parsed as table docs aliased to _stat.
    return _quote_identifier("{}{}".format(table, suffix))


def _quote_phrase(phrase):
    return '"{}"'.format(phrase.replace('"', '""'))


def _phrases(query):
    """Split a MATCH query into its matchable phrase strings.

    The catalog search covered here uses plain keyword queries; each
    whitespace separated token is treated as one phrase.
    """
    if isinstance(query, str):
        tokens = query.split()
    else:
        tokens = list(query)
    return [token.strip('"') for token in tokens if token.strip('"')]


# ---------------------------------------------------------------------------
# Library description
# ---------------------------------------------------------------------------

@dataclass
class FtsLibrary:
    """One local FTS4 library and its column mapping.

    ``column_map`` maps local FTS4 column names to canonical column names.
    """

    connection: object
    table: str
    column_map: dict
    name: str = None

    def __post_init__(self):
        if not self.column_map:
            raise ValueError("column_map must not be empty")
        self.column_map = dict(self.column_map)
        if self.name is None:
            self.name = self.table
        quoted = _quote_identifier(self.table)
        local_columns = {
            row[1] for row in self.connection.execute(
                "PRAGMA table_info({})".format(quoted)
            )
        }
        missing = set(self.column_map) - local_columns
        if missing:
            raise ValueError(
                "library {!r} has no column(s): {}".format(
                    self.name, ", ".join(sorted(missing))
                )
            )
        ddl = self.connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (self.table,),
        ).fetchone()[0]
        self.tokenizer = _parse_tokenizer(ddl)
        self.tokenizer_clause = _tokenizer_clause(ddl)
        self._term_cache = {}
        self._indexed_positions = self._column_positions(local_columns, ddl)
        unindexed_mapping = [
            local for local in self.column_map
            if local not in self._indexed_positions
        ]
        if unindexed_mapping:
            raise ValueError(
                "library {!r} maps UNINDEXED column(s), which cannot be "
                "ranked: {}".format(self.name, ", ".join(unindexed_mapping))
            )
        unmapped_indexed = [
            name for name in self._indexed_positions
            if name not in self.column_map
        ]
        if unmapped_indexed:
            raise ValueError(
                "library {!r} has indexed column(s) missing from column_map; "
                "they would change matching but never reach the merged "
                "library: {}".format(self.name, ", ".join(unmapped_indexed))
            )
        self._load_shadow_stats()

    def _column_positions(self, local_columns, ddl):
        unindexed = set(re.findall(r"([\w]+)\s+UNINDEXED", ddl, flags=re.IGNORECASE))
        positions = {}
        for row in self.connection.execute(
            "PRAGMA table_info({})".format(_quote_identifier(self.table))
        ):
            cid, name = row[0], row[1]
            if name in local_columns and name not in unindexed:
                positions[name] = cid
        return positions

    def _load_shadow_stats(self):
        stat_row = self.connection.execute(
            "SELECT value FROM {} WHERE id = 0".format(
                _shadow_table(self.table, "_stat")
            )
        ).fetchone()
        if stat_row is None:
            self.row_count = 0
            self.column_totals = {name: 0 for name in self.column_map}
            self.doc_sizes = {}
            return
        stats = _decode_varints(stat_row[0])
        self.row_count = stats[0]
        self.column_totals = {
            local: stats[1 + position]
            for local, position in self._indexed_positions.items()
            if local in self.column_map
        }
        self.doc_sizes = {}
        for docid, blob in self.connection.execute(
            "SELECT docid, size FROM {}".format(
                _shadow_table(self.table, "_docsize")
            )
        ):
            sizes = _decode_varints(blob)
            self.doc_sizes[docid] = sizes

    def doc_length(self, docid, local_column):
        return self.doc_sizes[docid][self._indexed_positions[local_column]]

    def term_column_stats(self, phrase):
        """Per-column ``(total_hits, document_frequency)`` for one phrase.

        ``total_hits`` is the phrase's occurrence count across every row in
        this library (the middle matchinfo value), which upper-bounds the
        term frequency of any single unseen document.
        """
        cached = self._term_cache.get(phrase)
        if cached is not None:
            return cached
        quoted = _quote_identifier(self.table)
        row = self.connection.execute(
            "SELECT matchinfo({}, 'pcx') FROM {} WHERE {} MATCH ? LIMIT 1".format(
                quoted, quoted, quoted
            ),
            (_quote_phrase(phrase),),
        ).fetchone()
        if row is None:
            stats = {local: (0, 0) for local in self.column_map}
        else:
            info = _decode_matchinfo(row[0])
            stats = {}
            for local, position in sorted(
                self._indexed_positions.items(), key=lambda item: item[1]
            ):
                if local not in self.column_map:
                    continue
                triple = 2 + 3 * position
                stats[local] = (info[triple + 1], info[triple + 2])
        self._term_cache[phrase] = stats
        return stats

    def term_document_frequency(self, phrase):
        """Per-column document count containing ``phrase`` in this library."""
        return {
            local: stats[1]
            for local, stats in self.term_column_stats(phrase).items()
        }

    def matching_documents(self, phrases):
        """Yield ``(docid, tf matrix, values)`` for docs matching any phrase.

        The tf matrix is ``{phrase: {local_column: hit_count}}`` and
        ``values`` holds the mapped columns in canonical column order.
        """
        if not phrases:
            return
        quoted = _quote_identifier(self.table)
        match_query = " OR ".join(_quote_phrase(phrase) for phrase in phrases)
        # Select mapped columns in canonical order (column_map key order
        # matches its value order), so the returned value tuple lines up with
        # the canonical columns even when local storage order differs.
        canonical_locals = list(self.column_map.keys())
        select_columns = ", ".join(
            _quote_identifier(local) for local in canonical_locals
        )
        rows = self.connection.execute(
            "SELECT rowid, {select_columns}, matchinfo({table}, 'pcx') "
            "FROM {table} WHERE {table} MATCH ?".format(
                select_columns=select_columns, table=quoted
            ),
            (match_query,),
        ).fetchall()
        for row in rows:
            docid = row[0]
            values = tuple(row[1:-1])
            info = _decode_matchinfo(row[-1])
            phrase_count, column_count = info[0], info[1]
            tf = {
                phrase: {local: 0 for local in canonical_locals}
                for phrase in phrases
            }
            for phrase_index, phrase in enumerate(phrases):
                for local, position in sorted(
                    self._indexed_positions.items(), key=lambda item: item[1]
                ):
                    if local not in self.column_map:
                        continue
                    triple = 2 + 3 * (phrase_index * column_count + position)
                    tf[phrase][local] = info[triple]
            yield docid, tf, values


def _parse_tokenizer(ddl):
    match = re.search(r"tokenize\s*=?\s*([^,)]+)", ddl, flags=re.IGNORECASE)
    if match is None:
        return "unicode61"
    return match.group(1).strip().strip("'\"")


def _tokenizer_clause(ddl):
    """Return the verbatim ``tokenize ...`` clause from an FTS4 DDL, or None."""
    match = re.search(
        r"tokenize\s*(?:=\s*)?.+?(?=\))", ddl, flags=re.IGNORECASE
    )
    if match is None:
        return None
    return match.group(0).strip()


# ---------------------------------------------------------------------------
# Shared BM25 scoring (identical for federated and merged paths)
# ---------------------------------------------------------------------------

def _bm25_score(term_frequency, doc_length, average_length, document_frequency,
                total_documents, k1=K1, b=B):
    """Okapi BM25, returning the negative score used by ``rank_bm25()``.

    Every argument is indexed ``[phrase_index][column_index]`` (scalars for
    the single-value ones).  Floating point operations happen in phrase then
    column order, so callers sharing the same inputs get bit-identical
    results.
    """
    score = 0.0
    for phrase_index in range(len(document_frequency)):
        for column_index in range(len(document_frequency[phrase_index])):
            avg = average_length[column_index]
            if avg == 0:
                length_norm = 0.0
            else:
                length_norm = (
                    1 - b + b * (doc_length[column_index] / avg)
                )
            df = document_frequency[phrase_index][column_index]
            idf = max(
                math.log(
                    (total_documents - df + 0.5) / (df + 0.5)
                ),
                0.0,
            )
            tf = term_frequency[phrase_index][column_index]
            denominator = tf + k1 * length_norm
            if denominator == 0:
                rhs = 0.0
            else:
                rhs = (tf * (k1 + 1)) / denominator
            score += idf * rhs
    return -score


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

@dataclass
class FederatedResult:
    rank: int
    score: float
    library: str
    library_index: int
    docid: int
    values: tuple
    columns: tuple

    def as_dict(self):
        """Canonical column name to value, plus rank, score and provenance."""
        data = dict(zip(self.columns, self.values))
        data.update(
            rank=self.rank,
            score=self.score,
            library=self.library,
            docid=self.docid,
        )
        return data


@dataclass
class FederatedRanking:
    columns: tuple
    query: str
    results: list = field(default_factory=list)
    libraries_searched: int = 0
    stopped_early: bool = False

    def __iter__(self):
        return iter(self.results)

    def __len__(self):
        return len(self.results)

    def keys(self):
        return list(self.columns)


def _validate_libraries(libraries):
    if not libraries:
        raise ValueError("at least one FtsLibrary is required")
    canonical = list(libraries[0].column_map.values())
    for library in libraries[1:]:
        if list(library.column_map.values()) != canonical:
            raise ValueError(
                "all column_map values must share the same canonical columns "
                "in the same order; library {!r} differs".format(library.name)
            )
    tokenizers = {library.tokenizer for library in libraries}
    if len(tokenizers) != 1:
        raise ValueError(
            "all libraries must use the same tokenizer, found: {}".format(
                ", ".join(sorted(tokenizers))
            )
        )
    return canonical


# ---------------------------------------------------------------------------
# Federated ranking
# ---------------------------------------------------------------------------

def rank_federated(libraries, query, limit=None, early_stop=True,
                   k1=K1, b=B):
    """Rank documents across separate FTS4 libraries with merged statistics.

    ``limit`` cuts the ranking after that many results.  When ``early_stop``
    is set and a limit is requested, remaining libraries are skipped only
    once an upper bound on every unseen document's score proves they cannot
    reach the current top ``limit``.
    """
    canonical = _validate_libraries(libraries)
    phrases = _phrases(query)
    column_count = len(canonical)

    total_documents = sum(library.row_count for library in libraries)
    field_totals = [0] * column_count
    for column_index, canonical_name in enumerate(canonical):
        for library in libraries:
            local_name = _local_name(library, canonical_name)
            field_totals[column_index] += library.column_totals[local_name]
    average_length = [
        total / total_documents if total_documents else 0.0
        for total in field_totals
    ]

    document_frequency = []
    for phrase in phrases:
        per_column = [0] * column_count
        for library in libraries:
            local_df = library.term_document_frequency(phrase)
            for column_index, canonical_name in enumerate(canonical):
                per_column[column_index] += local_df[
                    _local_name(library, canonical_name)
                ]
        document_frequency.append(per_column)

    ranking = FederatedRanking(
        columns=tuple(canonical), query=query if isinstance(query, str)
        else " ".join(phrases)
    )
    if not phrases:
        return ranking

    candidates = []
    searched = 0
    for library_index, library in enumerate(libraries):
        searched += 1
        for docid, tf_map, values in library.matching_documents(phrases):
            tf = [
                [
                    tf_map[phrase][_local_name(library, canonical_name)]
                    for canonical_name in canonical
                ]
                for phrase in phrases
            ]
            doc_length = [
                library.doc_length(
                    docid, _local_name(library, canonical_name)
                )
                for canonical_name in canonical
            ]
            score = _bm25_score(
                tf, doc_length, average_length, document_frequency,
                total_documents, k1=k1, b=b,
            )
            candidates.append(
                {
                    "score": score,
                    "library_index": library_index,
                    "library": library.name,
                    "docid": docid,
                    "values": values,
                }
            )

        if (
            early_stop
            and limit is not None
            and len(candidates) >= limit
            and library_index + 1 < len(libraries)
            and _cannot_reach_limit(
                libraries[library_index + 1:], phrases, canonical,
                document_frequency, total_documents, average_length,
                _current_kth_score(candidates, limit), k1, b,
            )
        ):
            break

    ranking.libraries_searched = searched
    ranking.stopped_early = searched < len(libraries)
    ranking.results = _finalize(candidates, canonical, limit)
    return ranking


def _local_name(library, canonical_name):
    for local, canonical in library.column_map.items():
        if canonical == canonical_name:
            return local
    raise KeyError(canonical_name)


def _current_kth_score(candidates, limit):
    ordered = sorted(candidates, key=lambda item: item["score"])
    return ordered[limit - 1]["score"]


def _cannot_reach_limit(unseen_libraries, phrases, canonical,
                        document_frequency, total_documents, average_length,
                        kth_score, k1, b):
    """Upper-bound the best positive BM25 score available in unseen libs.

    For one phrase/column the BM25 term is
    ``idf * tf * (k1 + 1) / (tf + k1 * d)``, increasing in ``tf`` and
    decreasing in the length normalization ``d``.  In an unseen document the
    term frequency cannot exceed the phrase's total hit count in that
    library's column, and ``d`` is at least the value for the shortest
    observed field.  Combining those two independent maxima gives a valid
    per-column upper bound; summing over phrases and columns bounds the whole
    document, so if it still cannot beat the current k-th score the remaining
    libraries may safely be skipped.
    """
    if total_documents == 0 or not unseen_libraries:
        return True
    if sum(library.row_count for library in unseen_libraries) == 0:
        return True
    column_count = len(canonical)
    min_length_norm = [None] * column_count
    for column_index, canonical_name in enumerate(canonical):
        shortest = None
        for library in unseen_libraries:
            local = _local_name(library, canonical_name)
            position = library._indexed_positions[local]
            for sizes in library.doc_sizes.values():
                length = sizes[position]
                if shortest is None or length < shortest:
                    shortest = length
        if shortest is None or average_length[column_index] == 0:
            continue
        min_length_norm[column_index] = (
            1 - b + b * (shortest / average_length[column_index])
        )
    bound = 0.0
    for phrase_index, phrase in enumerate(phrases):
        for column_index, canonical_name in enumerate(canonical):
            df = document_frequency[phrase_index][column_index]
            idf = max(
                math.log(
                    (total_documents - df + 0.5) / (df + 0.5)
                ),
                0.0,
            )
            if idf == 0.0:
                continue
            max_tf = 0
            for library in unseen_libraries:
                local = _local_name(library, canonical_name)
                max_tf = max(
                    max_tf, library.term_column_stats(phrase)[local][0]
                )
            norm = min_length_norm[column_index]
            if max_tf == 0 or norm is None:
                continue
            denominator = max_tf + k1 * norm
            bound += idf * (max_tf * (k1 + 1)) / denominator
    # Scores are returned negated, so compare on the positive scale.
    return bound < -kth_score


def _assign_ranks(ordered):
    """Attach competition ranks (ties share a rank) in score order."""
    results = []
    previous_score = None
    competition_rank = 0
    for position, candidate in enumerate(ordered, start=1):
        if previous_score is None or candidate["score"] != previous_score:
            competition_rank = position
            previous_score = candidate["score"]
        results.append(
            FederatedResult(
                rank=competition_rank,
                score=candidate["score"],
                library=candidate["library"],
                library_index=candidate["library_index"],
                docid=candidate["docid"],
                values=candidate["values"],
                columns=candidate["columns"],
            )
        )
    return results


def _finalize(candidates, canonical, limit):
    ordered = sorted(
        candidates,
        key=lambda item: (item["score"], item["library_index"], item["docid"]),
    )
    if limit is not None:
        ordered = ordered[:limit]
    for candidate in ordered:
        candidate.setdefault("columns", tuple(canonical))
    return _assign_ranks(ordered)


# ---------------------------------------------------------------------------
# Synthetic merged library
# ---------------------------------------------------------------------------

class MergedLibrary:
    """A single FTS4 database holding every library's documents."""

    def __init__(self, libraries, table="merged", connection=None,
                 k1=K1, b=B):
        self.canonical = _validate_libraries(libraries)
        self.libraries = libraries
        self.table = table
        self.connection = connection
        self.k1 = k1
        self.b = b
        self.provenance = {}
        self._build()

    def _build(self):
        if self.connection is None:
            self.connection = sqlite3.connect(":memory:")
        columns = ", ".join(_quote_identifier(name) for name in self.canonical)
        clause = self.libraries[0].tokenizer_clause
        tokenize = "" if clause is None else ", " + clause
        self.connection.execute(
            "CREATE VIRTUAL TABLE {} USING fts4({}{})".format(
                _quote_identifier(self.table), columns, tokenize
            )
        )
        insert_sql = "INSERT INTO {} ({}) VALUES ({})".format(
            _quote_identifier(self.table),
            ", ".join(_quote_identifier(name) for name in self.canonical),
            ", ".join("?" for _ in self.canonical),
        )
        merged_rowid = 0
        for library_index, library in enumerate(self.libraries):
            local_columns = [
                _local_name(library, name) for name in self.canonical
            ]
            select_sql = "SELECT rowid, {} FROM {} ORDER BY rowid".format(
                ", ".join(_quote_identifier(name) for name in local_columns),
                _quote_identifier(library.table),
            )
            for row in library.connection.execute(select_sql):
                local_docid = row[0]
                self.connection.execute(insert_sql, row[1:])
                merged_rowid += 1
                self.provenance[merged_rowid] = (
                    library_index, library.name, local_docid
                )
        self._register_global_bm25()

    def _register_global_bm25(self):
        stat_row = self.connection.execute(
            "SELECT value FROM {} WHERE id = 0".format(
                _shadow_table(self.table, "_stat")
            )
        ).fetchone()
        column_count = len(self.canonical)
        if stat_row is None:
            total_documents = 0
            field_totals = [0] * column_count
        else:
            stat = _decode_varints(stat_row[0])
            total_documents = stat[0]
            field_totals = stat[1:1 + column_count]
        average_length = [
            total / total_documents if total_documents else 0.0
            for total in field_totals
        ]
        helper = _bm25_score

        def rank_bm25_global(raw_matchinfo):
            if not raw_matchinfo:
                return None
            info = _decode_matchinfo(raw_matchinfo)
            phrase_count, match_column_count = info[0], info[1]
            term_frequency = []
            document_frequency = []
            for phrase_index in range(phrase_count):
                tf_row = []
                df_row = []
                for column_index in range(column_count):
                    triple = 2 + 3 * (
                        phrase_index * match_column_count + column_index
                    )
                    tf_row.append(info[triple])
                    df_row.append(info[triple + 2])
                term_frequency.append(tf_row)
                document_frequency.append(df_row)
            length_offset = 2 + 3 * phrase_count * match_column_count + 1
            doc_length = list(
                info[length_offset:length_offset + column_count]
            )
            return helper(
                term_frequency, doc_length, average_length,
                document_frequency, total_documents, k1=self.k1, b=self.b,
            )

        self.connection.create_function(
            "rank_bm25_global", 1, rank_bm25_global
        )

    def search(self, query, limit=None):
        """Direct FTS4 query against the merged database."""
        phrases = _phrases(query)
        ranking = FederatedRanking(
            columns=tuple(self.canonical),
            query=query if isinstance(query, str) else " ".join(phrases),
        )
        if not phrases:
            return ranking
        quoted = _quote_identifier(self.table)
        column_list = ", ".join(
            _quote_identifier(name) for name in self.canonical
        )
        match_query = " OR ".join(_quote_phrase(phrase) for phrase in phrases)
        rows = self.connection.execute(
            """
            SELECT rowid, {column_list},
                   rank_bm25_global(matchinfo({table}, 'pcxnl')) AS score
            FROM {table} WHERE {table} MATCH ?
            ORDER BY score ASC, rowid ASC
            """.format(column_list=column_list, table=quoted),
            (match_query,),
        ).fetchall()
        if limit is not None:
            rows = rows[:limit]
        columns_tuple = tuple(self.canonical)
        candidates = []
        for row in rows:
            merged_rowid, values, score = row[0], row[1:-1], row[-1]
            library_index, library_name, docid = self.provenance[merged_rowid]
            candidates.append(
                {
                    "score": score,
                    "library_index": library_index,
                    "library": library_name,
                    "docid": docid,
                    "values": tuple(values),
                    "columns": columns_tuple,
                }
            )
        # Re-sort with the federated tie-break so ranking does not depend on
        # the synthetic rowid assignment.
        candidates.sort(
            key=lambda item: (
                item["score"], item["library_index"], item["docid"]
            )
        )
        ranking.results = _assign_ranks(candidates)
        ranking.libraries_searched = len(self.libraries)
        return ranking


def merge_libraries(libraries, table="merged", connection=None, k1=K1, b=B):
    """Copy small libraries into one synthetic FTS4 database."""
    return MergedLibrary(
        libraries, table=table, connection=connection, k1=k1, b=b
    )
