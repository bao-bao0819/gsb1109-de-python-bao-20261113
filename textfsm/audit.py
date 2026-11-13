"""Offline cross-check for CLI echoes and TextFSM templates.

This module does not replace the normal TextFSM/CliTable parse path. It
provides an offline verification entry point for cases where:

* More than one index row matches the supplied attributes (for example, a
  device model has been truncated, so the first matching row is ambiguous).
* More than one template can read the same echo.
* A template returns zero records. That is not proof that the device has no
  entities unless the echo actually hits the template's own End/EOF marker.
* Two templates use different names for the same logical field. A caller
  supplied field map can confirm the equivalence.

The result deliberately separates columns that every compared template
confirms from columns that exist only in, or disagree between, templates.

Programmatic use::

    from textfsm import audit

    report = audit.audit_echo(
        echo,
        index_file='index',
        template_dir='templates',
        attributes={'Vendor': 'Cisco', 'Command': 'show version'},
        field_map={'Model': 'Chassis'},
    )
    print(report.format_text())

Command line use::

    python -m textfsm.audit echo.txt --template-dir templates \\
        --index index --attribute Vendor=Cisco \\
        --attribute Command='show version' --json
"""

import argparse
import json
import os
import re

from textfsm import clitable
from textfsm import parser


class AuditError(Exception):
  """Base class for audit errors."""


TEMPLATE_ERRORS = (parser.TextFSMError, parser.TextFSMTemplateError)


class TemplateAudit(object):
  """Parse result and end-marker evidence for one template."""

  def __init__(
      self,
      name,
      path,
      index_rows=(),
      headers=(),
      records=(),
      terminated=False,
      termination_state=None,
      end_marker=None,
      end_marker_rule=None,
      end_marker_rule_line=None,
      declared_end_states=(),
      end_marker_rules=(),
      status='parsed',
      error=None,
  ):
    self.name = name
    self.path = path
    self.index_rows = tuple(index_rows)
    self.headers = tuple(headers)
    self.records = list(records)
    self.terminated = terminated
    self.termination_state = termination_state
    self.end_marker = end_marker
    self.end_marker_rule = end_marker_rule
    self.end_marker_rule_line = end_marker_rule_line
    self.declared_end_states = tuple(declared_end_states)
    self.end_marker_rules = tuple(end_marker_rules)
    self.status = status
    self.error = error

  @property
  def row_count(self):
    return len(self.records)

  @property
  def empty_status(self):
    """Returns how a zero-record result should be interpreted.

    None means the template returned at least one record. A confirmed empty
    result requires an explicit End/EOF transition in the echo; merely
    declaring an empty EOF state is not evidence that it was reached.
    """
    if self.status == 'missing':
      return 'template-missing'
    if self.status == 'error':
      return 'template-error'
    if self.row_count:
      return None
    if self.terminated:
      return 'confirmed-empty'
    if self.end_marker_rules:
      return 'unverified-missing-end-marker'
    return 'unverified-template-has-no-end-marker'

  @property
  def empty_confirmed(self):
    return self.empty_status == 'confirmed-empty'

  @property
  def complete(self):
    """Whether the template reached its explicit end state."""
    return bool(self.terminated)

  def to_dict(self):
    return {
        'name': self.name,
        'path': self.path,
        'index_rows': list(self.index_rows),
        'status': self.status,
        'error': self.error,
        'headers': list(self.headers),
        'row_count': self.row_count,
        'records': [dict(record) for record in self.records],
        'terminated': self.terminated,
        'termination_state': self.termination_state,
        'end_marker': self.end_marker,
        'end_marker_rule': self.end_marker_rule,
        'end_marker_rule_line': self.end_marker_rule_line,
        'declared_end_states': list(self.declared_end_states),
        'end_marker_rules': list(self.end_marker_rules),
        'complete': self.complete,
        'empty_status': self.empty_status,
        'empty_confirmed': self.empty_confirmed,
    }


