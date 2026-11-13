import pytest
import sqlite_utils

from sqlite_history import column_value_chain, configure_history


def make_db():
    db = sqlite_utils.Database(memory=True)
    db["customers"].create(
        {"id": int, "name": str, "address": str}, pk="id"
    )
    # Existing row is captured by the backfill when tracking starts
    db["customers"].insert({"id": 1, "name": "Ada", "address": "X"})
    configure_history(db.conn, "customers")
    return db


def test_backfill_is_the_chain_origin_and_later_events_carry_forward():
    db = make_db()
    # version 2 only touches name: address must be carried forward
    db["customers"].update(1, {"name": "Ada L"})
    # version 3 explicitly changes the address
    db["customers"].update(1, {"address": "Z"})

    chain = column_value_chain(db.conn, "customers", 1, "address", 3)

    assert chain.complete is True
    assert [event.version for event in chain.events] == [1, 2, 3]
    assert [event.value for event in chain.events] == ["X", "X", "Z"]
    assert [event.changed for event in chain.events] == [True, False, True]
    assert [event.deleted for event in chain.events] == [False, False, False]
    origin = chain.origin
    assert origin.version == 3
    assert origin.value == "Z"


def test_value_changed_back_to_x_points_at_the_latest_write_not_the_first():
    db = make_db()
    db["customers"].update(1, {"address": "Y"})  # v2
    db["customers"].update(1, {"address": "X"})  # v3, back to X

    chain = column_value_chain(db.conn, "customers", 1, "address", 3)

    assert chain.complete is True
    assert [event.version for event in chain.events] == [1, 2, 3]
    assert [event.value for event in chain.events] == ["X", "Y", "X"]
    assert [event.changed for event in chain.events] == [True, True, True]
    # The origin must be v3 (the explicit change back), never v1
    assert chain.origin.version == 3
    assert chain.origin.value == "X"


def test_chain_can_be_queried_at_an_intermediate_version():
    db = make_db()
    db["customers"].update(1, {"address": "Y"})  # v2
    db["customers"].update(1, {"address": "X"})  # v3

    chain = column_value_chain(db.conn, "customers", 1, "address", 2)

    assert chain.complete is True
    assert [event.version for event in chain.events] == [1, 2]
    assert [event.value for event in chain.events] == ["X", "Y"]
    assert chain.origin.version == 2


def test_chain_stops_when_early_history_was_discarded():
    db = make_db()
    db["customers"].update(1, {"address": "Y"})  # v2
    db["customers"].update(1, {"address": "Z"})  # v3
    db["customers"].update(1, {"name": "Ada B"})  # v4, address carried

    # Library only kept part of the history: versions 1 and 2 are gone
    db.conn.execute(
        "DELETE FROM _customers_history WHERE _version < 3"
    )

    chain = column_value_chain(db.conn, "customers", 1, "address", 4)

    assert chain.complete is False
    assert [event.version for event in chain.events] == [3, 4]
    assert [event.value for event in chain.events] == ["Z", "Z"]
    assert [event.changed for event in chain.events] == [True, False]
    assert chain.origin.version == 3


def test_chain_stops_at_a_hole_in_the_middle_of_history():
    db = make_db()
    db["customers"].update(1, {"address": "Y"})  # v2
    db["customers"].update(1, {"address": "Z"})  # v3
    db["customers"].update(1, {"address": "W"})  # v4

    # Version 3 was purged, leaving a real gap between v2 and v4
    db.conn.execute(
        "DELETE FROM _customers_history WHERE _version = 3"
    )

    chain = column_value_chain(db.conn, "customers", 1, "address", 4)

    assert chain.complete is False
    assert [event.version for event in chain.events] == [4]
    assert chain.events[0].value == "W"
    assert chain.origin.version == 4


def test_explicit_null_value_is_resolved_not_treated_as_unknown():
    db = sqlite_utils.Database(memory=True)
    db["customers"].create(
        {"id": int, "name": str, "address": str}, pk="id"
    )
    # Insert with a NULL address: the insert trigger records every column
    db["customers"].insert({"id": 1, "name": "Ada", "address": None})
    configure_history(db.conn, "customers")
    db["customers"].update(1, {"name": "Ada L"})  # v2 carries NULL forward

    chain = column_value_chain(db.conn, "customers", 1, "address", 2)

    assert chain.complete is True
    assert [event.version for event in chain.events] == [1, 2]
    assert [event.value for event in chain.events] == [None, None]
    assert [event.changed for event in chain.events] == [True, False]
    assert chain.origin.version == 1
    assert chain.origin.value is None


def test_delete_event_is_part_of_the_chain():
    db = make_db()
    db["customers"].update(1, {"address": "Y"})  # v2
    db["customers"].delete(1)  # v3 deletion

    chain = column_value_chain(db.conn, "customers", 1, "address", 3)

    assert chain.complete is True
    assert [event.version for event in chain.events] == [1, 2, 3]
    assert [event.value for event in chain.events] == ["X", "Y", "Y"]
    assert [event.changed for event in chain.events] == [True, True, False]
    assert [event.deleted for event in chain.events] == [False, False, True]
    assert chain.origin.version == 3
    assert chain.origin.deleted is True


def test_error_when_history_table_missing():
    db = sqlite_utils.Database(memory=True)
    db["customers"].create({"id": int, "address": str}, pk="id")
    with pytest.raises(ValueError, match="History table"):
        column_value_chain(db.conn, "customers", 1, "address", 1)


def test_error_when_column_untracked():
    db = make_db()
    with pytest.raises(ValueError, match="not tracked"):
        column_value_chain(db.conn, "customers", 1, "nonexistent", 1)


def test_error_when_version_missing():
    db = make_db()
    with pytest.raises(ValueError, match="No history event"):
        column_value_chain(db.conn, "customers", 1, "address", 99)
