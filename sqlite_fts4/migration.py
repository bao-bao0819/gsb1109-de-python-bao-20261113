"""FTS4 索引升级（单栏正文 -> 摘要/详情双栏）的可比性工具。

场景：FTS4 索引把单栏 ``body``（正文）拆成 ``summary``（摘要）和
``detail``（详情）两栏，历史库只留存旧单栏表的 ``matchinfo`` 二进制，
需要给月报出具一张升级前后的可比性对照。

matchinfo 各段从两栏恢复到单栏时的性质（栏数 c=2）：

* ``p`` / ``n``：与拆栏无关，直接精确恢复。
* ``x`` 的行内词次、全库词次：词次可加，按短语对两栏求和即可精确
  恢复（总词次数在映射明确时可以合并）。
* ``x`` 的命中文档数：一篇稿同时命中两栏只该算一个命中文档。有
  ``b`` 位域、或对每个短语单独 MATCH 时可以精确去重；若只保留了多
  短语查询的 ``x``，没有交集证据，只能给界限
  ``[max(d_i), min(n, sum(d_i))]``。
* ``l`` 行长度（词次）：可加，精确恢复。
* ``a`` 平均列长：FTS4 存的是向下取整的整数，合栏均值是两栏真值之
  和，只能给界限 ``[sum(a_i), sum(a_i) + c - 1]``，精确值靠合栏对照。
* ``s`` 最长匹配子序列：跨栏边界的信息丢失，无法从拆栏 matchinfo
  恢复，只能在合栏对照里复核。

把两栏重新拼成单栏正文建一张对照 FTS4 表，其 ``matchinfo`` 与历史
库逐字节一致，因此所有指标（含 ``rank_bm25`` / ``rank_score``）都能
在小样本对照中精确复核。
"""

import sqlite3

from . import _annotate_matchinfo

EXACT = "exact"
BOUNDED = "bounded"
CONTROL_ONLY = "control_only"

STATUS_TITLE = {
    EXACT: "精确恢复，与对照一致",
    BOUNDED: "无法精确恢复，界限包含对照值",
    CONTROL_ONLY: "拆栏信息丢失，仅合栏对照可复核",
}

DEFAULT_FORMAT = "pcxnalybs"


def annotated_matchinfo(conn, table, query, fmt=DEFAULT_FORMAT):
    """返回查询命中的第一行的注释版 matchinfo，无命中时返回 None。"""
    row = conn.execute(
        "select matchinfo({table}, ?) from {table} where {table} match ? "
        "limit 1".format(table=table),
        [fmt, query],
    ).fetchone()
    if row is None:
        return None
    return _annotate_matchinfo(row[0], fmt)


def annotated_matchinfo_rows(conn, table, query, fmt=DEFAULT_FORMAT):
    """按 rowid 顺序返回 (rowid, 注释版 matchinfo) 列表。"""
    rows = conn.execute(
        "select rowid, matchinfo({table}, ?) from {table} "
        "where {table} match ? order by rowid".format(table=table),
        [fmt, query],
    ).fetchall()
    return [(rowid, _annotate_matchinfo(buf, fmt)) for rowid, buf in rows]


def recombine_columns(conn, source, columns, target, body="body"):
    """把多栏 FTS4 表重新拼成单栏正文表（小样本对照）。

    拼接时的额外空格不影响 FTS4 分词，因此对照表与旧单栏表的
    matchinfo 逐字节一致。
    """
    conn.execute("create virtual table {target} using fts4({body})".format(
        target=target, body=body))
    expression = " || ' ' || ".join(
        "coalesce({col}, '')".format(col=col) for col in columns)
    conn.execute(
        "insert into {target}(rowid, {body}) "
        "select rowid, {expression} from {source}".format(
            target=target, body=body, expression=expression, source=source))
    return target