class ColumnAudit(object):
  """Comparison of one logical column across templates."""

  def __init__(
      self,
      canonical,
      status='determined',
      reason='same-field-in-all-templates',
      templates=(),
      aliases=None,
      values=None,
      value_consistent=None,
  ):
    self.canonical = canonical
    self.status = status
    self.reason = reason
    self.templates = tuple(templates)
    self.aliases = dict(aliases or {})
    self.values = dict(values or {})
    self.value_consistent = value_consistent

  @property
  def determined(self):
    return self.status == 'determined'

  @property
  def template_dependent(self):
    return self.status == 'template-dependent'

  def to_dict(self):
    return {
        'column': self.canonical,
        'status': self.status,
        'reason': self.reason,
        'templates': list(self.templates),
        'aliases': dict(self.aliases),
        'values': {key: list(value) for key, value in self.values.items()},
        'value_consistent': self.value_consistent,
    }


class CandidateAudit(object):
  """One plausible index row and the columns its template list provides."""

  def __init__(
      self,
      row,
      fields=None,
      templates=(),
      selected=False,
      match_kind='regex',
      template_names=(),
      determined_columns=(),
      template_dependent_columns=(),
  ):
    self.row = row
    self.fields = dict(fields or {})
    self.templates = tuple(templates)
    self.selected = selected
    self.match_kind = match_kind
    self.template_names = tuple(template_names)
    self.determined_columns = tuple(determined_columns)
    self.template_dependent_columns = tuple(template_dependent_columns)

  def to_dict(self):
    return {
        'row': self.row,
        'selected': self.selected,
        'match_kind': self.match_kind,
        'fields': dict(self.fields),
        'templates': list(self.templates),
        'parsed_templates': list(self.template_names),
        'determined_columns': [
            column.to_dict() for column in self.determined_columns
        ],
        'template_dependent_columns': [
            column.to_dict() for column in self.template_dependent_columns
        ],
    }


