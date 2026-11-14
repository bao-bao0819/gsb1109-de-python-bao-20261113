"""Build a repair-station device renumbering case with sqlite-history triggers.

Scenarios embedded:
  1. In-place primary-key change        A001 -> A002 (same rowid chain)
  2. Delete then rowid reuse            E001 deleted; E002 inserted reusing rowid
  3. Two devices swapping business nos  C001 <-> D001 via a temporary key
  4. Two forms claiming one successor   F3 and F4 both name C002
"""
import sqlite3

from sqlite_history import configure_history

DB_PATH = "audit/repairs.db"


def build(db_path=DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        DROP TABLE IF EXISTS devices;
        DROP TABLE IF EXISTS handover_forms;
        DROP TABLE IF EXISTS _devices_history;
        DROP TABLE IF EXISTS _handover_forms_history;
        CREATE TABLE devices (
            biz_no TEXT PRIMARY KEY,
            model TEXT,
            status TEXT
        );
        CREATE TABLE handover_forms (
            form_id TEXT PRIMARY KEY,
            predecessor_no TEXT,
            successor_no TEXT,
            issued_at TEXT
        );
        """
    )
    configure_history(conn, "devices")

    cur = conn.cursor()
    cur.executemany(
        "INSERT INTO devices (biz_no, model, status) VALUES (?, ?, ?)",
        [
            ("A001", "泵车", "在用"),
            ("B001", "吊车", "在用"),
            ("C001", "叉车", "在用"),
            ("D001", "铲车", "在用"),
            ("E001", "钻机", "在用"),
        ],
    )
    conn.commit()

    # 1. In-place primary key change, then a normal attribute update.
    cur.execute("UPDATE devices SET biz_no = 'A002' WHERE biz_no = 'A001'")
    cur.execute("UPDATE devices SET status = '维修中' WHERE biz_no = 'A002'")

    # 3. Swap business numbers of two physical devices through a temporary key.
    cur.execute("UPDATE devices SET biz_no = 'TMP9' WHERE biz_no = 'C001'")
    cur.execute("UPDATE devices SET biz_no = 'C001' WHERE biz_no = 'D001'")
    cur.execute("UPDATE devices SET biz_no = 'D001' WHERE biz_no = 'TMP9'")

    # 2. Delete the highest-rowid device, then insert a new one (rowid reuse).
    cur.execute("DELETE FROM devices WHERE biz_no = 'E001'")
    cur.execute("INSERT INTO devices (biz_no, model, status) VALUES ('E002', '钻机', '全新入库')")

    # A brand-new device that two handover forms will both claim as successor.
    cur.execute("INSERT INTO devices (biz_no, model, status) VALUES ('C002', '叉车', '新登记')")
    conn.commit()

    cur.executemany(
        "INSERT INTO handover_forms (form_id, predecessor_no, successor_no, issued_at) "
        "VALUES (?, ?, ?, ?)",
        [
            ("F1", "A001", "A002", "2026-11-10 09:00"),
            ("F2", "E001", "E002", "2026-11-10 10:00"),
            ("F3", "C001", "C002", "2026-11-10 11:00"),
            ("F4", "D001", "C002", "2026-11-10 11:30"),
        ],
    )
    conn.commit()
    conn.close()


if __name__ == "__main__":
    build()
    print("built", DB_PATH)
