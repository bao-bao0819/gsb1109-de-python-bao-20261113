import sqlite3

import pytest

from sqlite_fts4 import register_functions
from sqlite_fts4.migration import (
    EXACT,
    BOUNDED,
    CONTROL_ONLY,
    annotated_matchinfo_rows,
    average_length_bounds,
    build_comparison,
    column_docs_per_phrase,
    count_docs_via_bitfield,
    count_docs_with_hits,
    docs_bounds,
    merge_hits,
    phrase_column_x,
    recombine_columns,
    render_markdown,
    validate_comparison,
)


sqlite_version = tuple(
    map(
        int,
        sqlite3.connect(":memory:")
        .execute("select sqlite_version()")
        .fetchone()[0]
        .split("."),
    )
)

needs_bitfield = pytest.mark.skipif(
    sqlite_version < (3, 8, 11), reason="matchinfo 'b' was added in SQLite 3.8.11"
)

# rowid -> (摘要 summary, 详情 detail)；row 5 同时在两栏命中 dog/cat
DOCS = {
    1: ("dog", "cat dog bird"),
    2: ("dog fish", ""),
    3: ("cat", "cat bird"),
    4: ("", "only fish here"),
    5: ("dog cat", "dog dog cat"),
}


@pytest.fixture
def conn():
    conn = sqlite3.connect(":memory:")
    register_functions(conn)
    conn.executescript(
        "create virtual table old using fts4(body);"
        "create virtual table new using fts4(summary, detail);"
    )
    for rowid, (summary, detail) in DOCS.items():
        conn.execute(
            "insert into old(rowid, body) values (?, ?)",
            (rowid, (summary + " " + detail).strip()),
        )
        conn.execute(
            "insert into new(rowid, summary, detail) values (?, ?, ?)",
            (rowid, summary, detail),
        )
    recombine_columns(conn, "new", ["summary", "detail"], "recombined")
    return conn


def test_recombined_table_has_same_rows(conn):
    rows = conn.execute(
        "select rowid, body from recombined order by rowid"
    ).fetchall()
    old_rows = conn.execute(
        "select rowid, body from old order by rowid"
    ).fetchall()
    assert [r[0] for r in rows] == [r[0] for r in old_rows]


@pytest.mark.parametrize(
    "query",
    ["dog", "cat", "dog cat", "bird fish", "dog bird", "ferret"],
)
@pytest.mark.parametrize("fmt", ["pcx", "pcxnal", "pcnalx", "pcxnalyb", "pcxnals"])
def test_recombined_matchinfo_byte_identical(conn, query, fmt):
    old = [
        buf
        for (buf,) in conn.execute(
            "select matchinfo(old, ?) from old where old match ? order by rowid",
            [fmt, query],
        )
    ]
    rec = [
        buf
        for (buf,) in conn.execute(
            "select matchinfo(recombined, ?) from recombined "
            "where recombined match ? order by rowid",
            [fmt, query],
        )
    ]
    assert old == rec


def test_merge_hits_sums_column_hits(conn):
    rows = annotated_matchinfo_rows(conn, "new", "dog")
    merged = merge_hits(rows[0][1])
    old_rows = annotated_matchinfo_rows(conn, "old", "dog")
    old_x = phrase_column_x(old_rows[0][1])[0][0]
    assert merged[0]["row_hits"] == old_x["hits_this_column_this_row"]
    assert merged[0]["global_hits"] == old_x["hits_this_column_all_rows"]


def test_merged_row_length_matches_old(conn):
    for rowid in (1, 2, 5):
        new = annotated_matchinfo_rows(conn, "new", "dog")
        old = dict(annotated_matchinfo_rows(conn, "old", "dog"))
        new_by_id = dict(new)
        assert sum(v["num_tokens"] for v in new_by_id[rowid]["l"]["value"]) == (
            old[rowid]["l"]["value"][0]["num_tokens"]
        )


@needs_bitfield
def test_docs_with_hits_exact_via_match_and_bitfield(conn):
    old_rows = dict(annotated_matchinfo_rows(conn, "old", "dog"))
    old_docs = phrase_column_x(old_rows[1])[0][0]["docs_with_hits"]
    assert count_docs_with_hits(conn, "new", "dog") == old_docs
    assert count_docs_via_bitfield(conn, "new", "dog") == old_docs