def phrase_column_x(annotated):
    """把 x 段整理为 {phrase_index: {column_index: x 详情}}。"""
    grouped = {}
    for entry in annotated["x"]["value"]:
        grouped.setdefault(entry["phrase_index"], {})[entry["column_index"]] = entry
    return grouped


def merge_hits(annotated):
    """每个短语的行内词次与全库词次：两栏求和，精确恢复。"""
    merged = {}
    for phrase_index, columns in phrase_column_x(annotated).items():
        merged[phrase_index] = {
            "row_hits": sum(
                v["hits_this_column_this_row"] for v in columns.values()),
            "global_hits": sum(
                v["hits_this_column_all_rows"] for v in columns.values()),
        }
    return merged


def column_docs_per_phrase(annotated):
    """每个短语在各栏的 docs_with_hits：{phrase_index: [d_col0, d_col1]}。"""
    grouped = phrase_column_x(annotated)
    return {
        phrase_index: [
            columns[column_index]["docs_with_hits"]
            for column_index in sorted(columns)
        ]
        for phrase_index, columns in grouped.items()
    }


def columns_hit_by_phrase(annotated):
    """从 b 位域读出每个短语在当前行命中了哪些栏。

    一篇稿同时命中摘要和详情时，两个栏位都为 1，但命中文档只能计一次。
    """
    column_count = annotated["c"]["value"]
    hit_columns = {}
    for key, bits in annotated["b"]["decoded"].items():
        phrase_index = int(key.split("_")[1])
        hit_columns[phrase_index] = [
            column_index
            for column_index in range(column_count)
            if bits[column_index] == "1"
        ]
    return hit_columns


def count_docs_with_hits(conn, table, phrase):
    """对单个短语单独 MATCH，精确统计命中文档数（自动跨栏去重）。"""
    return conn.execute(
        "select count(*) from {table} where {table} match ?".format(table=table),
        [phrase],
    ).fetchone()[0]


def count_docs_via_bitfield(conn, table, phrase):
    """用 b 位域对单短语查询逐文档去重，精确统计命中文档数。"""
    rows = annotated_matchinfo_rows(conn, table, phrase, fmt="pcb")
    return sum(
        1 for _, annotated in rows
        if columns_hit_by_phrase(annotated).get(0))


def docs_bounds(col_docs, n_rows):
    """无交集证据时命中文档数的界限。

    各栏命中文档集合大小为 d_i、全集为 n 个文档，并集满足
    max(d_i) <= |并集| <= min(n, sum(d_i))。
    """
    lower = max(col_docs)
    upper = min(n_rows, sum(col_docs))
    return lower, upper


def average_length_bounds(col_avgs):
    """合栏平均列长（a 段）的界限。

    FTS4 的 a 是向下取整的整数。设两栏真值均值为 mu_i，存值
    a_i = floor(mu_i)，则合栏存值 floor(mu_0 + mu_1) 落在
    [sum(a_i), sum(a_i) + c - 1]。
    """
    total = sum(col_avgs)
    return total, total + len(col_avgs) - 1


def merged_row_length(annotated):
    """当前行各栏词次长度求和，精确恢复旧单栏行长度。"""
    return sum(entry["num_tokens"] for entry in annotated["l"]["value"])


def _record(metric, level, historical, derived, control, status,
            phrase=None, rowid=None, bounds=None):
    return {
        "metric": metric,
        "level": level,
        "phrase": phrase,
        "rowid": rowid,
        "historical": historical,
        "derived": derived,
        "bounds": bounds,
        "control": control,
        "status": status,
    }


def _score_rows(conn, table, query):
    rows = conn.execute(
        "select rowid, rank_bm25(matchinfo({t}, 'pcnalx')), "
        "rank_score(matchinfo({t}, 'pcx')) from {t} where {t} match ? "
        "order by rowid".format(t=table),
        [query],
    ).fetchall()
    return {rowid: (bm25, score) for rowid, bm25, score in rows}


