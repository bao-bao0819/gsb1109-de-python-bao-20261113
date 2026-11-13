#!/usr/bin/python
#
# Copyright 2022 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied. See the License for the specific language governing
# permissions and limitations under the License.

"""TCLI Table - CLI data in TextTable format.

Class that reads CLI output and parses into tabular format.

Supports the use of index files to map TextFSM templates to device/command
output combinations and store the data in a TextTable.

Is the glue between an automated command scraping program (such as RANCID) and
the TextFSM output parser.
"""

import copy
import dataclasses
import datetime
import io
import os
import re
import threading
import textfsm
from textfsm import texttable


class Error(Exception):
  """Base class for errors."""


class IndexTableError(Error):
  """General IndexTable error."""


class CliTableError(Error):  # pylint: disable=g-bad-exception-name
  """General CliTable error."""


class IndexTable(object):
  """Class that reads and stores comma-separated values as a TextTable.

  Stores a compiled regexp of the value for efficient matching.

  Includes functions to preprocess Columns (both compiled and uncompiled).

  Attributes:
    index: TextTable, the index file parsed into a texttable.
    compiled: TextTable, the table but with compiled regexp for each field.
  """

  def __init__(self, preread=None, precompile=None, file_path=None):
    """Create new IndexTable object.

    Args:
      preread: func, Pre-processing, applied to each field as it is read.
      precompile: func, Pre-compilation, applied to each field before compiling.
      file_path: String, Location of file to use as input.
    """
    self.index = None
    self.compiled = None
    if file_path:
      self._index_file = file_path
      self._index_handle = open(self._index_file, 'r')
      self._ParseIndex(preread, precompile)

  def __del__(self):
    """Close index handle."""
    if hasattr(self, '_index_handle'):
      self._index_handle.close()

  def __len__(self):
    """Returns number of rows in table."""
    return self.index.size

  def __copy__(self):
    """Returns a copy of an IndexTable object."""
    clone = IndexTable()
    if hasattr(self, '_index_file'):
      # pylint: disable=protected-access
      clone._index_file = self._index_file
      clone._index_handle = self._index_handle

    clone.index = self.index
    clone.compiled = self.compiled
    return clone

  def __deepcopy__(self, memodict=None):
    """Returns a deepcopy of an IndexTable object."""
    clone = IndexTable()
    if hasattr(self, '_index_file'):
      # pylint: disable=protected-access
      clone._index_file = copy.deepcopy(self._index_file)
      clone._index_handle = open(clone._index_file, 'r')

    clone.index = copy.deepcopy(self.index)
    clone.compiled = copy.deepcopy(self.compiled)
    return clone

  def _ParseIndex(self, preread, precompile):
    """Reads index file and stores entries in TextTable.

    For optimisation reasons, a second table is created with compiled entries.

    Args:
      preread: func, Pre-processing, applied to each field as it is read.
      precompile: func, Pre-compilation, applied to each field before compiling.

    Raises:
      IndexTableError: If the column headers has illegal column labels.
    """
    self.index = texttable.TextTable()
    self.index.CsvToTable(self._index_handle)

    if preread:
      for row in self.index:
        for col in row.header:
          row[col] = preread(col, row[col])

    self.compiled = copy.deepcopy(self.index)

    for row in self.compiled:
      for col in row.header:
        if precompile:
          row[col] = precompile(col, row[col])
        if row[col]:
          row[col] = re.compile(row[col])

  def GetRowMatch(self, attributes):
    """Returns the row number that matches the supplied attributes."""
    for row in self.compiled:
      try:
        for key in attributes:
          # Silently skip attributes not present in the index file.
          # pylint: disable=E1103
          if (
              key in row.header
              and row[key]
              and not row[key].match(attributes[key])
          ):
            # This line does not match, so break and try next row.
            raise StopIteration()
        return row.row
      except StopIteration:
        pass
    return 0


