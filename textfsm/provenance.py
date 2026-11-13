"""Line and cell provenance for TextFSM offline parsing.

TextFSM answers "what records were produced?".  Provenance answers the
follow-up questions asked when a row is missing or looks wrong:

  * Which physical input lines did a rule match?
  * Did a line match at all, was it only consumed for a state change/clear,
    or did its values reach the final table?
  * Which input lines contributed to one committed cell?
  * When looking at a physical line, which actions were merely control and
    which values did it finally contribute?

The classes in this module are deliberately read-only views over a parse.
The parser creates and updates them while ``ParseText`` runs; callers use the
``ParseTrace`` returned by ``TextFSM.ParseTextWithTrace`` (or available as
``TextFSM.trace`` after a normal parse).

Line numbers are physical, 1-based numbers.  Result row indexes are 0-based,
matching the lists returned by ``ParseText``.  A synthetic EOF commit has no
physical line number and is represented by ``line_number=None``.
"""

import collections


class LineDisposition(object):
  """The primary fate of a physical input line.

  ``CONTROL`` means the line matched at least one rule but contributed no
  value to the final table (for example it only changed state or cleared a
  record).  ``DISCARDED`` means values were assigned while parsing the line
  but none of them survived into a committed record.  ``UNPARSED`` means the
  FSM stopped before reaching the line (for example an ``End`` transition);
  such lines are still listed so physical line lookup is complete.
  """

  UNMATCHED = 'unmatched'
  CONTROL = 'control'
  DATA = 'data'
  DISCARDED = 'discarded'
  UNPARSED = 'unparsed'

  VALUES = (UNMATCHED, CONTROL, DATA, DISCARDED, UNPARSED)


class ControlAction(object):
  """A non-data action caused by a matched rule.

  Attributes:
    kind: str, one of ``state``, ``record``, ``clear``, ``clearall``,
      ``continue`` or ``error``.
    line: SourceLine, the input line that caused the action.
    application: RuleApplication, the rule application that caused it.
    detail: dict, additional action specific information.
  """

  def __init__(self, kind, line=None, application=None, detail=None):
    self.kind = kind
    self.line = line
    self.application = application
    self.detail = detail or {}

  @property
  def line_number(self):
    return self.line.number if self.line is not None else None

  def to_dict(self):
    return {
        'kind': self.kind,
        'line_number': self.line_number,
        'rule_line_number': (
            self.application.rule_line_number
            if self.application is not None
            else None
        ),
        'detail': dict(self.detail),
    }

  def __repr__(self):
    return 'ControlAction(kind=%r, line=%r)' % (self.kind, self.line_number)


class Assignment(object):
  """A value captured from an input line by a matched rule.

  One Assignment is created per named value captured by a rule.  The object
  is shared between the line view and the cells it eventually reaches.  A
  List value creates one Assignment per list element, which is why a single
  cell can have several contributing lines.
  """

  def __init__(self, line, application, value_name, value, element_index=None):
    self.line = line
    self.application = application
    self.value_name = value_name
    self.value = value
    self.element_index = element_index
    self.final_cells = []
    self.status = 'pending'
    self.kind = 'assign'
    self.detail = {}

  @property
  def line_number(self):
    return self.line.number if self.line is not None else None

  @property
  def column(self):
    return self.value_name

  @property
  def is_data(self):
    """Whether this assignment actually carried a value.

    TextFSM assigns ``None`` for unmatched optional named groups.  Those
    assignments are useful for explaining clears, but they are not data.
    """
    if isinstance(self.value, dict):
      return True
    return self.value is not None

  @property
  def committed(self):
    return bool(self.final_cells)

  def _Attach(self, cell):
    if cell not in self.final_cells:
      self.final_cells.append(cell)
    self.status = 'committed'

  def _Detach(self, cell):
    while cell in self.final_cells:
      self.final_cells.remove(cell)
    if not self.final_cells and self.status == 'committed':
      self.status = 'replaced'

  def _Mark(self, status):
    """Set a non-committed assignment's terminal status.

    Terminal statuses are not overwritten by later clears; the first event
    that explains why a value did not reach the table is the useful one.
    """
    if self.status == 'pending' and not self.final_cells:
      self.status = status

  def to_dict(self):
    return {
        'line_number': self.line_number,
        'value_name': self.value_name,
        'value': self.value,
        'element_index': self.element_index,
        'status': self.status,
        'kind': self.kind,
        'committed': self.committed,
        'cells': [
            {'record': cell.record_index, 'column': cell.column}
            for cell in self.final_cells
        ],
        'committed_cell': (
            {
                'record': self.final_cells[0].record_index,
                'column': self.final_cells[0].column,
            }
            if self.final_cells
            else None
        ),
        'detail': dict(self.detail),
    }

  def __repr__(self):
    return 'Assignment(line=%r, value_name=%r, value=%r, status=%r)' % (
        self.line_number,
        self.value_name,
        self.value,
        self.status,
    )


