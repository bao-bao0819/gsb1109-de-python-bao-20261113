import datetime
import sqlite3

import pytest

from sqlite_fts4.mentions import (
    Alias,
    Dictionary,
    DictionaryEntry,
    DocMeta,
    Hit,
    bounds_from_aggregates,
    count_from_fts,
    fetch_hits,
    load_doc_meta,
    _apply_containment,
)


def build_db():
    """Spaced CJK so the stock unicode61/simple tokenizers index each term."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE VIRTUAL TABLE articles_fts USING fts4(title, body);
        CREATE TABLE articles (
            id INTEGER PRIMARY KEY,
            doc_id TEXT,
            pub_date TEXT,
            title TEXT,
            body TEXT
        );
        """
    )
    rows = [
        # doc_id, date, title, body
        ("A", "2024-01-01", "甲二代 发布", "甲二代 热销 中"),
        ("B", "2024-02-01", "甲 深度 评测", "甲 与 甲二代 同台 对比"),
        ("C", "2024-03-01", "甲二代", "甲二代 正式 上市"),
        ("D", "2024-04-01", "甲", "甲 甲 依然 在售"),
        ("E", "2025-07-01", "代号甲 上线", "代号甲 开始 铺货"),
        ("F", "2025-01-01", "代号甲 传闻", "代号甲 尚未 定名"),
        ("G", None, "甲 快讯", "甲 相关 报道"),
    ]
    for rowid, (doc_id, day, title, body) in enumerate(rows, start=1):
        conn.execute(
            "INSERT INTO articles_fts (rowid, title, body) VALUES (?, ?, ?)",
            (rowid, title, body),
        )
        conn.execute(
            "INSERT INTO articles (id, doc_id, pub_date, title, body) "
            "VALUES (?, ?, ?, ?, ?)",
            (rowid, doc_id, day, title, body),
        )
    return conn


def build_dictionary():
    return Dictionary.from_dicts(
        [
            {
                "canonical": "甲",
                "aliases": [
                    {"text": "甲"},
                    {"text": "代号甲", "valid_from": "2025-06-01"},
                ],
                "contained_by": ["甲二代"],
            },
            {"canonical": "甲二代", "aliases": [{"text": "甲二代"}]},
        ]
    )


@pytest.fixture
def conn():
    return build_db()


@pytest.fixture
def dictionary():
    return build_dictionary()


@pytest.fixture
def docs(conn):
    return load_doc_meta(conn, "articles", "pub_date", id_column="doc_id")


def test_dictionary_validates_duplicate_alias():
    with pytest.raises(ValueError):
        Dictionary.from_dicts(
            [
                {"canonical": "x", "aliases": [{"text": "共用"}]},
                {"canonical": "y", "aliases": [{"text": "共用"}]},
            ]
        )


def test_dictionary_validates_unknown_containment_and_cycles():
    with pytest.raises(ValueError):
        Dictionary.from_dicts(
            [{"canonical": "x", "aliases": [{"text": "x"}], "contained_by": ["z"]}]
        )
    with pytest.raises(ValueError):
        Dictionary(
            [
                DictionaryEntry(
                    canonical="x", aliases=[Alias("x")], contained_by=["y"]
                ),
                DictionaryEntry(
                    canonical="y", aliases=[Alias("y")], contained_by=["x"]
                ),
            ]
        )


def test_alias_validity_window():
    alias = Alias("代号甲", valid_from=datetime.date(2025, 6, 1))
    assert not alias.valid_on(datetime.date(2025, 5, 31))
    assert alias.valid_on(datetime.date(2025, 6, 1))
    assert alias.valid_on(datetime.date(2026, 1, 1))


def test_fetch_hits_distinguishes_unsearchable_cjk():
    # Detection works at term granularity, so use a corpus where the term only
    # ever occurs inside unsegmented CJK runs.
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE VIRTUAL TABLE fts USING fts4(title, body)")
    conn.execute(
        "INSERT INTO fts (rowid, title, body) VALUES (?, ?, ?)",
        (1, "无分词连续汉字标题", "正文包含连续词甲二代无空格"),
    )
    hits_by_term, unsearchable = fetch_hits(conn, "fts", ["甲二代"])
    assert hits_by_term["甲二代"] == []
    assert unsearchable == ["甲二代"]