class CliTable(texttable.TextTable):
  """Class that reads CLI output and parses into tabular format.

  Reads an index file and uses it to map command strings to templates. It then
  uses TextFSM to parse the command output (raw) into a tabular format.

  The superkey is the set of columns that contain data that uniquely defines the
  row, the key is the row number otherwise. This is typically gathered from the
  templates 'Key' value but is extensible.

  Attributes:
    raw: String, Unparsed command string from device/command.
    index_file: String, file where template/command mappings reside.
    template_dir: String, directory where index file and templates reside.
  """

  # Parse each template index only once across all instances.
  # Without this, the regexes are parsed at every call to CliTable().
  _lock = threading.Lock()
  INDEX = {}

  def synchronised(func):
    """Synchronisation decorator."""

    # pylint: disable=E0213
    def Wrapper(main_obj, *args, **kwargs):
      main_obj._lock.acquire()  # pylint: disable=W0212
      try:
        return func(main_obj, *args, **kwargs)  # pylint: disable=E1102
      finally:
        main_obj._lock.release()  # pylint: disable=W0212

    return Wrapper

  @synchronised
  def __init__(self, index_file=None, template_dir=None):
    """Create new CLiTable object.

    Args:
      index_file: String, file where template/command mappings reside.
      template_dir: String, directory where index file and templates reside.
    """
    # pylint: disable=E1002
    super(CliTable, self).__init__()
    self._keys = set()
    self.raw = None
    self.index_file = index_file
    self.template_dir = template_dir
    if index_file:
      self.ReadIndex(index_file)

  def ReadIndex(self, index_file=None):
    """Reads the IndexTable index file of commands and templates.

    Args:
      index_file: String, file where template/command mappings reside.

    Raises:
      CliTableError: A template column was not found in the table.
    """

    self.index_file = index_file or self.index_file
    fullpath = os.path.join(self.template_dir, self.index_file)
    if self.index_file and fullpath not in self.INDEX:
      self.index = IndexTable(self._PreParse, self._PreCompile, fullpath)
      self.INDEX[fullpath] = self.index
    else:
      self.index = self.INDEX[fullpath]

    # Does the IndexTable have the right columns.
    if 'Template' not in self.index.index.header:  # pylint: disable=E1103
      raise CliTableError("Index file does not have 'Template' column.")

  def _TemplateNamesToFiles(self, template_str):
    """Parses a string of templates into a list of file handles."""

    template_list = template_str.split(':')
    template_files = []
    try:
      for tmplt in template_list:
        template_files.append(open(os.path.join(self.template_dir, tmplt), 'r'))
    except:
      for tmplt in template_files:
        tmplt.close()
      raise

    return template_files

  def ParseCmd(self, cmd_input, attributes=None, templates=None):
    """Creates a TextTable table of values from cmd_input string.

    Parses command output with template/s. If more than one template is found
    subsequent tables are merged if keys match (dropped otherwise).

    Args:
      cmd_input: String, Device/command response.
      attributes: Dict, attribute that further refine matching template.
      templates: String list of templates to parse with. If None, uses index

    Raises:
      CliTableError: A template was not found for the given command.
    """
    # Store raw command data within the object.
    self.raw = cmd_input

    if not templates:
      # Find template in template index.
      row_idx = self.index.GetRowMatch(attributes)
      if row_idx:
        templates = self.index.index[row_idx]['Template']
      else:
        raise CliTableError(
            'No template found for attributes: "%s"' % attributes
        )

    template_files = self._TemplateNamesToFiles(templates)

    try:
      # Re-initialise the table.
      self.Reset()
      self._keys = set()
      self.table = self._ParseCmdItem(self.raw, template_file=template_files[0])

      # Add additional columns from any additional tables.
      for tmplt in template_files[1:]:
        self.extend(
            self._ParseCmdItem(self.raw, template_file=tmplt), set(self._keys)
        )
    finally:
      for f in template_files:
        f.close()

  def _ParseCmdItem(self, cmd_input, template_file=None):
    """Creates Texttable with output of command.

    Args:
      cmd_input: String, Device response.
      template_file: File object, template to parse with.

    Returns:
      TextTable containing command output.

    Raises:
      CliTableError: A template was not found for the given command.
    """
    # Build FSM machine from the template.
    fsm = textfsm.TextFSM(template_file)
    if not self._keys:
      self._keys = set(fsm.GetValuesByAttrib('Key'))

    # Pass raw data through FSM.
    table = texttable.TextTable()
    table.header = fsm.header

    # Fill TextTable from record entries.
    for record in fsm.ParseText(cmd_input):
      table.Append(record)
    return table

  def _PreParse(self, key, value):
    """Executed against each field of each row read from index table."""
    if key == 'Command':
      return re.sub(r'(\[\[.+?\]\])', self._Completion, value)
    else:
      return value

  def _PreCompile(self, key, value):
    """Executed against each field of each row before compiling as regexp."""
    if key == 'Template':
      return
    else:
      return value

  def _Completion(self, match):
    r"""Replaces double square brackets with variable length completion.

    Completion cannot be mixed with regexp matching or '\' characters
    i.e. '[[(\n)]] would become (\(n)?)?.'

    Args:
      match: A regex Match() object.

    Returns:
      String of the format '(a(b(c(d)?)?)?)?'.
    """
    # Strip the outer '[[' & ']]' and replace with ()? regexp pattern.
    word = str(match.group())[2:-2]
    return '(' + ('(').join(word) + ')?' * len(word)

  def LabelValueTable(self, keys=None):
    """Return LabelValue with FSM derived keys."""
    keys = keys or self.superkey
    # pylint: disable=E1002
    return super(CliTable, self).LabelValueTable(keys)

  # pylint: disable=W0622
  def sort(self, cmp=None, key=None, reverse=False):
    """Overrides sort func to use the KeyValue for the key."""
    if not key and self._keys:
      key = self.KeyValue
    super(CliTable, self).sort(cmp=cmp, key=key, reverse=reverse)

  # pylint: enable=W0622

  def AddKeys(self, key_list):
    """Mark additional columns as being part of the superkey.

    Supplements the Keys already extracted from the FSM template.
    Useful when adding new columns to existing tables.
    Note: This will impact attempts to further 'extend' the table as the
    superkey must be common between tables for successful extension.

    Args:
      key_list: list of header entries to be included in the superkey.

    Raises:
      KeyError: If any entry in list is not a valid header entry.
    """

    for keyname in key_list:
      if keyname not in self.header:
        raise KeyError("'%s'" % keyname)

    self._keys = self._keys.union(set(key_list))

  @property
  def superkey(self):
    """Returns a set of column names that together constitute the superkey."""
    sorted_list = []
    for header in self.header:
      if header in self._keys:
        sorted_list.append(header)
    return sorted_list

  def KeyValue(self, row=None):
    """Returns the super key value for the row."""
    if not row:
      if self._iterator:
        # If we are inside an iterator use current row iteration.
        row = self[self._iterator]
      else:
        row = self.row
    # If no superkey then use row number.
    if not self.superkey:
      return ['%s' % row.row]

    sorted_list = []
    for header in self.header:
      if header in self.superkey:
        sorted_list.append(row[header])
    return sorted_list

  def JoinTraffic(self, traffic, hot_swaps=None, **kwargs):
    """Joins interface traffic to this table's interface inventory.

    This table is treated as the interface-number snapshot. Traffic rows are
    matched against that snapshot and against explicit hot-swap events. A
    traffic row is only claimed when the evidence leaves exactly one serial,
    or when the traffic row itself supplies a matching serial.

    The table is replaced with the joined table and a JoinResult is returned.
    The result keeps ambiguous and unclaimed traffic rows visible instead of
    reporting only the number of output rows.

    Args:
      traffic: TextTable, CSV buffer/string, or iterable of mappings.
      hot_swaps: Optional TextTable, CSV buffer/string, or iterable of mappings
        describing explicit insert/remove/replace events.
      **kwargs: Column and time-window options accepted by
        JoinInterfaceTraffic.

    Returns:
      JoinResult describing the join and per-traffic-row attribution.
    """
    result = JoinInterfaceTraffic(
        self, traffic, hot_swaps=hot_swaps, **kwargs
    )
    self.table = result.table
    return result

  @classmethod
  def JoinTables(cls, inventory, traffic, hot_swaps=None, **kwargs):
    """Joins two tables without mutating either one.

    Args:
      inventory: The interface-number snapshot table.
      traffic: The later traffic table.
      hot_swaps: Optional explicit insert/remove/replace events.
      **kwargs: Column and time-window options accepted by
        JoinInterfaceTraffic.

    Returns:
      JoinResult describing the join and per-traffic-row attribution.
    """
    return JoinInterfaceTraffic(
        inventory, traffic, hot_swaps=hot_swaps, **kwargs
    )