class AuditReport(object):
  """Structured offline audit result."""

  def __init__(
      self,
      echo_source,
      echo_text,
      index_file=None,
      template_dir=None,
      attributes=None,
      candidates=(),
      templates=(),
      columns=(),
      explicit_templates=(),
      warnings=(),
      no_index_match=False,
      index_selected_row=None,
  ):
    self.echo_source = echo_source
    self.echo_text = echo_text
    self.index_file = index_file
    self.template_dir = template_dir
    self.attributes = dict(attributes or {})
    self.candidates = list(candidates)
    self.templates = list(templates)
    self.columns = list(columns)
    self.explicit_templates = tuple(explicit_templates)
    self.warnings = list(warnings)
    self.no_index_match = no_index_match
    self.index_selected_row = index_selected_row

  @property
  def index_candidates(self):
    return [candidate for candidate in self.candidates if candidate.row]

  @property
  def selected_candidate(self):
    for candidate in self.candidates:
      if candidate.selected:
        return candidate
    return self.candidates[0] if self.candidates else None

  @property
  def selected_row(self):
    return self.index_selected_row

  @property
  def ambiguous_index(self):
    return len(self.index_candidates) > 1

  @property
  def determined_columns(self):
    return [column for column in self.columns if column.status == 'determined']

  @property
  def template_dependent_columns(self):
    return [
        column
        for column in self.columns
        if column.status == 'template-dependent'
    ]

  @property
  def stable_columns(self):
    return self.determined_columns

  @property
  def confirmed_empty_templates(self):
    return [
        template.name
        for template in self.templates
        if template.empty_status == 'confirmed-empty'
    ]

  @property
  def unverified_empty_templates(self):
    return [
        template.name
        for template in self.templates
        if template.empty_status
        and template.empty_status.startswith('unverified')
    ]

  @property
  def incomplete_templates(self):
    return [
        template.name
        for template in self.templates
        if template.status == 'parsed'
        and not template.terminated
        and (template.declared_end_states or template.end_marker_rules)
    ]

  def to_dict(self):
    candidate_dicts = [candidate.to_dict() for candidate in self.candidates]
    determined = [column.to_dict() for column in self.determined_columns]
    dependent = [
        column.to_dict() for column in self.template_dependent_columns
    ]
    return {
        'echo': {
            'source': self.echo_source,
            'line_count': len(self.echo_text.splitlines()),
        },
        'index': {
            'file': self.index_file,
            'template_dir': self.template_dir,
            'attributes': dict(self.attributes),
            'matched_rows': [
                candidate.row for candidate in self.index_candidates
            ],
            'selected_row': self.index_selected_row,
            'selection_rule': 'first-match',
            'ambiguous': self.ambiguous_index,
            'no_match': self.no_index_match,
        },
        'candidates': candidate_dicts,
        'templates': [template.to_dict() for template in self.templates],
        'explicit_templates': list(self.explicit_templates),
        'columns': {
            'determined': determined,
            'template_dependent': dependent,
        },
        'determined_columns': determined,
        'stable_columns': determined,
        'template_dependent_columns': dependent,
        'summary': {
            'ambiguous_index': self.ambiguous_index,
            'selected_row': self.index_selected_row,
            'confirmed_empty_templates': self.confirmed_empty_templates,
            'unverified_empty_templates': self.unverified_empty_templates,
            'incomplete_templates': self.incomplete_templates,
        },
        'warnings': list(self.warnings),
    }

  def as_dict(self):
    return self.to_dict()

  def to_json(self, indent=2, sort_keys=False):
    return json.dumps(
        self.to_dict(), indent=indent, sort_keys=sort_keys, ensure_ascii=False
    )

  def format_text(self):
    """Returns a concise, human-readable audit report."""
    lines = []
    source = self.echo_source or '<stdin>'
    lines.append('Offline audit: %s (%d lines)' % (
        source, len(self.echo_text.splitlines())))
    if self.index_file:
      lines.append('Index: %s' % self.index_file)
    if self.attributes:
      lines.append('Attributes: %s' % _format_attributes(self.attributes))

    if self.no_index_match:
      lines.append('Index matches: 0 (no template selected)')
    elif self.index_candidates:
      note = 'ambiguous, current first-match selection' if (
          self.ambiguous_index) else 'unique'
      lines.append(
          'Index matches: %d (%s; selected row %s)'
          % (len(self.index_candidates), note, self.index_selected_row)
      )
      for candidate in self.index_candidates:
        marker = '*' if candidate.selected else ' '
        lines.append(
            '  %s row %s  templates=%s  fields=%s'
            % (
                marker,
                candidate.row,
                ':'.join(candidate.templates) or '<none>',
                _format_attributes(candidate.fields),
            )
        )
    else:
      lines.append('Index matches: n/a')

    if self.explicit_templates:
      lines.append(
          'Explicit templates: %s' % ':'.join(self.explicit_templates)
      )
    lines.append('')
    lines.append('Templates:')
    if not self.templates:
      lines.append('  <none>')
    for template in self.templates:
      if template.status in ('missing', 'error'):
        lines.append('  %s: %s (%s)' % (
            template.name, template.status, template.error))
        continue
      lines.append(
          '  %s: rows=%d terminated=%s state=%s marker=%s'
          % (
              template.name,
              template.row_count,
              _yes_no(template.terminated),
              template.termination_state or '-',
              repr(template.end_marker) if template.end_marker else '-',
          )
      )
      if template.empty_status:
        lines.append('    zero-row verdict: %s' % template.empty_status)

    lines.append('')
    lines.append('Determined columns:')
    if not self.determined_columns:
      lines.append('  <none>')
    for column in self.determined_columns:
      alias_note = ''
      if column.aliases:
        alias_note = ' (aliases: %s)' % _format_attributes(column.aliases)
      value_note = ''
      if column.value_consistent is False:
        value_note = ', values differ'
      elif column.value_consistent is None:
        value_note = ', schema only (no records)'
      lines.append(
          '  %s: in %s%s%s'
          % (
              column.canonical,
              ', '.join(column.templates),
              alias_note,
              value_note,
          )
      )

    lines.append('')
    lines.append('Template-dependent columns:')
    if not self.template_dependent_columns:
      lines.append('  <none>')
    for column in self.template_dependent_columns:
      lines.append(
          '  %s: %s (in %s)'
          % (
              column.canonical,
              column.reason,
              ', '.join(column.templates) or '<none>',
          )
      )

    if self.warnings:
      lines.append('')
      lines.append('Warnings:')
      for warning in self.warnings:
        lines.append('  - %s' % warning)
    return '\n'.join(lines)

  def format(self):
    return self.format_text()

  def __str__(self):
    return self.format_text()