def test_exact_counts_distinct_documents(conn, dictionary, docs):
    reports = count_from_fts(conn, "articles_fts", dictionary, docs)
    jia = reports["甲"]
    gen2 = reports["甲二代"]

    # B (standalone 甲), D (title+body still one document), E (alias in window)
    assert jia.exact == 3
    assert sorted(jia.doc_ids) == ["B", "D", "E"]
    # F used the alias before its valid_from date; G has no publication date
    assert gen2.exact == 3
    assert sorted(gen2.doc_ids) == ["A", "B", "C"]
    for report in (jia, gen2):
        assert report.exact_available
        assert report.lower_bound == report.upper_bound == report.exact


def test_undated_document_is_flagged(conn, dictionary, docs):
    reports = count_from_fts(conn, "articles_fts", dictionary, docs)
    note = " ".join(reports["甲"].notes)
    assert "G" not in reports["甲"].doc_ids
    assert "publication date unavailable" in note


def test_non_overlapping_short_and_long_both_count(conn, dictionary, docs):
    # In B the standalone 甲 token precedes 甲二代; offsets show separate spans,
    # so containment must not swallow it.
    reports = count_from_fts(conn, "articles_fts", dictionary, docs)
    assert "B" in reports["甲"].doc_ids
    assert "B" in reports["甲二代"].doc_ids
    assert reports["甲"].suppressed == []


def test_first_mention_is_verifiable(conn, dictionary, docs):
    reports = count_from_fts(conn, "articles_fts", dictionary, docs)
    first_gen2 = reports["甲二代"].first_mention
    assert first_gen2.doc_id == "A"
    assert first_gen2.date == datetime.date(2024, 1, 1)
    assert first_gen2.column == "title"
    assert first_gen2.alias == "甲二代"
    assert first_gen2.char_offset >= 0
    assert "甲二代" in first_gen2.snippet

    first_jia = reports["甲"].first_mention
    assert first_jia.doc_id == "B"
    assert first_jia.date == datetime.date(2024, 2, 1)


def test_columns_restriction_ignores_title(conn, dictionary, docs):
    reports = count_from_fts(
        conn, "articles_fts", dictionary, docs, columns=["body"]
    )
    # C's only standalone content: title is 甲二代; body still has 甲二代,
    # while D's body contains 甲 and its title-only duplicate collapses anyway.
    assert "D" in reports["甲"].doc_ids
    assert reports["甲二代"].doc_ids.count("A") == 1


def test_containment_suppresses_document_when_all_hits_nested(dictionary):
    candidates = {
        "甲": {
            1: [
                Hit(1, "body", 1, byte_offset=10, byte_length=3, alias="甲"),
            ]
        },
        "甲二代": {
            1: [
                Hit(1, "body", 1, byte_offset=10, byte_length=9, alias="甲二代"),
            ]
        },
    }
    meta = {1: DocMeta(rowid=1, date=datetime.date(2024, 1, 1), doc_id="X")}
    suppressions = _apply_containment(dictionary, candidates, meta)
    assert 1 not in candidates["甲"]
    assert candidates["甲二代"][1]
    assert suppressions[0].sub_concept == "甲"
    assert suppressions[0].super_concept == "甲二代"
    assert suppressions[0].doc_id == "X"


def test_containment_keeps_document_with_standalone_hit(dictionary):
    candidates = {
        "甲": {
            1: [
                Hit(1, "body", 1, byte_offset=0, byte_length=3, alias="甲"),
                Hit(1, "body", 1, byte_offset=20, byte_length=3, alias="甲"),
            ]
        },
        "甲二代": {
            1: [
                Hit(1, "body", 1, byte_offset=20, byte_length=9, alias="甲二代"),
            ]
        },
    }
    meta = {1: DocMeta(rowid=1, date=datetime.date(2024, 1, 1), doc_id="X")}
    _apply_containment(dictionary, candidates, meta)
    # Document stays credited to 甲, but only the standalone occurrence remains.
    assert len(candidates["甲"][1]) == 1
    assert candidates["甲"][1][0].byte_offset == 0