class JoinError(CliTableError):
  """Error while joining interface inventory and traffic tables."""


# Join status values. These are deliberately plain strings so callers can
# serialise them without an extra dependency.
CLAIMED = 'claimed'
AMBIGUOUS = 'ambiguous'
UNCLAIMED = 'unclaimed'

_MIN_TIME = datetime.datetime.min
_MAX_TIME = datetime.datetime.max

_INTERFACE_ALIASES = (
    'interface', 'interfacename', 'port', 'portname', 'slotport', 'name',
    '接口', '接口名', '端口', '端口名',
)
_SERIAL_ALIASES = (
    'serial', 'serialnumber', 'serialnum', 'serialno', 'sn',
    'boardserial', 'boardsn', 'cardserial', 'cardsn',
    '序列号', '板卡序列号', '板卡sn',
)
_TIME_ALIASES = (
    'time', 'timestamp', 'datetime', 'collectedat', 'observedat',
    'collected', 'observed', 'inventorytime', 'snapshotat', 'eventtime',
    'changetime', '采集时间', '盘点时间', '快照时间', '事件时间',
    '变更时间', '时间', '呼叫时间',
)
_START_ALIASES = (
    'starttime', 'start', 'windowstart', 'begintime', 'callstart',
    'callstartwindow', 'callwindowstart', 'callbegin', 'fromtime',
    '开始时间', '呼叫开始', '起始时间', '呼叫窗口开始',
)
_END_ALIASES = (
    'endtime', 'end', 'windowend', 'finishtime', 'callend',
    'callendwindow', 'callwindowend', 'callfinish', 'totime',
    '结束时间', '呼叫结束', '终止时间', '呼叫窗口结束',
)
_ACTION_ALIASES = (
    'action', 'event', 'eventtype', 'operation', 'op',
    '动作', '事件', '事件类型', '操作',
)
_OLD_SERIAL_ALIASES = (
    'oldserial', 'fromserial', 'previousserial', 'prevserial',
    'oldserialnumber', '旧序列号', '原序列号', '旧sn',
)
_NEW_SERIAL_ALIASES = (
    'newserial', 'toserial', 'nextserial', 'newserialnumber',
    '新序列号', '新sn',
)

_INSERT_ACTIONS = frozenset((
    'insert', 'inserted', 'add', 'added', 'in', 'plug', 'plugged',
    'present', 'up', '插入', '上线', '在位',
))
_REMOVE_ACTIONS = frozenset((
    'remove', 'removed', 'delete', 'deleted', 'detach', 'detached',
    'out', 'unplug', 'unplugged', 'absent', 'down',
    '拔出', '移除', '下线', '不在位',
))
_REPLACE_ACTIONS = frozenset((
    'replace', 'replaced', 'swap', 'swapped', 'change', 'changed',
    '更换', '替换',
))


def _NormaliseColumnName(name):
  return re.sub(r'[\s_\-]+', '', str(name)).casefold()


def _FindColumn(header, aliases, explicit=None):
  """Returns a column name from header matching explicit or aliases."""
  if explicit:
    if explicit not in header:
      raise JoinError("Column %r not found in table." % explicit)
    return explicit
  for column in header:
    if _NormaliseColumnName(column) in aliases:
      return column
  return None


def _CoerceTable(source, label):
  """Converts a table-like source into a TextTable without mutating it."""
  if isinstance(source, texttable.TextTable):
    return source

  if isinstance(source, str):
    source = io.StringIO(source)

  if hasattr(source, 'readline'):
    content = source.read()
    if isinstance(content, bytes):
      content = content.decode('utf-8')
    if not content.strip():
      return texttable.TextTable()
    table = texttable.TextTable()
    table.CsvToTable(io.StringIO(content))
    if not table.header:
      raise JoinError('%s table has no header.' % label)
    return table

  try:
    rows = list(source)
  except TypeError as exc:
    raise JoinError(
        '%s must be a TextTable, CSV input or iterable of mappings.' % label
    ) from exc

  if not rows:
    return texttable.TextTable()

  if isinstance(rows[0], dict):
    header = []
    for row in rows:
      for key in row:
        if key not in header:
          header.append(key)
    table = texttable.TextTable()
    table.header = header
    for row in rows:
      table.Append([row.get(column, '') for column in header])
    return table

  raise JoinError('%s rows must be mappings.' % label)