class OfflineAudit(object):
  """Builds an offline audit for an echo, an index and its templates.

  Args:
    echo: String, the captured CLI echo.
    index_file: String, optional index file path passed to CliTable.
    template_dir: String, directory containing the index and templates.
    attributes: Dict, index attributes used for matching.
    field_map: Field equivalence map. Accepts a dict of canonical -> alias(es)
      or an iterable of (canonical, alias) pairs. See `normalize_field_map`.
    templates: Optional iterable of template names. If supplied, these are
      parsed even when no index row matches.
    echo_source: Optional display name for the echo.
  """

  def __init__(
      self,
      echo,
      index_file=None,
      template_dir=None,
      attributes=None,
      field_map=None,
      templates=None,
      echo_source=None,
  ):
    self.echo = echo or ''
    self.index_file = index_file
    self.template_dir = template_dir
    self.attributes = dict(attributes or {})
    self.field_map = normalize_field_map(field_map)
    self.templates = _split_templates(templates) if templates else ()
    self.echo_source = echo_source
    self._template_cache = {}
    self._index_rows = None

  def run(self):
    warnings = []
    candidates = []
    template_index_rows = {}
    no_index_match = False
    index_candidate_count = 0

    if self.index_file:
      try:
        rows, index_table = self._read_index()
      except (OSError, clitable.CliTableError, clitable.IndexTableError,
              re.error) as exc:
        raise AuditError('Unable to read index: %s' % exc) from exc
      self._index_rows = index_table
      matches = index_table.index.GetRowMatches(self.attributes)
      no_index_match = not matches
      if not matches:
        warnings.append(
            'No index row matched the supplied attributes; no template was '
            'selected by the index.'
        )
      for position, row_number in enumerate(matches):
        row = rows.get(row_number)
        if row is None:
          continue
        fields = {
            column: row[column]
            for column in row
            if column != 'Template'
        }
        template_names = _split_templates(row['Template'])
        candidates.append(
            CandidateAudit(
                row=row_number,
                fields=fields,
                templates=template_names,
                selected=(position == 0),
            )
        )
        index_candidate_count += 1
      if len(matches) > 1:
        warnings.append(
            'Index matched %d rows; existing first-match behaviour selects '
            'row %s, which may not be the correct device.'
            % (len(matches), matches[0])
        )
    elif not self.templates:
      raise AuditError(
          'An index file or at least one template is required for the audit.'
      )

    if self.templates:
      explicit_names = list(self.templates)
      explicit_candidate = CandidateAudit(
          row=0,
          fields={'Template': ':'.join(explicit_names)},
          templates=explicit_names,
      )
      if index_candidate_count:
        candidates.insert(0, explicit_candidate)
      else:
        explicit_candidate.selected = True
        candidates.append(explicit_candidate)
        for position, candidate in enumerate(candidates):
          if candidate is not explicit_candidate:
            candidate.selected = False

    all_names = []
    for candidate in candidates:
      for name in candidate.templates:
        if name not in all_names:
          all_names.append(name)

    reports = []
    for name in all_names:
      template_index_rows.setdefault(name, [])
      report = self._audit_template(name)
      reports.append(report)
      self._template_cache[name] = report

    for candidate in candidates:
      for name in candidate.templates:
        if name not in template_index_rows:
          template_index_rows[name] = []
        if candidate.row:
          template_index_rows[name].append(candidate.row)
      parsed_names = [
          name
          for name in candidate.templates
          if name in self._template_cache
          and self._template_cache[name].status in ('parsed', 'empty')
      ]
      determined, dependent = self._compare_columns(parsed_names)
      candidate.template_names = parsed_names
      candidate.determined_columns = determined
      candidate.template_dependent_columns = dependent

    parsed_all_names = [
        name
        for name in all_names
        if self._template_cache[name].status in ('parsed', 'empty')
    ]
    determined_all, dependent_all = self._compare_columns(parsed_all_names)
    report_columns = determined_all + dependent_all

    self._attach_index_rows(reports, template_index_rows)
    self._add_template_warnings(reports, warnings)

    return AuditReport(
        echo_source=self.echo_source,
        echo_text=self.echo,
        index_file=self.index_file,
        template_dir=self.template_dir,
        attributes=self.attributes,
        candidates=candidates,
        templates=reports,
        columns=report_columns,
        explicit_templates=self.templates,
        warnings=warnings,
        no_index_match=no_index_match,
        index_selected_row=next(
            (
                candidate.row
                for candidate in candidates
                if candidate.selected and candidate.row
            ),
            None,
        ),
    )

  def report(self):
    return self.run()

  def _read_index(self):
    template_dir = self.template_dir
    if template_dir is None:
      template_dir = os.path.dirname(self.index_file) or '.'
    table = clitable.CliTable(self.index_file, template_dir)
    rows = {}
    for row in table.index.index:
      rows[row.row] = {
          column: row[column] for column in row.header
      }
    return rows, table

  def _resolve_template_path(self, name):
    if os.path.isabs(name):
      return name
    template_dir = self.template_dir
    if template_dir is None:
      if self.index_file:
        template_dir = os.path.dirname(self.index_file)
      else:
        template_dir = '.'
    return os.path.join(template_dir or '.', name)

  def _audit_template(self, name):
    path = self._resolve_template_path(name)
    try:
      handle = open(path, 'r')
    except OSError as exc:
      return TemplateAudit(
          name=name,
          path=path,
          status='missing',
          error=str(exc),
      )

    try:
      with handle:
        fsm = parser.TextFSM(handle)
        headers = list(fsm.header)
        result = fsm.ParseText(self.echo)
        records = []
        for row in result:
          records.append(dict(zip(headers, row)))
        return TemplateAudit(
            name=name,
            path=path,
            headers=headers,
            records=records,
            terminated=fsm.last_parse_terminated,
            termination_state=fsm.last_parse_termination_state,
            end_marker=fsm.last_parse_termination_line,
            end_marker_rule=(
                str(fsm.last_parse_termination_rule)
                if fsm.last_parse_termination_rule
                else None
            ),
            end_marker_rule_line=(
                fsm.last_parse_termination_rule.line_num
                if fsm.last_parse_termination_rule
                else None
            ),
            declared_end_states=fsm.explicit_end_states,
            end_marker_rules=[str(rule) for rule in fsm.end_marker_rules],
            status='parsed' if records else 'empty',
        )
    except TEMPLATE_ERRORS as exc:
      return TemplateAudit(
          name=name,
          path=path,
          status='error',
          error=str(exc),
      )

  def _compare_columns(self, names):
    """Compares declared fields and record values across templates."""
    if not names:
      return [], []

    field_headers = {}
    for name in names:
      report = self._template_cache[name]
      field_headers[name] = {
          self.field_map.resolve(header): header for header in report.headers
      }

    all_canonicals = []
    for name in names:
      report = self._template_cache[name]
      for header in report.headers:
        canonical = self.field_map.resolve(header)
        if canonical not in all_canonicals:
          all_canonicals.append(canonical)

    columns = []
    for canonical in all_canonicals:
      present = []
      aliases = {}
      values = {}
      for name in names:
        report = self._template_cache[name]
        if canonical not in field_headers[name]:
          continue
        actual = field_headers[name][canonical]
        present.append(name)
        if actual != canonical:
          aliases[name] = actual
        values[name] = [
            record.get(actual, '') for record in report.records
        ]

      if len(present) != len(names):
        missing = [name for name in names if name not in present]
        columns.append(
            ColumnAudit(
                canonical=canonical,
                status='template-dependent',
                reason='missing-from-template:%s' % ','.join(missing),
                templates=present,
                aliases=aliases,
                values=values,
                value_consistent=False,
            )
        )
        continue

      consistent = _values_agree(values)
      if consistent is False:
        columns.append(
            ColumnAudit(
                canonical=canonical,
                status='template-dependent',
                reason='value-mismatch',
                templates=present,
                aliases=aliases,
                values=values,
                value_consistent=False,
            )
        )
      else:
        columns.append(
            ColumnAudit(
                canonical=canonical,
                status='determined',
                reason=(
                    'same-field-in-all-templates'
                    if not aliases
                    else 'same-field-in-all-templates-via-alias'
                ),
                templates=present,
                aliases=aliases,
                values=values,
                value_consistent=consistent,
            )
        )

    determined = [
        column for column in columns if column.status == 'determined'
    ]
    dependent = [
        column
        for column in columns
        if column.status == 'template-dependent'
    ]
    return determined, dependent

  def _attach_index_rows(self, reports, template_index_rows):
    for report in reports:
      report.index_rows = tuple(template_index_rows.get(report.name, ()))

  def _add_template_warnings(self, reports, warnings):
    for template in reports:
      if template.status == 'missing':
        warnings.append(
            'Template %s is missing; its fields and rows are unverified.'
            % template.name
        )
      elif template.status == 'error':
        warnings.append(
            'Template %s could not be parsed: %s'
            % (template.name, template.error)
        )
      elif template.empty_status == 'confirmed-empty':
        warnings.append(
            'Template %s returned zero records and matched its own end '
            'marker; an empty result is confirmed for this template.'
            % template.name
        )
      elif template.empty_status and template.empty_status.startswith(
          'unverified'
      ):
        warnings.append(
            'Template %s returned zero records without matching an end '
            'marker; absence is NOT confirmed (the echo may be truncated or '
            'not recognised by this template).' % template.name
        )
      elif template.status == 'parsed' and not template.terminated:
        if template.declared_end_states or template.end_marker_rules:
          warnings.append(
              'Template %s returned %d records but did not reach its declared '
              'end marker; the result may be incomplete.'
              % (template.name, template.row_count)
          )