def build_comparison(conn, old_table, new_table, recombined_table, query,
                     phrases):
    """构造一条查询在三张表上的可比性对照记录列表。

    old_table：历史库单栏正文表；new_table：升级后的摘要/详情双栏表；
    recombined_table：由双栏合回一栏的小样本对照表；phrases：查询里
    的短语（FTS4 隐式 AND）。
    """
    old_rows = dict(annotated_matchinfo_rows(conn, old_table, query))
    new_rows = dict(annotated_matchinfo_rows(conn, new_table, query))
    control_rows = dict(annotated_matchinfo_rows(conn, recombined_table, query))
    if not old_rows:
        raise ValueError("查询 {!r} 没有命中文档，无法生成对照".format(query))
    if set(old_rows) != set(new_rows) or set(old_rows) != set(control_rows):
        raise ValueError(
            "三张表的命中文档集合不一致：{} / {} / {}".format(
                set(old_rows), set(new_rows), set(control_rows)))

    rowids = sorted(old_rows)
    first_old = old_rows[rowids[0]]
    first_new = new_rows[rowids[0]]
    first_control = control_rows[rowids[0]]
    n_rows = first_old["n"]["value"]
    records = []

    records.append(_record(
        "p 可匹配短语数", "corpus",
        first_old["p"]["value"], first_new["p"]["value"],
        first_control["p"]["value"], EXACT))
    records.append(_record(
        "n 表内文档总数", "corpus",
        n_rows, first_new["n"]["value"],
        first_control["n"]["value"], EXACT))
    records.append(_record(
        "整查询命中文档数（AND 去重）", "corpus",
        len(rowids), len(new_rows), len(control_rows), EXACT))

    old_x = phrase_column_x(first_old)
    new_x = phrase_column_x(first_new)
    control_x = phrase_column_x(first_control)
    new_col_docs = column_docs_per_phrase(first_new)
    new_a = [entry["average_num_tokens"] for entry in first_new["a"]["value"]]

    for phrase_index, phrase in enumerate(phrases):
        old_global = old_x[phrase_index][0]["hits_this_column_all_rows"]
        new_global = merge_hits(first_new)[phrase_index]["global_hits"]
        control_global = control_x[phrase_index][0]["hits_this_column_all_rows"]
        records.append(_record(
            "x 全库总词次", "corpus", old_global, new_global,
            control_global, EXACT, phrase=phrase))

        old_docs = old_x[phrase_index][0]["docs_with_hits"]
        exact_docs = count_docs_with_hits(conn, new_table, phrase)
        bitfield_docs = count_docs_via_bitfield(conn, new_table, phrase)
        bounds = docs_bounds(new_col_docs[phrase_index], n_rows)
        control_docs = control_x[phrase_index][0]["docs_with_hits"]
        records.append(_record(
            "x 命中文档数（单短语 MATCH 精确去重）", "corpus",
            old_docs, exact_docs, control_docs, EXACT, phrase=phrase))
        records.append(_record(
            "x 命中文档数（b 位域跨栏去重）", "corpus",
            old_docs, bitfield_docs, control_docs, EXACT, phrase=phrase))
        records.append(_record(
            "x 命中文档数（仅 x，无交集证据）", "corpus",
            old_docs, None, control_docs, BOUNDED,
            phrase=phrase, bounds=bounds))

        for rowid in rowids:
            old_row_hits = old_rows[rowid]["x"]["value"]
            old_row_hits = next(
                v["hits_this_column_this_row"]
                for v in old_row_hits
                if v["phrase_index"] == phrase_index)
            new_row_hits = merge_hits(new_rows[rowid])[phrase_index]["row_hits"]
            control_row_hits = next(
                v["hits_this_column_this_row"]
                for v in control_rows[rowid]["x"]["value"]
                if v["phrase_index"] == phrase_index)
            records.append(_record(
                "x 行内词次", "row", old_row_hits, new_row_hits,
                control_row_hits, EXACT,
                phrase=phrase, rowid=rowid))

    for rowid in rowids:
        old_length = old_rows[rowid]["l"]["value"][0]["num_tokens"]
        new_length = merged_row_length(new_rows[rowid])
        control_length = control_rows[rowid]["l"]["value"][0]["num_tokens"]
        records.append(_record(
            "l 行长度（词次）", "row", old_length, new_length,
            control_length, EXACT, rowid=rowid))

        old_subsequence = old_rows[rowid]["s"]["value"][0][
            "length_phrase_subsequence_match"]
        control_subsequence = control_rows[rowid]["s"]["value"][0][
            "length_phrase_subsequence_match"]
        records.append(_record(
            "s 最长匹配子序列", "row", old_subsequence, None,
            control_subsequence, CONTROL_ONLY, rowid=rowid))

    old_average = first_old["a"]["value"][0]["average_num_tokens"]
    control_average = first_control["a"]["value"][0]["average_num_tokens"]
    records.append(_record(
        "a 平均列长（整数取整）", "corpus", old_average, None,
        control_average, BOUNDED,
        bounds=average_length_bounds(new_a)))

    old_scores = _score_rows(conn, old_table, query)
    control_scores = _score_rows(conn, recombined_table, query)
    for rowid in rowids:
        records.append(_record(
            "rank_bm25 评分", "row", old_scores[rowid][0], None,
            control_scores[rowid][0], CONTROL_ONLY, rowid=rowid))
        # rank_score = -sum(行内词次/全库词次)：合栏是“和之比”，不等于
        # 两栏“比之和”，故只能在合栏对照中复核。
        records.append(_record(
            "rank_score 评分", "row", old_scores[rowid][1], None,
            control_scores[rowid][1], CONTROL_ONLY, rowid=rowid))

    return records