def _ParseTime(value, label='time'):
  """Parses a timestamp into a naive UTC datetime.

  Epoch values are accepted as int/float so a numeric-looking serial or
  identifier in a time column does not silently turn into 1970-era data.
  """
  if value is None:
    return None
  if isinstance(value, datetime.datetime):
    result = value
  elif isinstance(value, datetime.date):
    result = datetime.datetime.combine(value, datetime.time.min)
  elif isinstance(value, (int, float)) and not isinstance(value, bool):
    result = _ParseEpoch(value)
  else:
    text = str(value).strip()
    if not text:
      return None
    if re.fullmatch(r'[+-]?\d{10,}(\.\d+)?', text):
      result = _ParseEpoch(float(text))
    else:
      result = _ParseTimeString(text, label)
  if result.tzinfo is not None:
    result = result.astimezone(datetime.timezone.utc).replace(tzinfo=None)
  return result


def _ParseEpoch(value):
  if abs(value) >= 1e11:
    value = value / 1000.0
  return datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=value)


def _ParseTimeString(text, label):
  candidate = text
  if candidate.endswith(('Z', 'z')):
    candidate = candidate[:-1] + '+00:00'
  try:
    result = datetime.datetime.fromisoformat(candidate)
  except ValueError:
    result = None
  if result is not None:
    return result

  formats = (
      '%Y-%m-%d %H:%M:%S.%f',
      '%Y-%m-%d %H:%M:%S',
      '%Y-%m-%d %H:%M',
      '%Y-%m-%d',
      '%Y/%m/%d %H:%M:%S',
      '%Y/%m/%d %H:%M',
      '%Y/%m/%d',
      '%Y%m%d%H%M%S',
      '%Y%m%d',
  )
  for fmt in formats:
    try:
      return datetime.datetime.strptime(text, fmt)
    except ValueError:
      continue
  raise JoinError('Unable to parse %s value %r.' % (label, text))


def _TimeText(value):
  if value is None:
    return ''
  if value == _MIN_TIME:
    return 'unknown'
  if value == _MAX_TIME:
    return 'open'
  return value.strftime('%Y-%m-%dT%H:%M:%S')


def _ActionName(value):
  if value is None:
    return None
  action = _NormaliseColumnName(value)
  if action in _INSERT_ACTIONS:
    return 'insert'
  if action in _REMOVE_ACTIONS:
    return 'remove'
  if action in _REPLACE_ACTIONS:
    return 'replace'
  raise JoinError('Unknown hot-swap action %r.' % value)


@dataclasses.dataclass
class _Owner:
  """An interval during which one serial occupied one interface."""

  interface: str
  serial: str
  start: datetime.datetime
  end: datetime.datetime = _MAX_TIME
  evidence: list = dataclasses.field(default_factory=list)
  inventory_row: object = None
  order: int = 0

  def Covers(self, start, end):
    """Whether this ownership overlaps the supplied half-open interval."""
    if start is None:
      return False
    if end is None or end <= start:
      return self.start <= start < self.end
    return max(self.start, start) < min(self.end, end)

  def FullyCovers(self, start, end):
    """Whether this ownership spans the whole supplied interval."""
    if start is None:
      return False
    if end is None or end <= start:
      return self.start <= start < self.end
    return self.start <= start and end <= self.end

  def Candidate(self):
    return JoinCandidate(
        interface=self.interface,
        serial=self.serial,
        evidence=tuple(self.evidence),
    )


def _EventRow(interface, serial, event_time):
  row = texttable.Row()
  row['Interface'] = interface
  row['Serial'] = serial
  row['InventoryTime'] = _TimeText(event_time)
  row.row = 1
  return row


@dataclasses.dataclass(frozen=True)
class JoinCandidate:
  """One possible owner of a traffic row."""

  interface: str
  serial: str
  evidence: tuple = ()


@dataclasses.dataclass(frozen=True)
class TrafficAttribution:
  """Attribution decision for one traffic row.

  status is one of 'claimed', 'ambiguous' or 'unclaimed'. A claimed row has
  exactly one candidate. An ambiguous row has at least two candidates and
  keeps every interpretation. An unclaimed row has no candidates.
  """

  traffic_row: int
  interface: str
  traffic_interface: str
  status: str
  serial: str
  candidates: tuple
  evidence: tuple
  reason: str
  table_rows: tuple = ()
  traffic: dict = dataclasses.field(default_factory=dict)

  @property
  def claimed(self):
    return self.status == CLAIMED

  @property
  def ambiguous(self):
    return self.status == AMBIGUOUS

  @property
  def unclaimed(self):
    return self.status == UNCLAIMED


@dataclasses.dataclass
class JoinResult:
  """Result of an evidence-based inventory/traffic join.

  Attributes:
    table: TextTable containing one row per claimed traffic row and one row per
      candidate for an ambiguous traffic row. Unclaimed traffic rows are
      omitted by default but always remain in attributions and Summary().
    attributions: One TrafficAttribution per input traffic row, in input order.
  """

  table: texttable.TextTable
  attributions: tuple

  @property
  def rows(self):
    return self.attributions

  @property
  def claimed(self):
    return tuple(row for row in self.attributions if row.status == CLAIMED)

  @property
  def ambiguous(self):
    return tuple(row for row in self.attributions if row.status == AMBIGUOUS)

  @property
  def unclaimed(self):
    return tuple(row for row in self.attributions if row.status == UNCLAIMED)

  @property
  def claimed_count(self):
    return len(self.claimed)

  @property
  def ambiguous_count(self):
    return len(self.ambiguous)

  @property
  def unclaimed_count(self):
    return len(self.unclaimed)

  @property
  def total_count(self):
    return len(self.attributions)

  @property
  def success_count(self):
    """Number of claimed traffic rows.

    This is only the claim count. Check ambiguous/unclaimed (or Complete) before
    treating the join as fully successful.
    """
    return self.claimed_count

  @property
  def complete(self):
    """Whether every traffic row was claimed unambiguously."""
    return not self.ambiguous and not self.unclaimed

  def Summary(self):
    """Returns counts that make unresolved traffic rows explicit."""
    return {
        'traffic_rows': self.total_count,
        'claimed': self.claimed_count,
        'ambiguous': self.ambiguous_count,
        'unclaimed': self.unclaimed_count,
        'output_rows': self.table.size,
        'complete': self.complete,
    }

  def SubTable(self, status):
    """Returns a TextTable with only rows of the requested join status."""
    table = texttable.TextTable()
    table.header = list(self.table.header)
    for row in self.table:
      if row.get('JoinStatus') == status:
        table.Append([row[column] for column in table.header])
    return table

  def __str__(self):
    summary = self.Summary()
    return (
        'JoinResult(traffic_rows=%(traffic_rows)d, claimed=%(claimed)d, '
        'ambiguous=%(ambiguous)d, unclaimed=%(unclaimed)d, '
        'output_rows=%(output_rows)d, complete=%(complete)s)' % summary
    )


