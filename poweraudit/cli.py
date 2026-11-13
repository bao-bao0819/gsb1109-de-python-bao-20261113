"""Command line entry point for the power metering audit."""

import argparse
import sys

from poweraudit import parser as audit_parser
from poweraudit.engine import audit
from poweraudit.report import render_json, render_text


def parse_args(argv=None):
  parser = argparse.ArgumentParser(
      description=('Determine which per-card power column relations explain'
                   ' every recorded chassis total.'))
  parser.add_argument(
      'audit_file',
      help='canonical file with [chassis], [cards] and [events] sections')
  parser.add_argument(
      '--json', action='store_true',
      help='emit machine-readable JSON instead of a text report')
  return parser.parse_args(argv)


def main(argv=None):
  args = parse_args(argv)
  chassis, cards, events = audit_parser.load_audit_file(args.audit_file)
  result = audit(chassis, cards, events)
  if args.json:
    print(render_json(result))
  else:
    print(render_text(result))
  return 0


if __name__ == '__main__':
  sys.exit(main())
