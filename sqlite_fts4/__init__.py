import struct
import math
import json
import traceback
from functools import wraps


def register_functions(conn):
    "Registers these custom functions against an SQLite connection"
    conn.create_function("rank_score", 1, rank_score)
    conn.create_function("decode_matchinfo", 1, decode_matchinfo_str)
    conn.create_function("annotate_matchinfo", 2, annotate_matchinfo)
    conn.create_function("rank_bm25", 1, rank_bm25)


def wrap_sqlite_function_in_error_logger(fn):
    # Because SQLite swallows exceptions inside custom functions
    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception:
            traceback.print_exc()
            raise

    return wrapper


def decode_matchinfo_str(buf):
    return str(list(decode_matchinfo(buf)))


def decode_matchinfo(buf, byte_order=None):
    # buf is a bytestring of unsigned integers, each 4 bytes long
    # byte_order defaults to None, meaning "use native byte order" - pass
    # "little" or "big" to decode buffers exported from another machine
    if byte_order is None:
        format_prefix = "@"
    elif byte_order == "little":
        format_prefix = "<"
    elif byte_order == "big":
        format_prefix = ">"
    else:
        raise ValueError("byte_order must be None, 'little' or 'big'")
    return struct.unpack(format_prefix + ("I" * (len(buf) // 4)), buf)


def _matchinfo_sections(format_string, num_phrases, num_columns):
    # Returns [(format_char, offset, size_in_integers), ...] for a format string
    sections = []
    offset = 0
    for ch in format_string:
        if ch in "pcn":
            size = 1
        elif ch == "x":
            size = 3 * num_phrases * num_columns
        elif ch == "y":
            size = num_phrases * num_columns
        elif ch == "b":
            size = num_phrases * ((num_columns + 31) // 32)
        elif ch in "als":
            size = num_columns
        else:
            raise ValueError("Unknown matchinfo format character: {!r}".format(ch))
        sections.append((ch, offset, size))
        offset += size
    return sections


def _validate_matchinfo_values(values, num_phrases, num_columns, sections):
    # Structural checks on one decoded interpretation: header counts must
    # match the archived metadata, and per-column hit relationships in the
    # 'x' section must hold (row hits <= total hits, docs <= hits, etc.)
    problems = []
    row_count = None
    for ch, offset, size in sections:
        if ch == "n":
            row_count = values[offset]
    for ch, offset, size in sections:
        if ch == "p" and values[offset] != num_phrases:
            problems.append(
                "phrase count is {}, expected {}".format(values[offset], num_phrases)
            )
        elif ch == "c" and values[offset] != num_columns:
            problems.append(
                "column count is {}, expected {}".format(values[offset], num_columns)
            )
        elif ch == "x":
            idx = offset
            for phrase_index in range(num_phrases):
                for column_index in range(num_columns):
                    hits_this_row = values[idx]
                    hits_all_rows = values[idx + 1]
                    docs_with_hits = values[idx + 2]
                    where = "phrase {}, column {}".format(phrase_index, column_index)
                    if hits_all_rows < hits_this_row:
                        problems.append(
                            "{}: hits this row {} exceed hits all rows {}".format(
                                where, hits_this_row, hits_all_rows
                            )
                        )
                    if docs_with_hits > hits_all_rows:
                        problems.append(
                            "{}: docs with hits {} exceed hits all rows {}".format(
                                where, docs_with_hits, hits_all_rows
                            )
                        )
                    if hits_all_rows > 0 and docs_with_hits == 0:
                        problems.append(
                            "{}: {} hits but zero docs with hits".format(
                                where, hits_all_rows
                            )
                        )
                    if row_count is not None and docs_with_hits > row_count:
                        problems.append(
                            "{}: docs with hits {} exceed row count {}".format(
                                where, docs_with_hits, row_count
                            )
                        )
                    idx += 3
    return problems


def _matchinfo_checksums(values, num_phrases, num_columns, sections):
    # Independent check values that can be recomputed per interpretation
    checksums = {}
    offsets = {ch: offset for ch, offset, _ in sections}
    if "x" in offsets:
        column_total_hits = [0] * num_columns
        column_docs_with_hits = [0] * num_columns
        idx = offsets["x"]
        for _ in range(num_phrases):
            for column_index in range(num_columns):
                column_total_hits[column_index] += values[idx + 1]
                column_docs_with_hits[column_index] += values[idx + 2]
                idx += 3
        checksums["column_total_hits"] = column_total_hits
        checksums["column_docs_with_hits"] = column_docs_with_hits
        checksums["total_hits"] = sum(column_total_hits)
    if "n" in offsets:
        checksums["row_count"] = values[offsets["n"]]
    return checksums


def _checksum_mismatches(computed, expected):
    mismatches = []
    for name, expected_value in expected.items():
        if name not in computed:
            mismatches.append(
                {"name": name, "error": "checksum cannot be computed for this format"}
            )
        elif computed[name] != expected_value:
            mismatches.append(
                {"name": name, "expected": expected_value, "actual": computed[name]}
            )
    return mismatches


def _distinguishing_checksums(candidates, num_phrases, num_columns, sections):
    # Checksum items whose computed values differ between the candidate
    # interpretations - each one is sufficient to tell them apart
    computed = {
        byte_order: _matchinfo_checksums(values, num_phrases, num_columns, sections)
        for byte_order, values in candidates.items()
    }
    distinguishing = []
    for name in computed.get("little", {}):
        little_value = computed["little"][name]
        big_value = computed["big"][name]
        if little_value != big_value:
            item = {"name": name, "little": little_value, "big": big_value}
            if isinstance(little_value, list):
                item["differ_at"] = [
                    i
                    for i, (a, b) in enumerate(zip(little_value, big_value))
                    if a != b
                ]
            distinguishing.append(item)
    return distinguishing


def _migrate_matchinfo_record(record, default_format_string):
    buf = record["buf"]
    num_phrases = record["num_phrases"]
    num_columns = record["num_columns"]
    format_string = record.get("format", default_format_string)
    expected_checksums = record.get("checksums") or {}
    result = {
        "num_phrases": num_phrases,
        "num_columns": num_columns,
        "format": format_string,
    }
    if "id" in record:
        result["id"] = record["id"]

    # Shape checks - these are the same under either byte order
    if len(buf) % 4:
        result.update(
            status="invalid",
            error="buffer length {} is not a multiple of 4".format(len(buf)),
        )
        return result
    try:
        sections = _matchinfo_sections(format_string, num_phrases, num_columns)
    except ValueError as ex:
        result.update(status="invalid", error=str(ex))
        return result
    expected_length = sections[-1][1] + sections[-1][2] if sections else 0
    num_integers = len(buf) // 4
    if num_integers != expected_length:
        result.update(
            status="invalid",
            error=(
                "buffer holds {} integers but format {!r} with {} phrases and "
                "{} columns requires {}".format(
                    num_integers, format_string, num_phrases, num_columns,
                    expected_length,
                )
            ),
        )
        return result

    # Try both byte orders - never assume the native one
    candidates = {}
    rejected = {}
    for byte_order in ("little", "big"):
        values = list(decode_matchinfo(buf, byte_order))
        problems = _validate_matchinfo_values(
            values, num_phrases, num_columns, sections
        )
        if problems:
            rejected[byte_order] = problems
        else:
            candidates[byte_order] = values

    if not candidates:
        result.update(
            status="invalid",
            error="no byte order interpretation passes validation",
            problems=rejected,
        )
        return result

    if len(candidates) == 2 and candidates["little"] == candidates["big"]:
        # Identical under both orders (e.g. all-zero segments) - accept as-is
        chosen_order, chosen = "either", candidates["little"]
    elif len(candidates) == 1:
        chosen_order, chosen = next(iter(candidates.items()))
    else:
        # Both orders are structurally valid but decode to different values:
        # only the archived checksums can tell them apart
        matching = [
            byte_order
            for byte_order, values in candidates.items()
            if not _checksum_mismatches(
                _matchinfo_checksums(values, num_phrases, num_columns, sections),
                expected_checksums,
            )
        ]
        if len(matching) == 1:
            chosen_order = matching[0]
            chosen = candidates[chosen_order]
        else:
            result.update(
                status="ambiguous",
                error=(
                    "both byte orders are valid but decode to different values; "
                    "compare the listed checksum items against the archive to "
                    "resolve this"
                ),
                candidates=candidates,
                differing_indices=[
                    i
                    for i, (a, b) in enumerate(
                        zip(candidates["little"], candidates["big"])
                    )
                    if a != b
                ],
                distinguishing_checksums=_distinguishing_checksums(
                    candidates, num_phrases, num_columns, sections
                ),
            )
            if not matching:
                result["checksum_mismatches"] = {
                    byte_order: _checksum_mismatches(
                        _matchinfo_checksums(
                            candidates[byte_order], num_phrases, num_columns, sections
                        ),
                        expected_checksums,
                    )
                    for byte_order in ("little", "big")
                }
            return result

    mismatches = _checksum_mismatches(
        _matchinfo_checksums(chosen, num_phrases, num_columns, sections),
        expected_checksums,
    )
    if mismatches:
        result.update(
            status="invalid",
            error="decoded values contradict the archived checksums",
            byte_order=chosen_order,
            checksum_mismatches=mismatches,
        )
        return result

    result.update(status="ok", byte_order=chosen_order, values=chosen)
    if expected_checksums:
        result["verified_checksums"] = sorted(expected_checksums)
    return result


def migrate_matchinfo_batch(records, format_string="pcx"):
    """Migrates a batch of archived matchinfo() buffers of unknown byte order.

    Each record is a dictionary with keys:
      - buf: the raw matchinfo() binary string
      - num_phrases: number of phrases in the original query ('p')
      - num_columns: number of columns in the FTS4 table ('c')
      - format: optional matchinfo format string, defaults to "pcx"
      - checksums: optional dictionary of independently archived check
        values - any of "column_total_hits", "column_docs_with_hits",
        "total_hits" (for formats containing 'x') and "row_count"
        (for formats containing 'n')
      - id: optional identifier, passed through to the result

    Returns one result dictionary per record. Status is "ok" (with the
    detected byte_order and decoded values), "invalid" (shape or hit
    relationship checks failed, or checksums were contradicted) or
    "ambiguous" (both byte orders are valid but decode differently - the
    result lists the checksum items sufficient to distinguish them; the
    native byte order is never assumed).
    """
    return [
        _migrate_matchinfo_record(record, format_string) for record in records
    ]


def _error(m):
    return {"error": m}


@wrap_sqlite_function_in_error_logger
def annotate_matchinfo(buf, format_string):
    return json.dumps(_annotate_matchinfo(buf, format_string), indent=2)


def _annotate_matchinfo(buf, format_string):
    # See https://www.sqlite.org/fts3.html#matchinfo for detailed specification
    matchinfo = list(decode_matchinfo(buf))
    if not matchinfo:
        return {}
    matchinfo_index = 0
    p_num_phrases = None
    c_num_columns = None

    def _next():
        nonlocal matchinfo_index
        value = matchinfo[matchinfo_index]
        matchinfo_index += 1
        return value, matchinfo_index - 1

    results = {}
    for ch in format_string:
        if ch == "p":
            p_num_phrases, idx = _next()
            results["p"] = {
                "value": p_num_phrases,
                "title": "Number of matchable phrases in the query",
                "idx": idx,
            }
        elif ch == "c":
            c_num_columns, idx = _next()
            results["c"] = {
                "value": c_num_columns,
                "title": "Number of user defined columns in the FTS table",
                "idx": idx,
            }
        elif ch == "x":
            # Depends on p and c
            if None in (p_num_phrases, c_num_columns):
                return _error("'x' must be preceded by 'p' and 'c'")
            info = []
            results["x"] = {
                "value": info,
                "title": "Details for each phrase/column combination",
            }
            # 3 * c_num_columns * p_num_phrases
            for phrase_index in range(p_num_phrases):
                for column_index in range(c_num_columns):
                    hits_this_column_this_row, idx1 = _next()
                    hits_this_column_all_rows, idx2 = _next()
                    docs_with_hits, idx3 = _next()
                    info.append(
                        {
                            "phrase_index": phrase_index,
                            "column_index": column_index,
                            "hits_this_column_this_row": hits_this_column_this_row,
                            "hits_this_column_all_rows": hits_this_column_all_rows,
                            "docs_with_hits": docs_with_hits,
                            "idxs": [idx1, idx2, idx3],
                        }
                    )
        elif ch == "y":
            if None in (p_num_phrases, c_num_columns):
                return _error("'y' must be preceded by 'p' and 'c'")
            info = []
            results["y"] = {
                "value": info,
                "title": "Usable phrase matches for each phrase/column combination",
            }
            for phrase_index in range(p_num_phrases):
                for column_index in range(c_num_columns):
                    hits_for_phrase_in_col, idx = _next()
                    info.append(
                        {
                            "phrase_index": phrase_index,
                            "column_index": column_index,
                            "hits_for_phrase_in_col": hits_for_phrase_in_col,
                            "idx": idx,
                        }
                    )
        elif ch == "b":
            if None in (p_num_phrases, c_num_columns):
                return _error("'b' must be preceded by 'p' and 'c'")
            values = []
            # We get back one integer for each 32 columns for each phrase
            num_32_column_chunks = (c_num_columns + 31) // 32
            decoded = {}
            for phrase_index in range(p_num_phrases):
                current_phrase_chunks = []
                for _ in range(num_32_column_chunks):
                    v = _next()[0]
                    values.append(v)
                    current_phrase_chunks.append(v)
                decoded["phrase_{}".format(phrase_index)] = "".join(
                    [
                        "{:032b}".format(unsigned_integer)[::-1]
                        for unsigned_integer in current_phrase_chunks
                    ]
                )
            results["b"] = {
                "title": "Bitfield showing which phrases occur in which columns",
                "value": values,
                # Each integer is a 32bit unsigned integer, least significant
                # bit is column 0, then column 1, then so on
                "decoded": decoded,
            }
        elif ch == "n":
            value, idx = _next()
            results["n"] = {
                "value": value,
                "title": "Number of rows in the FTS4 table",
                "idx": idx,
            }
        elif ch == "a":
            if c_num_columns is None:
                return _error("'a' must be preceded by 'c'")
            values = []
            for i in range(c_num_columns):
                value, idx = _next()
                values.append(
                    {"column_index": i, "average_num_tokens": value, "idx": idx}
                )
            results["a"] = {
                "title": "Average number of tokens in each column across the whole table",
                "value": values,
            }
        elif ch == "l":
            if c_num_columns is None:
                return _error("'l' must be preceded by 'c'")
            values = []
            for i in range(c_num_columns):
                value, idx = _next()
                values.append({"column_index": i, "num_tokens": value, "idx": idx})
            results["l"] = {
                "title": "Number of tokens in each column of the current row of the FTS4 table",
                "value": values,
            }
        elif ch == "s":
            if c_num_columns is None:
                return _error("'s' must be preceded by 'c'")
            values = []
            for i in range(c_num_columns):
                value, idx = _next()
                values.append(
                    {
                        "column_index": i,
                        "length_phrase_subsequence_match": value,
                        "idx": idx,
                    }
                )
            results["s"] = {
                "title": "Length of longest subsequence of phrase matching each column",
                "value": values,
            }
    return results


@wrap_sqlite_function_in_error_logger
def rank_score(raw_matchinfo):
    # Score using matchinfo called w/default args 'pcx' - based on example rank
    # function http://sqlite.org/fts3.html#appendix_a
    # The overall relevancy returned is the sum of the relevancies of each
    # column value in the FTS table. The relevancy of a column value is the
    # sum of the following for each reportable phrase in the FTS query:
    #   (<hit count > / <global hit count>)
    if not raw_matchinfo:
        return None
    matchinfo = _annotate_matchinfo(raw_matchinfo, "pcx")
    score = 0.0
    x_phrase_column_details = matchinfo["x"]["value"]
    for details in x_phrase_column_details:
        hits_this_column_this_row = details["hits_this_column_this_row"]
        hits_this_column_all_rows = details["hits_this_column_all_rows"]
        if hits_this_column_this_row > 0:
            score += float(hits_this_column_this_row) / hits_this_column_all_rows
    return -score


@wrap_sqlite_function_in_error_logger
def rank_bm25(raw_match_info):
    "Must be called with output of matchinfo 'pcnalx'"
    if not raw_match_info:
        return None
    match_info = _annotate_matchinfo(raw_match_info, "pcnalx")
    # How much should multiple matches in the same document increase the score?
    k = 1.2
    # How much should document length affect the score? (shorter docs = higher score)
    b = 0.75
    score = 0.0

    phrase_count = match_info["p"]["value"]
    column_count = match_info["c"]["value"]
    total_row_count = match_info["n"]["value"]

    for phrase_index in range(phrase_count):
        for column_index in range(column_count):
            average_num_tokens = match_info["a"]["value"][column_index][
                "average_num_tokens"
            ]
            num_tokens = match_info["l"]["value"][column_index]["num_tokens"]
            if average_num_tokens == 0:
                d = 0
            else:
                d = 1 - b + (b * (float(num_tokens) / float(average_num_tokens)))

            phrase_column_x = [
                v
                for v in match_info["x"]["value"]
                if v["column_index"] == column_index
                and v["phrase_index"] == phrase_index
            ][0]
            term_frequency = float(phrase_column_x["hits_this_column_this_row"])
            docs_with_hits = float(phrase_column_x["docs_with_hits"])

            # idf = inverse document frequency: is this term rare or common
            # across our entire corpus?
            idf = max(
                math.log(
                    (total_row_count - docs_with_hits + 0.5) / (docs_with_hits + 0.5)
                ),
                0,
            )
            denom = term_frequency + (k * d)
            if denom == 0:
                rhs = 0
            else:
                rhs = (term_frequency * (k + 1)) / denom

            score += idf * rhs

    return -score
