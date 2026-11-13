#!/usr/bin/python
#
# Copyright 2026 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Unittest for the offline TextFSM audit."""

import contextlib
import io
import json
import os
import tempfile
import unittest

from textfsm import audit
from textfsm import clitable


class UnitTestIndexAudit(unittest.TestCase):
  """Tests exhaustive index matching."""

  def testGetRowMatchesReturnsEveryMatch(self):
    table = clitable.IndexTable(
        file_path=os.path.join('testdata', 'audit_index')
    )
    self.assertEqual(
        table.GetRowMatches({'Model': 'WS-C4948'}), [1, 2]
    )

  def testGetRowMatchKeepsFirstMatchBehaviour(self):
    table = clitable.IndexTable(
        file_path=os.path.join('testdata', 'audit_index')
    )
    self.assertEqual(table.GetRowMatch({'Model': 'WS-C4948'}), 1)


class UnitTestOfflineAudit(unittest.TestCase):
  """Tests the offline verification report."""

  def setUp(self):
    super(UnitTestOfflineAudit, self).setUp()
    with open(os.path.join('testdata', 'audit_echo_version')) as echo_file:
      self.version_echo = echo_file.read()

  def _version_report(self, **kwargs):
    return audit.audit_echo(
        self.version_echo,
        index_file='audit_index',
        template_dir='testdata',
        attributes={'Model': 'WS-C4948'},
        **kwargs,
    )

  def testAmbiguousIndexIsReported(self):
    report = self._version_report()
    self.assertTrue(report.ambiguous_index)
    self.assertEqual([row.row for row in report.candidates], [1, 2])
    self.assertEqual(report.selected_row, 1)
    self.assertEqual(
        [candidate.selected for candidate in report.candidates],
        [True, False],
    )
    self.assertEqual(
        report.to_dict()['index']['matched_rows'], [1, 2]
    )
    self.assertEqual(report.to_dict()['index']['selection_rule'], 'first-match')
    self.assertIn('Index matched 2 rows', report.warnings[0])

  def testColumnsAreSplitByTemplate(self):
    report = self._version_report()
    determined = [column.canonical for column in report.determined_columns]
    dependent = [
        column.canonical for column in report.template_dependent_columns
    ]
    self.assertEqual(determined, ['Memory', 'Version'])
    self.assertEqual(dependent, ['Model', 'Chassis'])
    model = [
        column
        for column in report.template_dependent_columns
        if column.canonical == 'Model'
    ][0]
    self.assertEqual(
        model.reason, 'missing-from-template:audit_version_b'
    )

  def testFieldMapConfirmsRenamedField(self):
    report = self._version_report(field_map={'Model': 'Chassis'})
    determined = [column.canonical for column in report.determined_columns]
    dependent = [
        column.canonical for column in report.template_dependent_columns
    ]
    self.assertEqual(determined, ['Model', 'Memory', 'Version'])
    self.assertEqual(dependent, [])
    model = [
        column
        for column in report.determined_columns
        if column.canonical == 'Model'
    ][0]
    self.assertEqual(model.aliases, {'audit_version_b': 'Chassis'})
    self.assertTrue(model.value_consistent)
    by_name = {template.name: template for template in report.templates}
    self.assertEqual(
        by_name['audit_version_a'].records[0]['Model'], 'WS-C4948-10GE')
    self.assertEqual(
        by_name['audit_version_b'].records[0]['Chassis'], 'WS-C4948-10GE')

  def testFieldMapAcceptedAsPairList(self):
    report = self._version_report(field_map=[('Model', 'Chassis')])
    self.assertEqual(
        [column.canonical for column in report.determined_columns],
        ['Model', 'Memory', 'Version'],
    )

  def testZeroRowsWithoutMarkerIsUnverified(self):
    report = audit.audit_echo(
        'nothing here\n',
        template_dir='testdata',
        templates='audit_ports_marker',
    )
    template = report.templates[0]
    self.assertEqual(template.row_count, 0)
    self.assertFalse(template.terminated)
    self.assertEqual(
        template.empty_status, 'unverified-missing-end-marker'
    )
    self.assertFalse(template.empty_confirmed)
    self.assertEqual(
        report.unverified_empty_templates, ['audit_ports_marker']
    )
    self.assertIn('absence is NOT confirmed', report.warnings[0])

  def testZeroRowsWithMarkerIsConfirmed(self):
    report = audit.audit_echo(
        'nothing here\n-- More --\n',
        template_dir='testdata',
        templates='audit_ports_marker',
    )
    template = report.templates[0]
    self.assertEqual(template.empty_status, 'confirmed-empty')
    self.assertTrue(template.empty_confirmed)
    self.assertTrue(template.terminated)
    self.assertEqual(template.termination_state, 'End')
    self.assertEqual(template.end_marker, '-- More --')
    self.assertEqual(
        report.confirmed_empty_templates, ['audit_ports_marker']
    )

  def testTemplateWithoutEndMarkerIsUnverified(self):
    report = audit.audit_echo(
        'nothing here\n',
        template_dir='testdata',
        templates='audit_ports_no_marker',
    )
    template = report.templates[0]
    self.assertEqual(
        template.empty_status, 'unverified-template-has-no-end-marker'
    )

  def testNonMatchingIndexIsReported(self):
    report = audit.audit_echo(
        self.version_echo,
        index_file='audit_index',
        template_dir='testdata',
        attributes={'Model': 'NEVER-MATCH'},
    )
    self.assertTrue(report.no_index_match)
    self.assertFalse(report.candidates)
    self.assertEqual(report.to_dict()['index']['matched_rows'], [])
    self.assertIn('No index row matched', report.warnings[0])

  def testExplicitTemplatesDoNotRequireIndex(self):
    report = audit.audit_echo(
        self.version_echo,
        template_dir='testdata',
        templates='audit_version_a:audit_version_b',
    )
    self.assertEqual(len(report.templates), 2)
    self.assertIsNone(report.selected_row)
    self.assertFalse(report.ambiguous_index)
    self.assertEqual(
        report.to_dict()['explicit_templates'],
        ['audit_version_a', 'audit_version_b'],
    )

  def testMissingTemplateIsReported(self):
    report = audit.audit_echo(
        self.version_echo,
        template_dir='testdata',
        templates='audit_version_a:audit_missing',
    )
    missing = [
        template
        for template in report.templates
        if template.name == 'audit_missing'
    ][0]
    self.assertEqual(missing.status, 'missing')
    self.assertEqual(missing.empty_status, 'template-missing')
    self.assertTrue(
        any('audit_missing is missing' in warning
            for warning in report.warnings)
    )

  def testReportSerializesToJson(self):
    report = self._version_report()
    parsed = json.loads(report.to_json())
    self.assertEqual(parsed['index']['matched_rows'], [1, 2])
    self.assertEqual(
        [column['column'] for column in parsed['columns']['determined']],
        ['Memory', 'Version'],
    )
    self.assertEqual(
        [column['column']
         for column in parsed['columns']['template_dependent']],
        ['Model', 'Chassis'],
    )
    self.assertEqual(parsed['stable_columns'], parsed['determined_columns'])


