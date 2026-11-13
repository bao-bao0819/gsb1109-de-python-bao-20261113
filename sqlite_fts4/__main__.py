"""Command line entry point: ``python -m sqlite_fts4``.

Audits (and optionally repairs) an FTS4 external-content index against the
detail table it is built on.  A second run with --apply performs no new
repairs once the index matches the content.
"""

import argparse
import sqlite3
import sys

from sqlite_fts4.consistency import (
    ConsistencyError,
    audit,
    format_report,
    repair,
    result_to_json,
)


def build_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Audit an FTS4 external-content index for stale terms left behind "
            "by rowid reuse, using real inverted-index hits as evidence."
        )
    )
    parser.add_argument("database", help="path to the SQLite database file")
    parser.add_argument(
        "--fts", required=True, help="name of the FTS4 virtual table"
    )
    parser.add_argument(
        "--content",
        help="content table name (defaults to the FTS4 content=<table> option)",
    )
    parser.add_argument(
        "--rowid",
        help="content rowid column (defaults to content_rowid option or rowid)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="repair the evidenced rowids; without this flag only report",
    )
    parser.add_argument(
        "--json", action="store_true", help="emit the evidence report as JSON"
    )
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    conn = sqlite3.connect(args.database)
    try:
        try:
            result = audit(
                conn,
                args.fts,
                content_table=args.content,
                rowid_column=args.rowid,
            )
        except ConsistencyError as exc:
            print("error: {}".format(exc), file=sys.stderr)
            return 2

        if args.json:
            print(result_to_json(result))
        else:
            print(format_report(result))

        if args.apply and result.mismatches:
            repaired = repair(conn, result)
            if not args.json:
                print("repaired rowids: {}".format(repaired))
            # Re-audit: the same batch must now produce zero new evidence.
            recheck = audit(
                conn,
                args.fts,
                content_table=args.content,
                rowid_column=args.rowid,
            )
            if recheck.mismatches:
                print(
                    "error: {} mismatches remain after repair".format(
                        len(recheck.mismatches)
                    ),
                    file=sys.stderr,
                )
                return 1
            if not args.json:
                print("re-audit: no mismatches")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
