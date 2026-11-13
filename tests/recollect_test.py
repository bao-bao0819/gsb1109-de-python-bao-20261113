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

"""Unittest for recollect module."""

import unittest
from textfsm import clitable
from textfsm import recollect


class UnitTestRecollectPlan(unittest.TestCase):
  """Tests the RecollectPlan class."""

  def setUp(self):
    super(UnitTestRecollectPlan, self).setUp()
    clitable.CliTable.INDEX = {}
    self.vendor = {'Vendor': 'VendorA'}

  def _Plan(self, *archived):
    plan = recollect.RecollectPlan('recollect_index', 'testdata')
    for table in archived:
      plan.Add(table)
    return plan.Build()

  def testDerivableFromStoredFields(self):
    """Stored fields covering the new template need no re-collection."""
    plan = self._Plan(
        recollect.ArchivedTable('dev1', 'sh ver', ['Col1', 'Col2', 'Col3'],
                                attributes=self.vendor))
    self.assertEqual(1, len(plan.derivable))
    self.assertEqual([], plan.recollect)
    self.assertEqual([], plan.reparsed)

  def testReparseFromKeptRaw(self):
    """Kept raw output is re-parsed locally with the new template."""
    plan = self._Plan(
        recollect.ArchivedTable('dev2', 'sh int', ['Col1', 'Col2'],
                                attributes=self.vendor,
                                raw='a b c\nd e f\n'))
    self.assertEqual([], plan.recollect)
    self.assertEqual(1, len(plan.reparsed))
    archived, table = plan.reparsed[0]
    self.assertEqual('dev2', archived.device)
    self.assertEqual(table.table,
                     'Col1, Col2, Col3\na, b, c\nd, e, f\n')

  def testRecollectWhenNothingToProveWith(self):
    """Missing fields and no raw output lands on the re-collection list."""
    plan = self._Plan(
        recollect.ArchivedTable('dev3', 'sh int', ['Col1', 'Col2'],
                                attributes=self.vendor))
    self.assertEqual([], plan.derivable)
    self.assertEqual([], plan.reparsed)
    self.assertEqual(1, len(plan.recollect))
    entry = plan.recollect[0]
    self.assertEqual('dev3', entry.device)
    self.assertEqual('sh int', entry.command)
    self.assertEqual(['Col3'], entry.missing)

  def testRecollectDedupByDeviceAndCommand(self):
    """Tables from one command merge into a single re-collection entry."""
    plan = self._Plan(
        recollect.ArchivedTable('dev3', 'sh int', ['Col1', 'Col2'],
                                attributes=self.vendor),
        recollect.ArchivedTable('dev3', 'sh int', ['Col2', 'Col3'],
                                attributes=self.vendor),
        recollect.ArchivedTable('dev4', 'sh int', ['Col1', 'Col2'],
                                attributes=self.vendor))
    self.assertEqual(2, len(plan.recollect))
    self.assertEqual(['Col1', 'Col3'], plan.recollect[0].missing)
    self.assertEqual('dev3', plan.recollect[0].device)
    self.assertEqual(['Col3'], plan.recollect[1].missing)
    self.assertEqual('dev4', plan.recollect[1].device)

  def testObsoleteCommandNotRecollected(self):
    """Commands dropped from the new index need no re-collection."""
    plan = self._Plan(
        recollect.ArchivedTable('dev5', 'sh mac', ['Col1'],
                                attributes=self.vendor))
    self.assertEqual(1, len(plan.obsolete))
    self.assertEqual([], plan.recollect)

  def testUnreadableTemplateIsRecollected(self):
    """Unprovable required fields are re-collected with unknown fields."""
    plan = self._Plan(
        recollect.ArchivedTable('dev6', 'sh bog', ['Col1'],
                                attributes=self.vendor))
    self.assertEqual(1, len(plan.recollect))
    self.assertIsNone(plan.recollect[0].missing)
    # Unknown fields dominate any known ones for the same command.
    plan = self._Plan(
        recollect.ArchivedTable('dev6', 'sh bog', ['Col1'],
                                attributes=self.vendor),
        recollect.ArchivedTable('dev6', 'sh bog', ['Col1'],
                                attributes=self.vendor))
    self.assertEqual(1, len(plan.recollect))
    self.assertIsNone(plan.recollect[0].missing)

  def testRecollectTable(self):
    """The re-collection list renders as a table."""
    plan = self._Plan(
        recollect.ArchivedTable('dev3', 'sh int', ['Col1', 'Col2'],
                                attributes=self.vendor),
        recollect.ArchivedTable('dev6', 'sh bog', ['Col1'],
                                attributes=self.vendor))
    self.assertEqual(plan.RecollectTable().table,
                     'Device, Command, MissingFields\n'
                     'dev3, sh int, Col3\n'
                     'dev6, sh bog, *\n')


if __name__ == '__main__':
  unittest.main()