class FieldMap(object):
  """Resolves field-name aliases to a canonical field name."""

  def __init__(self, alias_to_canonical=None):
    self._alias_to_canonical = dict(alias_to_canonical or {})
    self._normalized = {
        _normalize_group_name(alias): canonical
        for alias, canonical in self._alias_to_canonical.items()
    }

  def resolve(self, name):
    if name in self._alias_to_canonical:
      return self._alias_to_canonical[name]
    return self._normalized.get(_normalize_group_name(name), name)

  def canonical(self, name):
    return self.resolve(name)

  def aliases_of(self, canonical):
    canonical = self.resolve(canonical)
    return [
        alias
        for alias, mapped in self._alias_to_canonical.items()
        if mapped == canonical and alias != canonical
    ]


def _normalize_group_name(value):
  return value.strip().lower().replace(' ', '').replace('_', '')


def normalize_field_map(field_map):
  """Builds a FieldMap from common mapping forms.

  Accepted forms:
    * {'Model': ['Chassis', 'ChassisType']}  (canonical first)
    * {'Model': 'Chassis'}
    * [('Model', 'Chassis'), ...]
    * ['Model=Chassis', 'Model:Chassis', 'Model->Chassis']
    * {'fields': <one of the above>}

  The left-hand name is the canonical name. Alias groups are merged
  transitively, so A=B and B=C all resolve to the same canonical field.
  """
  if field_map is None:
    return FieldMap()
  if isinstance(field_map, FieldMap):
    return field_map

  pairs = []
  if isinstance(field_map, dict):
    if 'fields' in field_map and len(field_map) == 1:
      return normalize_field_map(field_map['fields'])
    for canonical, aliases in field_map.items():
      if isinstance(aliases, str):
        pairs.append((canonical, aliases))
      else:
        for alias in aliases:
          pairs.append((canonical, alias))
  else:
    for item in field_map:
      if isinstance(item, str):
        canonical, alias = _parse_field_pair(item)
        pairs.append((canonical, alias))
      elif isinstance(item, dict):
        canonical = item.get('canonical') or item.get('field')
        aliases = item.get('aliases', item.get('alias', ()))
        if isinstance(aliases, str):
          aliases = (aliases,)
        for alias in aliases:
          pairs.append((canonical, alias))
      else:
        canonical, alias = item
        pairs.append((canonical, alias))

  parent = {}
  original_names = {}
  canonical_order = []

  def normalize(name):
    return name.strip().lower().replace(' ', '').replace('_', '')

  def find(name):
    parent.setdefault(name, name)
    while parent[name] != name:
      parent[name] = parent[parent[name]]
      name = parent[name]
    return name

  def union(left, right):
    left_root = find(left)
    right_root = find(right)
    if left_root != right_root:
      parent[right_root] = left_root

  for canonical, alias in pairs:
    if not canonical or not alias:
      continue
    canonical = canonical.strip()
    alias = alias.strip()
    if not canonical or not alias:
      continue
    normalized_canonical = normalize(canonical)
    normalized_alias = normalize(alias)
    original_names.setdefault(normalized_canonical, canonical)
    original_names.setdefault(normalized_alias, alias)
    if normalized_canonical not in canonical_order:
      canonical_order.append(normalized_canonical)
    union(normalized_canonical, normalized_alias)

  groups = {}
  for normalized, original in original_names.items():
    root = find(normalized)
    groups.setdefault(root, {})
    groups[root][normalized] = original

  canonical_for_root = {}
  for normalized in canonical_order:
    canonical_for_root.setdefault(find(normalized), normalized)

  alias_to_canonical = {}
  for root, group in groups.items():
    canonical_normalized = canonical_for_root.get(root)
    if canonical_normalized is None:
      canonical_normalized = sorted(group)[0]
    canonical = group.get(canonical_normalized, original_names[
        canonical_normalized])
    alias_to_canonical[canonical_normalized] = canonical
    alias_to_canonical[canonical] = canonical
    for normalized, original in group.items():
      alias_to_canonical[normalized] = canonical
      alias_to_canonical[original] = canonical
  return FieldMap(alias_to_canonical)