def _ResolveInventoryTimes(table, column, default_time, label):
  """Returns the observation time for each inventory row."""
  times = []
  fallback = _ParseTime(default_time, label) if default_time is not None else None
  for row in table:
    raw = row.get(column) if column else None
    parsed = _ParseTime(raw, label) if raw not in (None, '') else fallback
    if parsed is None:
      parsed = _MIN_TIME
    times.append(parsed)
  return times


def _InventoryOwners(table, interface_column, serial_column, time_column,
                     default_time):
  """Builds ownership intervals from one or more inventory snapshots."""
  if not table.header:
    return []
  if interface_column is None or serial_column is None:
    raise JoinError(
        'Inventory needs interface and serial columns; found %r.'
        % list(table.header)
    )

  rows = list(table)
  times = _ResolveInventoryTimes(
      table, time_column, default_time, 'inventory time'
  )
  snapshot_times = sorted(set(times))
  snapshot_ends = {
      observed_at: (
          snapshot_times[index + 1]
          if index + 1 < len(snapshot_times) else _MAX_TIME
      )
      for index, observed_at in enumerate(snapshot_times)
  }

  owners = []
  order = 0
  for row_index, row in enumerate(rows):
    interface = str(row[interface_column]).strip()
    serial = str(row[serial_column]).strip()
    if not interface or not serial:
      continue
    observed_at = times[row_index]
    owners.append(_Owner(
        interface=interface,
        serial=serial,
        start=observed_at,
        end=snapshot_ends[observed_at],
        evidence=[
            'inventory row %d' % (row_index + 1),
            'inventory time %s' % _TimeText(observed_at),
        ],
        inventory_row=row,
        order=order,
    ))
    order += 1
  return owners


def _CoerceEvents(source, interface_column, time_column, serial_column,
                  action_column, old_serial_column, new_serial_column):
  """Converts a hot-swap source into event dictionaries."""
  if source is None:
    return []
  table = _CoerceTable(source, 'hot swap')
  if not table.header:
    return []

  interface_column = _FindColumn(table.header, _INTERFACE_ALIASES,
                                 interface_column)
  time_column = _FindColumn(table.header, _TIME_ALIASES, time_column)
  serial_column = _FindColumn(table.header, _SERIAL_ALIASES, serial_column)
  action_column = _FindColumn(table.header, _ACTION_ALIASES, action_column)
  old_serial_column = _FindColumn(table.header, _OLD_SERIAL_ALIASES,
                                  old_serial_column)
  new_serial_column = _FindColumn(table.header, _NEW_SERIAL_ALIASES,
                                  new_serial_column)

  if interface_column is None:
    raise JoinError('Hot swap table needs an interface column.')
  if time_column is None:
    raise JoinError('Hot swap table needs a time column.')

  events = []
  for row in table:
    interface = str(row[interface_column]).strip()
    event_time = _ParseTime(row[time_column], 'hot swap time')
    if not interface or event_time is None:
      raise JoinError('Every hot swap row needs interface and time.')

    old_serial = (
        str(row[old_serial_column]).strip()
        if old_serial_column and row[old_serial_column] else ''
    )
    new_serial = (
        str(row[new_serial_column]).strip()
        if new_serial_column and row[new_serial_column] else ''
    )
    raw_serial = (
        str(row[serial_column]).strip()
        if serial_column and row[serial_column] else ''
    )
    action = _ActionName(row[action_column] if action_column else None)

    if action is None:
      if old_serial and new_serial:
        action = 'replace'
      elif old_serial and raw_serial:
        action = 'replace'
        new_serial = raw_serial
      elif new_serial:
        action = 'insert'
      elif old_serial:
        action = 'remove'
      elif raw_serial:
        raise JoinError(
            'Hot swap row with serial %r has no action.' % raw_serial
        )
      else:
        raise JoinError('Hot swap row needs an action or serial.')

    if action == 'replace':
      if not new_serial:
        new_serial = raw_serial
      if not old_serial and raw_serial == new_serial:
        old_serial = ''
    elif action == 'insert':
      if not new_serial:
        new_serial = raw_serial
    elif action == 'remove':
      if not old_serial:
        old_serial = raw_serial

    events.append({
        'time': event_time,
        'interface': interface,
        'action': action,
        'serial': new_serial if action != 'remove' else old_serial,
        'old_serial': old_serial,
        'new_serial': new_serial,
    })
  events.sort(key=lambda event: event['time'])
  return events


def _ActiveOwners(owners, interface, at):
  """Returns owners active at a given instant for one interface."""
  return [
      owner for owner in owners
      if owner.interface == interface and owner.start <= at < owner.end
  ]