class UnitTestParserTermination(unittest.TestCase):
  """Tests the termination evidence exposed by TextFSM."""

  def testExplicitEndIsRecorded(self):
    report = audit.audit_echo(
        'nothing here\n-- More --\n',
        template_dir='testdata',
        templates='audit_ports_marker',
    )
    self.assertEqual(report.templates[0].termination_state, 'End')
    self.assertEqual(
        report.templates[0].end_marker_rule_line, 6
    )

  def testDeclaredButUnreachedEofIsNotTermination(self):
    report = audit.audit_echo(
        'nothing here\n',
        template_dir='testdata',
        templates='audit_ports_empty',
    )
    template = report.templates[0]
    self.assertEqual(template.declared_end_states, ('EOF',))
    self.assertFalse(template.terminated)
    self.assertEqual(
        template.empty_status, 'unverified-missing-end-marker'
    )


class UnitTestAuditCli(unittest.TestCase):
  """Tests the command line entry point."""

  def _run(self, argv):
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
      code = audit.main(argv)
    return code, stdout.getvalue()

  def testCliJsonOutput(self):
    code, output = self._run([
        os.path.join('testdata', 'audit_echo_version'),
        '--index', 'audit_index',
        '--template-dir', 'testdata',
        '--attribute', 'Model=WS-C4948',
        '--field-map', 'Model=Chassis',
        '--json',
        '--fail-on-ambiguous-index',
    ])
    parsed = json.loads(output)
    self.assertEqual(code, 1)
    self.assertTrue(parsed['summary']['ambiguous_index'])
    self.assertEqual(
        [column['column']
         for column in parsed['columns']['determined']],
        ['Model', 'Memory', 'Version'],
    )

  def testCliFailOnUnverified(self):
    code, output = self._run([
        os.path.join('testdata', 'audit_echo_version'),
        '--template-dir', 'testdata',
        '--template', 'audit_ports_marker',
        '--fail-on-unverified',
    ])
    self.assertEqual(code, 1)
    self.assertIn('unverified-missing-end-marker', output)

  def testCliWritesOutputFile(self):
    with tempfile.TemporaryDirectory() as temp_dir:
      output_path = os.path.join(temp_dir, 'report.json')
      code, output = self._run([
          os.path.join('testdata', 'audit_echo_version'),
          '--index', 'audit_index',
          '--template-dir', 'testdata',
          '--attribute', 'Model=WS-C4948',
          '--json',
          '--output', output_path,
      ])
      self.assertEqual(code, 0)
      self.assertEqual(output, '')
      with open(output_path) as report_file:
        self.assertEqual(json.load(report_file)['index']['matched_rows'],
                         [1, 2])

  def testCliRequiresEcho(self):
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
      with self.assertRaises(SystemExit) as raised:
        audit.main([])
    self.assertEqual(raised.exception.code, 2)


class UnitTestFieldMap(unittest.TestCase):
  """Tests field-map normalization."""

  def testTransitiveAliases(self):
    field_map = audit.normalize_field_map({
        'Model': ['Chassis', 'ChassisType'],
        'ChassisType': 'Platform',
    })
    self.assertEqual(field_map.resolve('Chassis'), 'Model')
    self.assertEqual(field_map.resolve('ChassisType'), 'Model')
    self.assertEqual(field_map.resolve('Platform'), 'Model')

  def testUnmappedFieldIsUnchanged(self):
    field_map = audit.normalize_field_map({'Model': 'Chassis'})
    self.assertEqual(field_map.resolve('Memory'), 'Memory')

  def testFieldMapNormalizedCaseInsensitively(self):
    field_map = audit.normalize_field_map(['model=chassis'])
    self.assertEqual(field_map.resolve('MODEL'), 'model')
    self.assertEqual(field_map.resolve('Chassis'), 'model')
    self.assertEqual(field_map.resolve('CHASSIS'), 'model')


if __name__ == '__main__':
  unittest.main()