def _parse_field_pair(value):
  for separator in ('->', ':', '='):
    if separator in value:
      left, right = value.split(separator, 1)
      return left.strip(), right.strip()
  raise AuditError(
      'Invalid field map entry %r; expected CANONICAL=ALIAS.' % value
  )


def _split_templates(templates):
  """Accepts a string, colon/comma list, or iterable of template names."""
  if not templates:
    return ()
  if isinstance(templates, str):
    values = [templates]
  else:
    values = list(templates)
  names = []
  for value in values:
    for name in re.split(r'[:,]', value):
      name = name.strip()
      if name:
        names.append(name)
  return tuple(names)


def _values_agree(values):
  """Returns True/False/None for cross-template record values.

  Only templates that produced at least one record can contribute value
  evidence. A single producing template is schema-level agreement only,
  reported as None rather than as a confirmed match.
  """
  if not values:
    return None
  reference = None
  compared = 0
  for template_values in values.values():
    if not template_values:
      continue
    if reference is None:
      reference = template_values
    elif template_values != reference:
      return False
    else:
      compared += 1
  if reference is None or compared == 0:
    return None
  return True


def _format_attributes(attributes):
  return ', '.join(
      '%s=%s' % (key, value) for key, value in attributes.items()
  )


def _yes_no(value):
  return 'yes' if value else 'no'