def _ApplyEvents(owners, events):
  """Applies explicit hot-swap events to ownership intervals."""
  for event in events:
    event_time = event['time']
    interface = event['interface']
    action = event['action']
    evidence = 'hot swap %s@%s' % (action, _TimeText(event_time))
    active = _ActiveOwners(owners, interface, event_time)

    if action == 'remove':
      target_serial = event['old_serial']
      targets = [
          owner for owner in active
          if not target_serial or owner.serial == target_serial
      ]
      for owner in targets:
        owner.end = min(owner.end, event_time)
        owner.evidence.append(evidence)
      continue

    if action == 'replace':
      target_serial = event['old_serial']
      new_serial = event['new_serial']
      if not new_serial:
        raise JoinError('Replace event at %s has no new serial.' % evidence)
      if target_serial:
        targets = [owner for owner in active if owner.serial == target_serial]
      else:
        targets = [owner for owner in active if owner.serial != new_serial]
      for owner in targets:
        owner.end = min(owner.end, event_time)
        owner.evidence.append(evidence)
      _AddEventOwner(owners, interface, new_serial, event_time, evidence)
      continue

    # An insert is evidence for the new serial, but it does not by itself
    # prove that the previous serial was removed. Keep both interpretations
    # until a remove/replace event supplies that evidence.
    new_serial = event['new_serial']
    if not new_serial:
      raise JoinError('Insert event at %s has no serial.' % evidence)
    _AddEventOwner(owners, interface, new_serial, event_time, evidence)
  return owners


def _AddEventOwner(owners, interface, serial, event_time, evidence):
  """Adds an event-derived interval, merging with an identical active owner."""
  for owner in owners:
    if (owner.interface == interface and owner.serial == serial
        and owner.start <= event_time < owner.end):
      if evidence not in owner.evidence:
        owner.evidence.append(evidence)
      return owner
  owner = _Owner(
      interface=interface,
      serial=serial,
      start=event_time,
      end=_MAX_TIME,
      evidence=[evidence],
      inventory_row=_EventRow(interface, serial, event_time),
      order=len(owners),
  )
  owners.append(owner)
  return owner


def _UniqueOwners(owners):
  """Collapses owners to distinct interface+serial identities.

  Disjoint intervals for one identity stay separate in the ownership model,
  but they are one possible original board. Evidence from every interval is
  retained on the collapsed candidate.
  """
  by_identity = {}
  for owner in owners:
    key = (owner.interface, owner.serial)
    if key not in by_identity:
      by_identity[key] = _Owner(
          interface=owner.interface,
          serial=owner.serial,
          start=owner.start,
          end=owner.end,
          evidence=list(owner.evidence),
          inventory_row=owner.inventory_row,
          order=owner.order,
      )
      continue
    existing = by_identity[key]
    for item in owner.evidence:
      if item not in existing.evidence:
        existing.evidence.append(item)
    existing.start = min(existing.start, owner.start)
    existing.end = max(existing.end, owner.end)
  return sorted(by_identity.values(), key=lambda owner: (owner.interface,
                                                         owner.start,
                                                         owner.serial,
                                                         owner.order))


def _MatchOwners(owners, interface=None, serial=None, start=None, end=None):
  """Returns owners matching the supplied evidence.

  Disjoint intervals are never merged. Merging them would invent ownership
  across a gap and could hide a later reinsertion of the same serial.
  """
  selected = [
      owner for owner in owners
      if (interface is None or owner.interface == interface)
      and (serial is None or owner.serial == serial)
      and (start is None or owner.Covers(start, end))
  ]
  selected.sort(key=lambda owner: (owner.interface, owner.start,
                                   owner.serial, owner.order))
  matched = []
  for owner in selected:
    if matched:
      previous = matched[-1]
      if (previous.interface == owner.interface
          and previous.serial == owner.serial
          and previous.end == owner.start):
        previous.end = owner.end
        for item in owner.evidence:
          if item not in previous.evidence:
            previous.evidence.append(item)
        continue
    matched.append(_Owner(
        interface=owner.interface,
        serial=owner.serial,
        start=owner.start,
        end=owner.end,
        evidence=list(owner.evidence),
        inventory_row=owner.inventory_row,
        order=owner.order,
    ))
  return matched


def _TrafficWindow(row, time_column, start_column, end_column, default_time):
  """Returns (start, end) for a traffic row, with end None for a point."""
  if start_column and row.get(start_column) not in (None, ''):
    start = _ParseTime(row[start_column], 'traffic start time')
  elif time_column and row.get(time_column) not in (None, ''):
    start = _ParseTime(row[time_column], 'traffic time')
  else:
    start = _ParseTime(default_time, 'traffic time') if default_time else None

  if end_column and row.get(end_column) not in (None, ''):
    end = _ParseTime(row[end_column], 'traffic end time')
  else:
    end = None

  if start is None and end is not None:
    start = end
  if start is not None and end is not None and end < start:
    raise JoinError('Traffic end time precedes start time.')
  if start is not None and end == start:
    end = None
  return start, end


def _TrafficColumnMap(inventory_header, traffic_header, reserved):
  """Maps traffic column names to collision-free output column names."""
  output_header = list(inventory_header)
  reserved = set(reserved) | set(output_header)
  mapping = {}
  for column in traffic_header:
    target = column
    if target in reserved:
      target = 'Traffic.%s' % column
      suffix = 2
      while target in reserved:
        target = 'Traffic.%s.%d' % (column, suffix)
        suffix += 1
    mapping[column] = target
    reserved.add(target)
    output_header.append(target)
  return output_header, mapping


def _RowValues(row):
  return {column: row[column] for column in row.header}


