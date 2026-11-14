import sqlite3

import pytest

from sqlite_history import configure_history
from sqlite_history.merge import (
    MergeConflictError,
    create_receipt,
    merge_databases,
)


def make_pair():
    """Build an HQ database, check out a receipt, and copy it to a branch."""
    hq = sqlite3.connect(":memory:")
    hq.execute(
        "CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, city TEXT)"
    )
    configure_history(hq, "customers")
    hq.execute("INSERT INTO customers (id, name, city) VALUES (1, 'Alice', 'Berlin')")
    hq.execute("INSERT INTO customers (id, name, city) VALUES (2, 'Bob', 'London')")
    hq.commit()
    receipt_id = create_receipt(hq)
    branch = sqlite3.connect(":memory:")
    hq.backup(branch)
    return hq, branch, receipt_id


def customer(db, rowid):
    return db.execute(
        "SELECT id, name, city FROM customers WHERE rowid = ?", [rowid]
    ).fetchone()


def history_rows(db, rowid):
    return db.execute(
        "SELECT _version, _mask FROM _customers_history WHERE _rowid = ? "
        "ORDER BY _version",
        [rowid],
    ).fetchall()


def sources_for(db, receipt_id, rowid, column):
    return sorted(
        row[0]
        for row in db.execute(
            "SELECT source FROM _merge_sources WHERE receipt_id = ? "
            "AND table_name = 'customers' AND _rowid = ? AND column_name = ?",
            [receipt_id, rowid, column],
        ).fetchall()
    )


def test_independent_fields_merge():
    hq, branch, receipt_id = make_pair()
    hq.execute("UPDATE customers SET name = 'Alice HQ' WHERE id = 1")
    branch.execute("UPDATE customers SET city = 'Paris' WHERE id = 1")
    hq.commit()
    branch.commit()
    report = merge_databases(hq, branch, receipt_id)
    assert customer(hq, 1) == (1, "Alice HQ", "Paris")
    assert report.applied["update"] == 1
    assert report.conflicts == []
    assert sources_for(hq, receipt_id, 1, "name") == ["hq"]
    assert sources_for(hq, receipt_id, 1, "city") == ["branch"]


def test_same_field_same_result_keeps_one_value_two_sources():
    hq, branch, receipt_id = make_pair()
    hq.execute("UPDATE customers SET name = 'Alice Smith' WHERE id = 1")
    branch.execute("UPDATE customers SET name = 'Alice Smith' WHERE id = 1")
    hq.commit()
    branch.commit()
    report = merge_databases(hq, branch, receipt_id)
    # One business value, already correct on HQ so no update was needed
    assert customer(hq, 1) == (1, "Alice Smith", "Berlin")
    assert report.applied["update"] == 0
    assert report.conflicts == []
    # But both sides are recorded as provenance
    assert sources_for(hq, receipt_id, 1, "name") == ["branch", "hq"]


def test_delete_beats_update_regardless_of_side():
    hq, branch, receipt_id = make_pair()
    # HQ deletes row 1 while the branch edits it
    hq.execute("DELETE FROM customers WHERE id = 1")
    branch.execute("UPDATE customers SET name = 'Alice Changed' WHERE id = 1")
    # The branch deletes row 2 while HQ edits it
    branch.execute("DELETE FROM customers WHERE id = 2")
    hq.execute("UPDATE customers SET name = 'Bob Changed' WHERE id = 2")
    hq.commit()
    branch.commit()
    report = merge_databases(hq, branch, receipt_id)
    # Delete wins in both directions - the side order does not matter
    assert customer(hq, 1) is None
    assert customer(hq, 2) is None
    resolutions = {
        (c["rowid"], c["resolution"]) for c in report.conflicts
    }
    assert resolutions == {(1, "delete-wins"), (2, "delete-wins")}