@needs_bitfield
def test_docs_bounds_without_intersection_evidence(conn):
    # row 5 同时命中两栏，各栏 docs 之和高估，界限必须包含真实并集
    rows = dict(annotated_matchinfo_rows(conn, "new", "dog"))
    col_docs = column_docs_per_phrase(rows[1])[0]
    lower, upper = docs_bounds(col_docs, len(DOCS))
    truth = count_docs_with_hits(conn, "new", "dog")
    assert lower <= truth <= upper
    assert lower == truth  # 下界恒等于真实值
    assert upper > truth   # 上界因两栏交集未知而宽松


def test_docs_bounds_disjoint_case_is_tight():
    # 两栏命中文档不相交时上界收紧到真实值
    assert docs_bounds([2, 3], 10) == (3, 5)
    # 全集上限截断
    assert docs_bounds([4, 4], 5) == (4, 5)


def test_average_length_bounds_contains_control(conn):
    new_rows = dict(annotated_matchinfo_rows(conn, "new", "dog"))
    control_rows = dict(annotated_matchinfo_rows(conn, "recombined", "dog"))
    col_avgs = [
        entry["average_num_tokens"]
        for entry in new_rows[1]["a"]["value"]
    ]
    control_avg = control_rows[1]["a"]["value"][0]["average_num_tokens"]
    lower, upper = average_length_bounds(col_avgs)
    assert lower <= control_avg <= upper
    # a 是整数取整：两栏均长之和的小数部分无法从拆栏恢复
    assert upper == lower + 1


@needs_bitfield
@pytest.mark.parametrize("query,phrases", [("dog", ["dog"]), ("dog cat", ["dog", "cat"])])
def test_build_comparison_validates(conn, query, phrases):
    records = build_comparison(conn, "old", "new", "recombined", query, phrases)
    exact, bounded, control_only = validate_comparison(records)
    assert exact > 0 and bounded > 0 and control_only > 0

    by_status = {}
    for record in records:
        by_status.setdefault(record["status"], []).append(record)

    # 精确恢复：行内/全库词次、行长度、命中文档数（含跨栏去重）
    exact_metrics = {r["metric"] for r in by_status[EXACT]}
    assert "x 行内词次" in exact_metrics
    assert "x 全库总词次" in exact_metrics
    assert "l 行长度（词次）" in exact_metrics
    assert "x 命中文档数（b 位域跨栏去重）" in exact_metrics

    # 界限类：无交集证据的命中文档数、整数取整的平均列长
    bounded_metrics = {r["metric"] for r in by_status[BOUNDED]}
    assert "x 命中文档数（仅 x，无交集证据）" in bounded_metrics
    assert "a 平均列长（整数取整）" in bounded_metrics
    for record in by_status[BOUNDED]:
        lower, upper = record["bounds"]
        assert lower <= record["control"] <= upper

    # 仅对照可复核：s 跨栏子序列、两种评分
    control_metrics = {r["metric"] for r in by_status[CONTROL_ONLY]}
    assert "s 最长匹配子序列" in control_metrics
    assert "rank_bm25 评分" in control_metrics
    assert "rank_score 评分" in control_metrics
    for record in by_status[CONTROL_ONLY]:
        assert record["derived"] is None
        assert record["historical"] == record["control"]


@needs_bitfield
def test_comparison_counts_overlapping_document_once(conn):
    records = build_comparison(
        conn, "old", "new", "recombined", "dog cat", ["dog", "cat"]
    )
    docs_records = [
        r
        for r in records
        if r["metric"] == "x 命中文档数（单短语 MATCH 精确去重）"
    ]
    # dog 与 cat 都命中 row 1/3/5 共 3 篇；row 5 虽两栏同中，只计一篇
    assert {r["historical"] for r in docs_records} == {3}
    assert {r["derived"] for r in docs_records} == {3}


@needs_bitfield
def test_render_markdown_table(conn):
    records = build_comparison(
        conn, "old", "new", "recombined", "dog", ["dog"]
    )
    markdown = render_markdown(records, "dog")
    assert markdown.startswith("### 查询：`dog`")
    assert "历史库（单栏正文）" in markdown
    assert "合栏对照" in markdown
    assert "精确恢复，与对照一致" in markdown
    assert "界限包含对照值" in markdown
    # 表格行数 = 表头行 + 分隔行 + 记录行
    assert markdown.count("\n|") == len(records) + 2


def test_build_comparison_raises_on_divergent_corpus(conn):
    conn.execute("insert into old(rowid, body) values (99, 'extra dog doc')")
    with pytest.raises(ValueError):
        build_comparison(
            conn, "old", "new", "recombined", "dog", ["dog"]
        )