class RuleApplication(object):
  """One rule's application to one physical input line.

  A ``Continue`` rule leaves the input line active, so several applications
  can exist for the same physical line.  ``new_state`` and ``state_after``
  describe what the state machine did after the application.
  """

  def __init__(
      self,
      line,
      state,
      rule_index,
      rule_line_number,
      rule_text,
      line_op,
      record_op,
      new_state,
  ):
    self.line = line
    self.state = state
    self.state_after = state
    self.rule_index = rule_index
    self.rule_line_number = rule_line_number
    self.rule_text = rule_text
    self.line_op = line_op
    self.record_op = record_op
    self.new_state = new_state
    self.match_text = None
    self.groups = {}
    self.assignments = []
    self.control_actions = []

  @property
  def line_number(self):
    return self.line.number if self.line is not None else None

  @property
  def continued(self):
    return self.line_op == 'Continue'

  @property
  def changed_state(self):
    return self.state_after != self.state

  @property
  def has_data(self):
    return any(assignment.is_data for assignment in self.assignments)

  def to_dict(self):
    return {
        'line_number': self.line_number,
        'state': self.state,
        'state_after': self.state_after,
        'rule_index': self.rule_index,
        'rule_line_number': self.rule_line_number,
        'rule_text': self.rule_text,
        'line_op': self.line_op,
        'record_op': self.record_op,
        'new_state': self.new_state,
        'match_text': self.match_text,
        'groups': dict(self.groups),
        'assignments': [
            assignment.to_dict() for assignment in self.assignments
        ],
        'control_actions': [
            action.to_dict() for action in self.control_actions
        ],
        'continued': self.continued,
        'changed_state': self.changed_state,
    }

  def __repr__(self):
    return 'RuleApplication(line=%r, state=%r, rule=%r, line_op=%r)' % (
        self.line_number,
        self.state,
        self.rule_line_number,
        self.line_op,
    )


class CellProvenance(object):
  """The origin of one committed table cell.

  ``contributions`` is ordered by the order values entered the cell.  For a
  normal scalar field it has one entry.  For a List cell it has one entry per
  list element, so the same line can appear more than once and several
  different lines may appear.  A Fillup can replace the value in an already
  committed row; the contribution list is updated in that case.
  """

  def __init__(self, record_index, column, value, contributions=None):
    self.record_index = record_index
    self.column = column
    self.value = value
    self.contributions = []
    self.commit = None
    self.element_contributions = []
    if contributions:
      self.set_contributions(contributions)

  @property
  def row(self):
    return self.record_index

  @property
  def source_lines(self):
    """Unique physical line numbers contributing to this cell, in order."""
    result = []
    for assignment in self.contributions:
      number = assignment.line_number
      if number is not None and number not in result:
        result.append(number)
    return result

  @property
  def line_numbers(self):
    return self.source_lines

  @property
  def sources(self):
    """Assignments that contributed to the cell, one per element if a list."""
    return list(self.contributions)

  def set_contributions(self, contributions):
    """Replace the cell's contributors and update their reverse links."""
    for assignment in self.contributions:
      assignment._Detach(self)  # pylint: disable=protected-access
    self.contributions = [a for a in contributions if a is not None]
    for assignment in self.contributions:
      assignment._Attach(self)  # pylint: disable=protected-access

    # element_contributions keeps the one-per-element shape even when the
    # same Assignment object was reused (for example by Filldown).
    self.element_contributions = list(self.contributions)

  @property
  def commit_line(self):
    return self.commit.line_number if self.commit is not None else None

  @property
  def committed_by_eof(self):
    return self.commit is not None and self.commit.reason == 'eof'

  def to_dict(self):
    return {
        'record': self.record_index,
        'column': self.column,
        'value': self.value,
        'source_lines': self.source_lines,
        'commit_line': self.commit_line,
        'contributions': [
            assignment.to_dict() for assignment in self.contributions
        ],
    }

  def __repr__(self):
    return 'CellProvenance(record=%r, column=%r, source_lines=%r)' % (
        self.record_index,
        self.column,
        self.source_lines,
    )