def validate_comparison(records):
    """校验对照不变量，返回 (精确一致条数, 界限包含条数, 仅对照条数)。

    * 精确恢复：历史值、升级推导值、合栏对照值三者相等；
    * 界限类：历史值与对照值相等，且都落在界限内；
    * 仅对照类：历史值与对照值相等。
    """
    exact = bounded = control_only = 0
    for record in records:
        status = record["status"]
        if status == EXACT:
            if not (record["historical"] == record["derived"]
                    == record["control"]):
                raise AssertionError("精确恢复指标不一致: {}".format(record))
            exact += 1
        elif status == BOUNDED:
            lower, upper = record["bounds"]
            value = record["control"]
            if not (record["historical"] == value
                    and lower <= value <= upper):
                raise AssertionError("界限未包含对照值: {}".format(record))
            bounded += 1
        else:
            if record["historical"] != record["control"]:
                raise AssertionError("对照值与历史值不一致: {}".format(record))
            control_only += 1
    return exact, bounded, control_only


def render_markdown(records, query):
    """把对照记录渲染成月报可用的 Markdown 表格。"""
    lines = [
        "### 查询：`{}`".format(query),
        "",
        "| 指标 | 层级 | 短语 | 文档 | 历史库（单栏正文） | 升级推导 | 界限 | 合栏对照 | 结论 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]

    def fmt(value):
        if value is None:
            return "—"
        if isinstance(value, float):
            return "{:.6f}".format(value)
        return str(value)

    for record in records:
        bounds = "—"
        if record["bounds"] is not None:
            bounds = "[{}, {}]".format(*record["bounds"])
        lines.append("| {} | {} | {} | {} | {} | {} | {} | {} | {} |".format(
            record["metric"],
            record["level"],
            record["phrase"] if record["phrase"] is not None else "—",
            record["rowid"] if record["rowid"] is not None else "—",
            fmt(record["historical"]),
            fmt(record["derived"]),
            bounds,
            fmt(record["control"]),
            STATUS_TITLE[record["status"]],
        ))
    return "\n".join(lines)