def test_partial_chain_flagged_incomplete_and_not_backfilled():
    hq, branch, receipt_id = make_pair()
    branch.execute("UPDATE customers SET name = 'Alice v2' WHERE id = 1")
    branch.execute("UPDATE customers SET name = 'Alice v3' WHERE id = 1")
    branch.commit()
    # The branch only brings back part of its change chain (v2 is lost)
    branch.execute("DELETE FROM _customers_history WHERE _rowid = 1 AND _version = 2")
    branch.commit()
    hq_history_before = history_rows(hq, 1)
    report = merge_databases(hq, branch, receipt_id)
    # Flagged as incomplete evidence
    assert [(i["rowid"], i["side"]) for i in report.incomplete] == [(1, "branch")]
    stored = hq.execute(
        "SELECT side FROM _merge_incomplete WHERE receipt_id = ? AND _rowid = 1",
        [receipt_id],
    ).fetchone()
    assert stored == ("branch",)
    # Business value not applied
    assert customer(hq, 1) == (1, "Alice", "Berlin")
    # No old history fabricated from current values
    assert history_rows(hq, 1) == hq_history_before
    # The untouched row still merges fine
    assert report.applied["none"] == 1


def test_remerge_with_same_receipt_is_idempotent():
    hq, branch, receipt_id = make_pair()
    branch.execute("UPDATE customers SET city = 'Paris' WHERE id = 1")
    branch.execute("INSERT INTO customers (id, name, city) VALUES (3, 'Cara', 'Rome')")
    branch.commit()
    first = merge_databases(hq, branch, receipt_id)
    assert first.applied["update"] == 1
    assert first.applied["insert"] == 1
    business_before = hq.execute(
        "SELECT id, name, city FROM customers ORDER BY id"
    ).fetchall()
    history_before = history_rows(hq, 1) + history_rows(hq, 3)
    sources_before = hq.execute("SELECT COUNT(*) FROM _merge_sources").fetchone()[0]
    second = merge_databases(hq, branch, receipt_id)
    # No new business updates
    assert second.applied["update"] == 0
    assert second.applied["insert"] == 0
    assert second.applied["delete"] == 0
    assert (
        hq.execute("SELECT id, name, city FROM customers ORDER BY id").fetchall()
        == business_before
    )
    assert history_rows(hq, 1) + history_rows(hq, 3) == history_before
    # Provenance is not duplicated either
    assert hq.execute("SELECT COUNT(*) FROM _merge_sources").fetchone()[0] == sources_before


def test_branch_insert_merges_back():
    hq, branch, receipt_id = make_pair()
    branch.execute("INSERT INTO customers (id, name, city) VALUES (3, 'Cara', 'Rome')")
    branch.commit()
    report = merge_databases(hq, branch, receipt_id)
    assert customer(hq, 3) == (3, "Cara", "Rome")
    assert report.applied["insert"] == 1
    assert sources_for(hq, receipt_id, 3, "name") == ["branch"]


def test_conflicting_field_values_resolve_deterministically():
    hq, branch, receipt_id = make_pair()
    hq.execute("UPDATE customers SET city = 'Madrid' WHERE id = 1")
    branch.execute("UPDATE customers SET city = 'Paris' WHERE id = 1")
    hq.commit()
    branch.commit()
    report = merge_databases(hq, branch, receipt_id, on_conflict="hq")
    assert customer(hq, 1) == (1, "Alice", "Madrid")
    assert report.conflicts[0]["column"] == "city"
    assert report.conflicts[0]["resolution"] == "hq-wins"


def test_on_conflict_error_rolls_back():
    hq, branch, receipt_id = make_pair()
    hq.execute("UPDATE customers SET city = 'Madrid' WHERE id = 1")
    branch.execute("UPDATE customers SET city = 'Paris' WHERE id = 1")
    branch.execute("UPDATE customers SET name = 'Bob New' WHERE id = 2")
    hq.commit()
    branch.commit()
    with pytest.raises(MergeConflictError):
        merge_databases(hq, branch, receipt_id, on_conflict="error")
    # Nothing was applied
    assert customer(hq, 1) == (1, "Alice", "Madrid")
    assert customer(hq, 2) == (2, "Bob", "London")


def test_missing_receipt_on_branch_raises():
    hq, branch, receipt_id = make_pair()
    branch.execute("DELETE FROM _checkout_receipts")
    branch.commit()
    with pytest.raises(ValueError, match="receipt"):
        merge_databases(hq, branch, receipt_id)