def test_transitive_containment_applies_through_chain():
    chain = Dictionary.from_dicts(
        [
            {"canonical": "甲", "aliases": [{"text": "甲"}], "contained_by": ["甲二代"]},
            {
                "canonical": "甲二代",
                "aliases": [{"text": "甲二代"}],
                "contained_by": ["甲三代"],
            },
            {"canonical": "甲三代", "aliases": [{"text": "甲三代"}]},
        ]
    )
    candidates = {
        "甲": {1: [Hit(1, "body", 1, 0, 3, "甲")]},
        "甲二代": {1: [Hit(1, "body", 1, 0, 9, "甲二代")]},
        "甲三代": {1: [Hit(1, "body", 1, 0, 12, "甲三代")]},
    }
    meta = {1: DocMeta(rowid=1, date=datetime.date(2024, 1, 1), doc_id="X")}
    suppressions = _apply_containment(chain, candidates, meta)
    assert 1 not in candidates["甲"]
    assert 1 not in candidates["甲二代"]
    assert 1 in candidates["甲三代"]
    pairs = {(s.sub_concept, s.super_concept) for s in suppressions}
    assert {sub for sub, _ in pairs} == {"甲", "甲二代"}
    # Every recorded suppression follows a (transitive) dictionary edge.
    assert all(super_ in chain.super_closure()[sub] for sub, super_ in pairs)


def test_aggregate_bounds_from_alias_counts(dictionary):
    reports = bounds_from_aggregates(
        dictionary,
        alias_doc_counts={
            ("甲", "甲"): 10,
            ("甲", "代号甲"): 4,
            ("甲二代", "甲二代"): 7,
        },
        corpus_size=12,
    )
    jia = reports["甲"]
    assert jia.exact is None
    assert not jia.exact_available
    assert jia.lower_bound == 10  # largest single alias
    assert jia.upper_bound == 12  # 10 + 4, capped at corpus size
    assert any("aggregate-only" in note for note in jia.notes)
    assert any("containment" in note for note in jia.notes)
    gen2 = reports["甲二代"]
    assert (gen2.lower_bound, gen2.upper_bound) == (7, 7)


def test_aggregate_bounds_with_totals_only(dictionary):
    reports = bounds_from_aggregates(
        dictionary,
        concept_totals={"甲": 13, "甲二代": 7},
    )
    jia = reports["甲"]
    assert jia.exact is None
    assert (jia.lower_bound, jia.upper_bound) == (0, 13)
    assert any("upper bound only" in note for note in jia.notes)


def test_aggregate_flags_inconsistent_retained_totals(dictionary):
    reports = bounds_from_aggregates(
        dictionary,
        alias_doc_counts={("甲二代", "甲二代"): 7},
        concept_totals={"甲二代": 99},
    )
    assert any("contradict" in note for note in reports["甲二代"].notes)


def test_aggregate_first_reference_requires_doc_id(dictionary):
    reports = bounds_from_aggregates(
        dictionary,
        alias_doc_counts={("甲二代", "甲二代"): 7},
        first_refs={
            ("甲二代", "甲二代"): {
                "doc_id": "A",
                "date": "2024-01-01",
                "column": "title",
            }
        },
    )
    first = reports["甲二代"].first_mention
    assert first.doc_id == "A"
    assert first.date == datetime.date(2024, 1, 1)

    reports_no_id = bounds_from_aggregates(
        dictionary,
        alias_doc_counts={("甲二代", "甲二代"): 7},
        first_refs={("甲二代", "甲二代"): {"date": "2024-01-01"}},
    )
    report = reports_no_id["甲二代"]
    assert report.first_mention.doc_id is None
    assert any("cannot be independently checked" in n for n in report.notes)


def test_aggregate_no_retained_statistics(dictionary):
    reports = bounds_from_aggregates(dictionary)
    for report in reports.values():
        assert report.exact is None
        assert (report.lower_bound, report.upper_bound) == (0, 0)
        assert any("no retained statistics" in note for note in report.notes)