class RecordCommit(object):
  """The event that committed a result row.

  Attributes:
    record_index: int, 0-based index in the parse result.
    line: SourceLine or None.  None means the implicit EOF record.
    reason: str, ``record`` for an explicit Record operator or ``eof`` for
      the implicit end-of-input record.
    application: RuleApplication or None, the rule that recorded the row.
  """

  def __init__(
      self, record_index, line=None, reason='record', application=None
  ):
    self.record_index = record_index
    self.line = line
    self.reason = reason
    self.application = application

  @property
  def line_number(self):
    return self.line.number if self.line is not None else None

  @property
  def eof(self):
    return self.reason == 'eof'

  def to_dict(self):
    return {
        'record': self.record_index,
        'line_number': self.line_number,
        'reason': self.reason,
        'rule_line_number': (
            self.application.rule_line_number
            if self.application is not None
            else None
        ),
    }

  def __repr__(self):
    return 'RecordCommit(record=%r, line=%r, reason=%r)' % (
        self.record_index,
        self.line_number,
        self.reason,
    )


class RecordProvenance(object):
  """The committed values and source lines for one result row."""

  def __init__(self, record_index, commit=None):
    self.record_index = record_index
    self.commit = commit
    self.cells = collections.OrderedDict()

  @property
  def row(self):
    return self.record_index

  @property
  def source_lines(self):
    result = []
    for cell in self.cells.values():
      for number in cell.source_lines:
        if number not in result:
          result.append(number)
    return result

  @property
  def commit_line(self):
    return self.commit.line_number if self.commit is not None else None

  def cell(self, column):
    return self.cells[column]

  def to_dict(self):
    return {
        'record': self.record_index,
        'commit': self.commit.to_dict() if self.commit is not None else None,
        'cells': {
            column: cell.to_dict() for column, cell in self.cells.items()
        },
    }

  def __repr__(self):
    return 'RecordProvenance(record=%r, commit_line=%r)' % (
        self.record_index,
        self.commit_line,
    )


class SourceLine(object):
  """What happened to one physical input line.

  The split between ``control_actions`` and ``data`` is intentional: a rule
  application can both change state and contribute a value, but callers
  asking "where did this line go?" need to see those outcomes separately.
  """

  def __init__(self, number, text):
    self.number = number
    self.text = text
    self.applications = []
    self.assignments = []
    self.control_actions = []
    self.record_commits = []
    self.error = None
    self.parsed = True

  @property
  def index(self):
    """0-based index of the physical line."""
    return self.number - 1

  @property
  def matched(self):
    return bool(self.applications)

  @property
  def disposition(self):
    if not self.parsed:
      return LineDisposition.UNPARSED
    if not self.applications:
      return LineDisposition.UNMATCHED

    data_assignments = [
        assignment
        for assignment in self.assignments
        if assignment.is_data and assignment.committed
    ]
    if data_assignments:
      return LineDisposition.DATA

    if any(
        assignment.is_data for assignment in self.assignments
    ):
      return LineDisposition.DISCARDED

    return LineDisposition.CONTROL

  @property
  def kind(self):
    return self.disposition

  @property
  def data_assignments(self):
    return [
        assignment
        for assignment in self.assignments
        if assignment.is_data and assignment.committed
    ]

  @property
  def discarded_assignments(self):
    return [
        assignment
        for assignment in self.assignments
        if assignment.is_data and not assignment.committed
    ]

  @property
  def data(self):
    """Final table cells to which this line contributed."""
    result = []
    for assignment in self.data_assignments:
      for cell in assignment.final_cells:
        if cell not in result:
          result.append(cell)
    return result

  @property
  def final_cells(self):
    return self.data

  @property
  def control(self):
    return list(self.control_actions)

  @property
  def has_control(self):
    return bool(self.control_actions)

  @property
  def has_data(self):
    return bool(self.data_assignments)

  @property
  def changed_state(self):
    return any(application.changed_state for application in self.applications)

  @property
  def destination(self):
    """Alias for ``disposition``."""
    return self.disposition

  @property
  def outcome(self):
    """Alias for ``disposition``."""
    return self.disposition

  @property
  def actions(self):
    """Alias for the control actions caused by this line."""
    return list(self.control_actions)

  def explain(self):
    """Return a compact, serialisable explanation of this line's fate."""
    return {
        'line_number': self.number,
        'text': self.text,
        'disposition': self.disposition,
        'matched': self.matched,
        'control_actions': [
            action.to_dict() for action in self.control_actions
        ],
        'data': [cell.to_dict() for cell in self.data],
        'discarded': [
            assignment.to_dict()
            for assignment in self.discarded_assignments
        ],
        'applications': [
            application.to_dict() for application in self.applications
        ],
        'record_commits': [
            commit.to_dict() for commit in self.record_commits
        ],
        'error': self.error,
    }

  def to_dict(self):
    return self.explain()

  def __repr__(self):
    return 'SourceLine(number=%r, disposition=%r)' % (
        self.number,
        self.disposition,
    )