def JoinInterfaceTraffic(inventory, traffic, hot_swaps=None, *,
                         interface_column=None, serial_column=None,
                         inventory_time_column=None, inventory_time=None,
                         traffic_interface_column=None, traffic_serial_column=None,
                         traffic_time_column=None, traffic_start_column=None,
                         traffic_end_column=None,
                         event_interface_column=None, event_serial_column=None,
                         event_time_column=None, event_action_column=None,
                         event_old_serial_column=None,
                         event_new_serial_column=None,
                         include_unclaimed=False):
  """Joins interface traffic to the interface snapshot that preceded it.

  The inventory table is the device's interface-number snapshot. The traffic
  table is collected later. A board may have been hot-swapped in between, and
  its slot/interface number may have been reused. The join therefore uses:

  * the traffic row's call window (or point time),
  * the serial number recorded on each side, and
  * explicit insert/remove/replace events in hot_swaps.

  Ownership intervals are built from the inventory. An insert event adds a
  candidate but does not erase the previous serial unless a remove or replace
  event names it. This conservative rule means missing evidence produces an
  'ambiguous' attribution rather than a fabricated claim.

  Args:
    inventory: TextTable, CSV string/buffer, or iterable of mappings. It must
      contain an interface and a serial column.
    traffic: TextTable, CSV string/buffer, or iterable of mappings with an
      interface column. Time and serial columns are optional but improve
      attribution.
    hot_swaps: Optional explicit insert/remove/replace events. Each event needs
      an interface, a time and an action (or old/new serial columns).
    interface_column: Explicit inventory interface column name.
    serial_column: Explicit inventory serial column name.
    inventory_time_column: Optional inventory observation time column.
    inventory_time: Optional scalar time for inventory rows without a time.
    traffic_interface_column: Explicit traffic interface column name.
    traffic_serial_column: Explicit traffic serial column name.
    traffic_time_column: Explicit traffic point-time column.
    traffic_start_column: Explicit traffic window start column.
    traffic_end_column: Explicit traffic window end column.
    event_interface_column: Explicit event interface column name.
    event_serial_column: Explicit event serial column name.
    event_time_column: Explicit event time column name.
    event_action_column: Explicit event action column name.
    event_old_serial_column: Explicit event old-serial column name.
    event_new_serial_column: Explicit event new-serial column name.
    include_unclaimed: Also emit unclaimed traffic rows in the output table as
      diagnostic rows. Defaults to False because unclaimed traffic has no
      evidence-backed original interface. Unclaimed rows remain available in
      attributions and in the summary counts.

  Returns:
    JoinResult with the joined table and one attribution per traffic row.
  """
  inventory_table = _CoerceTable(inventory, 'inventory')
  traffic_table = _CoerceTable(traffic, 'traffic')

  if not inventory_table.header:
    raise JoinError('Inventory table is empty.')
  if not traffic_table.header:
    return JoinResult(table=_EmptyJoinTable(inventory_table, traffic_table),
                      attributions=())

  inv_interface = _FindColumn(inventory_table.header, _INTERFACE_ALIASES,
                              interface_column)
  inv_serial = _FindColumn(inventory_table.header, _SERIAL_ALIASES,
                           serial_column)
  inv_time = _FindColumn(inventory_table.header, _TIME_ALIASES,
                         inventory_time_column)

  traffic_interface = _FindColumn(traffic_table.header, _INTERFACE_ALIASES,
                                  traffic_interface_column)
  traffic_serial = _FindColumn(traffic_table.header, _SERIAL_ALIASES,
                               traffic_serial_column)
  traffic_time = _FindColumn(traffic_table.header, _TIME_ALIASES,
                             traffic_time_column)
  traffic_start = _FindColumn(traffic_table.header, _START_ALIASES,
                              traffic_start_column)
  traffic_end = _FindColumn(traffic_table.header, _END_ALIASES,
                            traffic_end_column)

  if inv_interface is None:
    raise JoinError('Inventory table needs an interface column.')
  if inv_serial is None:
    raise JoinError('Inventory table needs a serial column.')
  if traffic_interface is None:
    raise JoinError('Traffic table needs an interface column.')

  owners = _InventoryOwners(
      inventory_table, inv_interface, inv_serial, inv_time, inventory_time
  )
  events = _CoerceEvents(
      hot_swaps, event_interface_column, event_time_column,
      event_serial_column, event_action_column,
      event_old_serial_column, event_new_serial_column,
  )
  owners = _ApplyEvents(owners, events)

  diagnostic_columns = [
      'JoinStatus', 'JoinSerial', 'JoinCandidates', 'JoinEvidence',
      'JoinReason', 'JoinTrafficRow', 'JoinTrafficInterface',
  ]
  if traffic_serial is not None:
    diagnostic_columns.append('JoinTrafficSerial')
  output_header, traffic_map = _TrafficColumnMap(
      inventory_table.header, traffic_table.header, diagnostic_columns
  )
  for column in diagnostic_columns:
    if column in output_header:
      raise JoinError(
          'Column %r is reserved for join diagnostics.' % column
      )
    output_header.append(column)

  output = texttable.TextTable()
  output.header = output_header
  attributions = []

  for traffic_index, row in enumerate(traffic_table, start=1):
    observed_interface = str(row[traffic_interface]).strip()
    start, end = _TrafficWindow(
        row, traffic_time, traffic_start, traffic_end, None
    )
    row_serial = (
        str(row[traffic_serial]).strip()
        if traffic_serial is not None and row[traffic_serial] not in (None, '')
        else ''
    )

    by_time = _MatchOwners(
        owners, interface=observed_interface, start=start, end=end
    )
    by_serial = _MatchOwners(owners, serial=row_serial) if row_serial else []
    by_serial_time = (
        _MatchOwners(owners, serial=row_serial, start=start, end=end)
        if row_serial and start is not None else by_serial
    )
    full_time = [owner for owner in by_time if owner.FullyCovers(start, end)]
    full_serial_time = [
        owner for owner in by_serial_time
        if owner.FullyCovers(start, end)
    ]

    if row_serial:
      # The serial is direct evidence, but it does not erase a conflicting
      # time window. If serial and time point at different boards, keep both
      # interpretations instead of choosing one silently.
      serial_ids = {(owner.interface, owner.serial) for owner in by_serial}
      serial_time_ids = {
          (owner.interface, owner.serial) for owner in by_serial_time
      }
      time_ids = {(owner.interface, owner.serial) for owner in by_time}
      if not by_serial:
        status = UNCLAIMED
        candidates = []
        reason = (
            'traffic serial is absent from the inventory; the observed '
            'interface and time cannot claim an original board'
        )
      elif (len(serial_ids) == 1 and full_serial_time
            and len(full_serial_time) == len(by_serial_time)
            and (not by_time or serial_time_ids <= time_ids)):
        status = CLAIMED
        candidates = by_serial_time
        reason = 'traffic serial identifies the original interface at the call time'
      elif start is not None and not by_serial_time and len(serial_ids) == 1:
        status = AMBIGUOUS
        candidates = _UniqueOwners(by_serial + by_time)
        reason = (
            'traffic serial exists but does not overlap the call window; '
            'both the serial and the time-based owner are kept'
        )
      elif len(serial_ids) == 1:
        status = AMBIGUOUS
        candidates = _UniqueOwners(by_serial + by_time)
        if by_serial_time and len(full_serial_time) < len(by_serial_time):
          reason = (
              'traffic serial and call window conflict, and the window is '
              'not fully covered; all interpretations are kept'
          )
        else:
          reason = (
              'traffic serial and call window identify different owners; '
              'both interpretations are kept'
          )
      else:
        status = AMBIGUOUS
        candidates = _UniqueOwners(by_serial + by_time)
        reason = 'traffic serial appears on more than one interface'
    elif start is None:
      candidates = _UniqueOwners(by_time)
      if len(candidates) == 1:
        status = CLAIMED
        reason = 'single owner and no conflicting time or serial evidence'
      elif candidates:
        status = AMBIGUOUS
        reason = 'traffic time is missing and the interface has multiple owners'
      else:
        status = UNCLAIMED
        reason = 'no owner for the interface'
    else:
      candidates = _UniqueOwners(by_time)
      full_identities = _UniqueOwners(full_time)
      if len(full_identities) == 1 and len(candidates) == 1:
        status = CLAIMED
        reason = 'single owner fully covers the call window'
      elif candidates:
        status = AMBIGUOUS
        if len(candidates) == 1:
          reason = (
              'the only overlapping owner does not fully cover the call '
              'window; the row is not claimed'
          )
        else:
          reason = 'multiple owners overlap the call window'
      else:
        status = UNCLAIMED
        reason = 'no owner overlaps the call window'

    evidence = []
    for owner in candidates:
      for item in owner.evidence:
        if item not in evidence:
          evidence.append(item)
    if start is not None:
      evidence.append('traffic window %s..%s' % (
          _TimeText(start), _TimeText(end) if end is not None else 'point'))
    if row_serial:
      evidence.append('traffic serial %s' % row_serial)

    candidate_text = ' | '.join(
        '%s=%s' % (owner.interface, owner.serial) for owner in candidates
    )
    table_rows = []

    if status == UNCLAIMED:
      if include_unclaimed:
        values = _BuildOutputRow(
            output_header, traffic_map,
            row, None, 'unclaimed', '', '', evidence, reason,
            traffic_index, observed_interface, traffic_table.header,
            inv_interface, inv_serial, inv_time, traffic_serial,
        )
        output.Append(values)
        table_rows.append(output.size)
    else:
      for owner in candidates:
        values = _BuildOutputRow(
            output_header, traffic_map,
            row, owner, status, owner.serial, candidate_text, evidence, reason,
            traffic_index, observed_interface, traffic_table.header,
            inv_interface, inv_serial, inv_time, traffic_serial,
        )
        output.Append(values)
        table_rows.append(output.size)

    attributions.append(TrafficAttribution(
        traffic_row=traffic_index,
        interface=candidates[0].interface if candidates else '',
        traffic_interface=observed_interface,
        status=status,
        serial=candidates[0].serial if len(candidates) == 1 else '',
        candidates=tuple(owner.Candidate() for owner in candidates),
        evidence=tuple(evidence),
        reason=reason,
        table_rows=tuple(table_rows),
        traffic=_RowValues(row),
    ))

  return JoinResult(table=output, attributions=tuple(attributions))