def audit_echo(
    cmd_input,
    index_file=None,
    template_dir=None,
    attributes=None,
    field_map=None,
    templates=None,
    echo_source=None,
):
  """Audits an echo string and returns an AuditReport.

  This is the programmatic offline-verification entry point.
  """
  auditor = OfflineAudit(
      echo=cmd_input,
      index_file=index_file,
      template_dir=template_dir,
      attributes=attributes,
      field_map=field_map,
      templates=templates,
      echo_source=echo_source,
  )
  return auditor.run()


def audit_file(
    echo_path,
    index_file=None,
    template_dir=None,
    attributes=None,
    field_map=None,
    templates=None,
):
  """Reads an echo file and audits it."""
  with open(echo_path, 'r') as echo_file:
    text = echo_file.read()
  return audit_echo(
      text,
      index_file=index_file,
      template_dir=template_dir,
      attributes=attributes,
      field_map=field_map,
      templates=templates,
      echo_source=echo_path,
  )


def audit(*args, **kwargs):
  """Alias for audit_echo()."""
  return audit_echo(*args, **kwargs)


def verify(*args, **kwargs):
  """Alias for audit_echo()."""
  return audit_echo(*args, **kwargs)


verify_echo = audit_echo


def _load_json_file(path):
  with open(path, 'r') as handle:
    return json.load(handle)


def _parse_attributes(values, attribute_file=None):
  attributes = {}
  if attribute_file:
    loaded = _load_json_file(attribute_file)
    if not isinstance(loaded, dict):
      raise AuditError('Attribute file must contain a JSON object.')
    attributes.update(loaded)
  for value in values or ():
    if value.lstrip().startswith('{'):
      loaded = json.loads(value)
      if not isinstance(loaded, dict):
        raise AuditError('Attribute JSON must be an object.')
      attributes.update(loaded)
      continue
    if '=' not in value:
      raise AuditError(
          'Invalid attribute %r; expected NAME=VALUE.' % value
      )
    key, item = value.split('=', 1)
    attributes[key.strip()] = item.strip()
  return attributes


def _parse_field_map_args(values, field_map_file=None):
  field_map = {}
  if field_map_file:
    loaded = _load_json_file(field_map_file)
    if isinstance(loaded, dict) and 'fields' in loaded:
      loaded = loaded['fields']
    if isinstance(loaded, dict):
      for canonical, aliases in loaded.items():
        if isinstance(aliases, str):
          aliases = [aliases]
        field_map.setdefault(canonical, [])
        field_map[canonical].extend(aliases)
    else:
      entries = _split_templates(loaded)
      for value in entries:
        canonical, alias = _parse_field_pair(value)
        field_map.setdefault(canonical, []).append(alias)
  for value in values or ():
    canonical, alias = _parse_field_pair(value)
    field_map.setdefault(canonical, [])
    field_map[canonical].append(alias)
  return field_map


