"""Parsers for chassis totals, slot readings and insert/remove events.

Two input shapes are supported:

* CLI echo blocks parsed with the TextFSM templates shipped in
  ``poweraudit/templates``.
* A canonical sectioned text format used by the command line tool.  A
  missing rail is written as ``N/A`` and is preserved as missing rather
  than coerced to zero.
"""

import os

from textfsm import TextFSM

from poweraudit.engine import (
    CardSample,
    ChassisSample,
    Event,
    MISSING,
)

_TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), 'templates')


def _template_path(name):
  return os.path.join(_TEMPLATE_DIR, name)


def _parse_with_template(text, template_name):
  with open(_template_path(template_name), encoding='utf-8') as handle:
    fsm = TextFSM(handle)
  return fsm.ParseTextToDicts(text)


def parse_chassis_echo(text):
  """Parse a chassis total CLI echo into ChassisSample objects."""
  rows = _parse_with_template(text, 'chassis_power.template')
  return [ChassisSample(row['Time'], row['Total'], row['Tolerance'])
          for row in rows]


def parse_card_echo(text):
  """Parse a slot power CLI echo into CardSample objects."""
  rows = _parse_with_template(text, 'card_power.template')
  return [CardSample(
      row['Time'], row['Slot'], row['Model'],
      _optional_number(row['RailA']),
      _optional_number(row['RailB']),
      row['Tolerance']) for row in rows]


def parse_event_echo(text):
  """Parse a card insert/remove CLI echo into Event objects."""
  rows = _parse_with_template(text, 'card_events.template')
  return [Event(row['Time'], row['Slot'], row['Action'],
                row['Model'] or None) for row in rows]


def _optional_number(token):
  if token is None or token == '' or token.upper() == 'N/A':
    return MISSING
  return token


def parse_audit_file(text):
  """Parse the canonical sectioned format.

  Sections are ``[chassis]``, ``[cards]`` and ``[events]``.  Rows are
  comma or whitespace separated; blank lines and ``#`` comments are
  ignored.  Returns ``(chassis_samples, card_samples, events)``.
  """
  sections = {'[chassis]': [], '[cards]': [], '[events]': []}
  current = None
  for raw_line in text.splitlines():
    line = raw_line.strip()
    if not line or line.startswith('#'):
      continue
    lowered = line.lower()
    if lowered in sections:
      current = lowered
      continue
    if current is None:
      raise ValueError('row outside of any section: %r' % raw_line)
    fields = _split_row(line)
    sections[current].append(fields)

  chassis = [ChassisSample(row[0], row[1], row[2])
             for row in sections['[chassis]']]
  cards = [CardSample(row[0], row[1], row[2],
                      _optional_number(row[3]),
                      _optional_number(row[4]), row[5])
           for row in sections['[cards]']]
  events = [Event(row[0], row[1], row[2], row[3] if len(row) > 3 else None)
            for row in sections['[events]']]
  return chassis, cards, events


def _split_row(line):
  if ',' in line:
    return [field.strip() for field in line.split(',')]
  return line.split()


def load_audit_file(path):
  """Read a canonical audit file from disk."""
  with open(path, encoding='utf-8') as handle:
    return parse_audit_file(handle.read())