class ParseTrace(object):
  """A complete line-level account of one offline parse.

  Public lookups:

    trace.line(3)                    SourceLine for physical line 3
    trace.where(3)                   'unmatched'/'control'/'data'/'discarded'
    trace.cell(0, 'model')           CellProvenance for result row 0
    trace.cell_sources(0, 'model')   contributing physical line numbers
    trace.record(0)                  RecordProvenance for result row 0
    trace.explain_line(3)            serialisable line explanation
  """

  def __init__(self):
    self.lines = []
    self.records = []
    self.result = None
    self.error = None
    self._lines_by_number = {}
    self._records_by_index = {}
    self._headers = []

  def add_line(self, number, text):
    source_line = SourceLine(number, text)
    self.lines.append(source_line)
    self._lines_by_number[number] = source_line
    return source_line

  def add_record(self, record_index, commit, headers=None):
    record = RecordProvenance(record_index, commit=commit)
    self.records.append(record)
    self._records_by_index[record_index] = record
    if headers:
      self._headers = list(headers)
    return record

  def line(self, number):
    """Return the SourceLine for a 1-based physical line number."""
    try:
      return self._lines_by_number[number]
    except KeyError as exc:
      raise IndexError('No physical input line %r.' % number) from exc

  line_trace = line

  def record(self, record_index):
    """Return the RecordProvenance for a 0-based result row index."""
    try:
      return self._records_by_index[record_index]
    except KeyError as exc:
      raise IndexError('No result row %r.' % record_index) from exc

  def records_for_line(self, number):
    """Return committed records that contain data from a physical line."""
    result = []
    for cell in self.line(number).data:
      record = self.record(cell.record_index)
      if record not in result:
        result.append(record)
    return result

  def line_records(self, number):
    return self.records_for_line(number)

  def lines_for_record(self, record_index):
    """Return physical line numbers that contributed to a result row."""
    return self.record(record_index).source_lines

  def cells_for_line(self, number):
    """Return the committed cells a physical line contributed to."""
    return list(self.line(number).data)

  def cell(self, record_index, column):
    """Return the CellProvenance for a result row and column.

    Args:
      record_index: int, 0-based result row index.
      column: str column name, or int column position.
    """
    record = self.record(record_index)
    if isinstance(column, int):
      if not 0 <= column < len(self._headers):
        raise IndexError('No column index %r.' % column)
      column = self._headers[column]
    if column not in record.cells:
      # Empty columns are still cells even if nothing was assigned.
      if column in self._headers:
        return CellProvenance(record_index, column, None)
      raise IndexError(
          'No column %r in result row %r.' % (column, record_index)
      )
    return record.cells[column]

  def cell_sources(self, record_index, column):
    """Return contributing physical line numbers for a cell."""
    return self.cell(record_index, column).source_lines

  source_lines = cell_sources
  lines_for_cell = cell_sources

  def where(self, number):
    """Return the LineDisposition for a 1-based physical line number."""
    return self.line(number).disposition

  def explain_line(self, number):
    return self.line(number).explain()

  @property
  def headers(self):
    return list(self._headers)

  def summary(self):
    """Return counts for each line disposition."""
    counts = {value: 0 for value in LineDisposition.VALUES}
    for source_line in self.lines:
      counts[source_line.disposition] += 1
    return counts

  def to_dict(self):
    return {
        'lines': [source_line.to_dict() for source_line in self.lines],
        'records': [record.to_dict() for record in self.records],
        'headers': self.headers,
        'summary': self.summary(),
        'error': self.error,
    }

  def __len__(self):
    return len(self.lines)

  def __iter__(self):
    return iter(self.lines)

  def __repr__(self):
    return 'ParseTrace(lines=%r, records=%r)' % (
        len(self.lines),
        len(self.records),
    )
