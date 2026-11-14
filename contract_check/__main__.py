"""CLI: judge a release's compatibility promise against two schema files.

Usage:
    python -m contract_check OLD.json NEW.json [--reader strict|lenient] [--json]

Exit code is 0 when the promise is kept (or nothing was promised), 1 when
the check proves the promise broken and prints witness records.
"""

from __future__ import annotations

import argparse
import json
import sys

from contract_check.checker import LENIENT, STRICT, Schema, check_contract


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="contract_check",
        description="Judge a data-product compatibility promise.",
    )
    parser.add_argument("old_schema", help="JSON schema of the released (old) version")
    parser.add_argument("new_schema", help="JSON schema of the candidate (new) version")
    parser.add_argument(
        "--reader",
        choices=(STRICT, LENIENT),
        default=STRICT,
        help="field convention of the read side: strict readers reject unknown "
        "fields, lenient readers ignore them (default: strict)",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args(argv)

    old = Schema.load(args.old_schema)
    new = Schema.load(args.new_schema)
    verdict = check_contract(old, new, reader_convention=args.reader)

    if args.json:
        print(json.dumps(verdict.to_dict(), indent=2, sort_keys=True))
    else:
        print(f"product:  {old.product}")
        print(f"versions: {verdict.old_version} -> {verdict.new_version}")
        print(f"promise:  {verdict.promise}")
        print(f"status:   {verdict.status}")
        for violation in verdict.violations:
            print()
            print(f"  VIOLATION in {violation.direction} [{violation.kind}]")
            print(f"    field:  {violation.field}")
            print(f"    detail: {violation.detail}")
            print(f"    shortest failing source record: {json.dumps(violation.record, sort_keys=True)}")

    return 1 if verdict.status == "broken" else 0


if __name__ == "__main__":
    sys.exit(main())
