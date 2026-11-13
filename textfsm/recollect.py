"""Builds a re-collection plan after an index or template revision.

Old inspection runs stored parsed tables, but only some devices kept the
raw command output. After the index file or the templates change, this
module works out which (device, command) pairs genuinely need to be
re-collected from the network:

* If the stored fields already cover everything the new templates produce,
  the new result can be derived losslessly from the saved table, so the
  device is not touched.
* Otherwise, if the raw command output was kept, it is re-parsed locally
  with the new templates.
* Only what cannot be proven either way lands on the re-collection list,
  de-duplicated by (device, command) since several saved tables may have
  been produced from a single command. Each entry lists exactly which
  fields the re-collection is expected to fill in.
"""

import os

import textfsm
from textfsm import clitable
from textfsm import texttable


class Error(Exception):
  """Base class for errors."""


class RecollectError(Error):
  """General recollection planning error."""


class ArchivedTable(object):
  """One parsed table saved by a previous inspection run.

  Attributes:
    device: String, hostname of the device the table came from.
    command: String, command whose output was parsed into this table.
    fields: List of column names stored in the saved table.
    attributes: Dict of extra index-matching attributes (e.g. Vendor).
    raw: String, raw command output if it was kept, else None.
  """

  def __init__(self, device, command, fields, attributes=None, raw=None):
    self.device = device
    self.command = command
    self.fields = list(fields)
    self.attributes = dict(attributes) if attributes else {}
    self.raw = raw

  def __repr__(self):
    return 'ArchivedTable(%r, %r)' % (self.device, self.command)


class RecollectEntry(object):
  """One line of the re-collection list.

  Attributes:
    device: String, device to log in to.
    command: String, command to re-run.
    missing: Sorted list of fields the re-collection must supply, or None
      if the required fields could not be determined.
  """

  def __init__(self, device, command, missing):
    self.device = device
    self.command = command
    self.missing = missing

  def __repr__(self):
    return 'RecollectEntry(%r, %r, %r)' % (
        self.device, self.command, self.missing)


class RecollectPlan(object):
  """Computes which device/command pairs must be re-collected.

  Attributes:
    derivable: List of ArchivedTable, stored fields cover the new templates.
    reparsed: List of (ArchivedTable, CliTable) tuples re-parsed locally
      from kept raw output with the new templates.
    obsolete: List of ArchivedTable, command no longer matches the index.
    template_dir: String, directory holding the index and template files.
  """

  def __init__(self, index_file=None, template_dir=None):
    """Create new RecollectPlan object.

    Args:
      index_file: String, new version of the command/template index file.
      template_dir: String, directory where index and templates reside.
    """
    self._cli = clitable.CliTable(index_file, template_dir)
    self.template_dir = template_dir
    self._archived = []
    self._recollect = {}
    self.derivable = []
    self.reparsed = []
    self.obsolete = []

  def Add(self, archived):
    """Adds one saved table from the old inspection run.

    Args:
      archived: ArchivedTable, a parsed table kept from the old run.
    """
    self._archived.append(archived)

  def Build(self):
    """Classifies every added table and returns the plan."""
    for archived in self._archived:
      self._Classify(archived)
    return self

  @property
  def recollect(self):
    """Returns the re-collection list, sorted by device then command."""
    entries = []
    for (device, command), missing in self._recollect.items():
      if missing is not None:
        missing = sorted(missing)
      entries.append(RecollectEntry(device, command, missing))
    entries.sort(key=lambda entry: (entry.device, entry.command))
    return entries

  def RecollectTable(self):
    """Renders the re-collection list as a TextTable."""
    table = texttable.TextTable()
    table.header = ['Device', 'Command', 'MissingFields']
    for entry in self.recollect:
      missing = '*' if entry.missing is None else ', '.join(entry.missing)
      table.Append([entry.device, entry.command, missing])
    return table

  def _Attributes(self, archived):
    """Builds the index-matching attributes for a saved table."""
    attributes = {'Hostname': archived.device, 'Command': archived.command}
    attributes.update(archived.attributes)
    return attributes

  def _Classify(self, archived):
    """Sorts one saved table into derivable/reparsed/recollect/obsolete."""
    attributes = self._Attributes(archived)
    row_idx = self._cli.index.GetRowMatch(attributes)
    if not row_idx:
      # The new index no longer parses this command, nothing to re-collect.
      self.obsolete.append(archived)
      return

    templates = self._cli.index.index[row_idx]['Template']
    try:
      required = self._TemplateFields(templates)
    except (IOError, textfsm.Error):
      # Template unreadable: cannot prove what is missing, re-collect it.
      self._Recollect(archived, None)
      return

    missing = [field for field in required if field not in archived.fields]
    if not missing:
      # Stored fields are a superset, derive the new table locally.
      self.derivable.append(archived)
      return

    if archived.raw is not None:
      try:
        reparsed = self._Reparse(archived, attributes)
      except (IOError, clitable.Error, textfsm.Error):
        reparsed = None
      if reparsed is not None:
        self.reparsed.append((archived, reparsed))
        return

    self._Recollect(archived, missing)

  def _Recollect(self, archived, missing):
    """Merges one table's shortfall into the (device, command) entry."""
    key = (archived.device, archived.command)
    if key in self._recollect:
      if self._recollect[key] is None or missing is None:
        # Unknown required fields dominate anything already recorded.
        self._recollect[key] = None
      else:
        self._recollect[key].update(missing)
    else:
      self._recollect[key] = None if missing is None else set(missing)

  def _TemplateFields(self, templates):
    """Returns the ordered union of headers of a template list."""
    fields = []
    for name in templates.split(':'):
      with open(os.path.join(self.template_dir, name), 'r') as template_file:
        fsm = textfsm.TextFSM(template_file)
      for header in fsm.header:
        if header not in fields:
          fields.append(header)
    return fields

  def _Reparse(self, archived, attributes):
    """Re-parses kept raw output locally with the new templates."""
    cli = clitable.CliTable()
    cli.index = self._cli.index
    cli.template_dir = self.template_dir
    cli.ParseCmd(archived.raw, attributes=attributes)
    return cli