def _EmptyJoinTable(inventory_table, traffic_table):
  table = texttable.TextTable()
  diagnostics = [
      'JoinStatus', 'JoinSerial', 'JoinCandidates', 'JoinEvidence',
      'JoinReason', 'JoinTrafficRow', 'JoinTrafficInterface',
      'JoinTrafficSerial',
  ]
  header, _ = _TrafficColumnMap(
      inventory_table.header, traffic_table.header, diagnostics
  )
  header.extend(diagnostics)
  table.header = header
  return table


def _BuildOutputRow(header, traffic_map, traffic_row, owner, status,
                    serial, candidates, evidence, reason, traffic_row_number,
                    observed_interface, traffic_header,
                    inv_interface, inv_serial, inv_time, traffic_serial):
  """Builds one output row, preserving inventory and traffic values."""
  values = {column: '' for column in header}

  if owner is not None and owner.inventory_row is not None:
    for column in owner.inventory_row.header:
      if column in values:
        values[column] = owner.inventory_row[column]
  if owner is not None:
    values[inv_interface] = owner.interface
    values[inv_serial] = owner.serial
    if inv_time is not None and not values[inv_time]:
      values[inv_time] = _TimeText(owner.start)

  for column in traffic_header:
    values[traffic_map[column]] = traffic_row[column]

  values['JoinStatus'] = status
  values['JoinSerial'] = serial
  values['JoinCandidates'] = candidates
  values['JoinEvidence'] = '; '.join(evidence)
  values['JoinReason'] = reason
  values['JoinTrafficRow'] = str(traffic_row_number)
  values['JoinTrafficInterface'] = observed_interface
  if 'JoinTrafficSerial' in values and traffic_serial is not None:
    values['JoinTrafficSerial'] = traffic_row[traffic_serial]

  return [values[column] for column in header]