def _build_parser():
  parser_obj = argparse.ArgumentParser(
      prog='textfsm-audit',
      description=(
          'Offline cross-check of CLI echoes against index templates. '
          'Reports index ambiguity, template-dependent columns, and whether '
          'zero-row results are confirmed by an end marker.'
      ),
  )
  parser_obj.add_argument(
      'echo_file', nargs='?',
      help='Captured CLI echo file. Use - for stdin.',
  )
  parser_obj.add_argument(
      '-e', '--echo',
      help='Alternative path to the captured CLI echo file (use - for stdin).',
  )
  parser_obj.add_argument(
      '-i', '--index', dest='index_file',
      help='Index file name/path, relative to --template-dir.',
  )
  parser_obj.add_argument(
      '-t', '--template-dir', default='.',
      help='Directory containing the index and templates (default: .).',
  )
  parser_obj.add_argument(
      '--template', '--templates', action='append', default=[],
      help='Template name to audit. Repeat or use a comma/colon list.',
  )
  parser_obj.add_argument(
      '-a', '--attribute', '--attr', dest='attributes', action='append',
      default=[],
      help='Index attribute as NAME=VALUE or a JSON object. Repeatable.',
  )
  parser_obj.add_argument(
      '--attribute-file',
      help='JSON file with index attributes.',
  )
  parser_obj.add_argument(
      '--field-map', action='append', default=[],
      help='Field equivalence as CANONICAL=ALIAS (or CANONICAL:ALIAS). '
           'Repeatable.',
  )
  parser_obj.add_argument(
      '--field-map-file',
      help='JSON file with field equivalences.',
  )
  parser_obj.add_argument(
      '--json', action='store_true',
      help='Emit JSON instead of the human-readable report.',
  )
  parser_obj.add_argument(
      '--fail-on-ambiguous-index', action='store_true',
      help='Exit non-zero when more than one index row matches.',
  )
  parser_obj.add_argument(
      '--fail-on-unverified', action='store_true',
      help='Exit non-zero when a zero-row result is not confirmed by an end '
           'marker.',
  )
  parser_obj.add_argument(
      '--fail-on-incomplete', action='store_true',
      help='Exit non-zero when a template did not reach an end marker.',
  )
  parser_obj.add_argument(
      '-o', '--output', help='Write the report to this file instead of stdout.',
  )
  return parser_obj


def main(argv=None):
  """Command line entry point for offline verification."""
  arg_parser = _build_parser()
  args = arg_parser.parse_args(argv)

  echo_path = args.echo or args.echo_file
  if not echo_path:
    arg_parser.error('an echo file is required (positional or --echo)')
  if args.echo and args.echo_file:
    arg_parser.error('specify the echo file either positionally or via --echo')

  try:
    if echo_path == '-':
      import sys

      echo_text = sys.stdin.read()
      source = '<stdin>'
    else:
      with open(echo_path, 'r') as echo_file:
        echo_text = echo_file.read()
      source = echo_path

    attributes = _parse_attributes(args.attributes, args.attribute_file)
    field_map = _parse_field_map_args(args.field_map, args.field_map_file)
    report = audit_echo(
        echo_text,
        index_file=args.index_file,
        template_dir=args.template_dir,
        attributes=attributes,
        field_map=field_map,
        templates=args.template or None,
        echo_source=source,
    )
  except (AuditError, OSError, ValueError, json.JSONDecodeError) as exc:
    print('textfsm-audit: %s' % exc)
    return 2

  output = report.to_json() if args.json else report.format_text()
  if args.output:
    with open(args.output, 'w') as output_file:
      output_file.write(output)
      if not output.endswith('\n'):
        output_file.write('\n')
  else:
    print(output)

  if args.fail_on_ambiguous_index and report.ambiguous_index:
    return 1
  if args.fail_on_unverified and report.unverified_empty_templates:
    return 1
  if args.fail_on_incomplete and report.incomplete_templates:
    return 1
  return 0


if __name__ == '__main__':
  import sys

  sys.exit(main())
