#!/usr/bin/python
#
# Copyright 2012 Google Inc. All Rights Reserved.
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

"""Unittest for clitable script."""

import copy
import io
import os
import re
import unittest
from textfsm import clitable


class UnitTestIndexTable(unittest.TestCase):
  """Tests the IndexTable class."""

  def testParseIndex(self):
    """Test reading and index and parsing to index and compiled tables."""
    file_path = os.path.join('testdata', 'parseindex_index')
    indx = clitable.IndexTable(file_path=file_path)
    # Compare number of entries found in the index table.
    self.assertEqual(indx.index.size, 3)
    self.assertEqual(indx.index[2]['Template'], 'clitable_templateC')
    self.assertEqual(indx.index[3]['Template'], 'clitable_templateD')
    self.assertEqual(indx.index[1]['Command'], 'sh[[ow]] ve[[rsion]]')
    self.assertEqual(indx.index[1]['Hostname'], '.*')

    self.assertEqual(indx.compiled.size, 3)
    for col in ('Command', 'Vendor', 'Template', 'Hostname'):
      self.assertIsInstance(indx.compiled[1][col], re.Pattern)

    self.assertTrue(indx.compiled[1]['Hostname'].match('random string'))

    def _PreParse(key, value):
      if key == 'Template':
        return value.upper()
      return value

    def _PreCompile(key, value):
      if key in ('Template', 'Command'):
        return None
      return value

    self.assertEqual(indx.compiled.size, 3)
    indx = clitable.IndexTable(_PreParse, _PreCompile, file_path)
    self.assertEqual(indx.index[2]['Template'], 'CLITABLE_TEMPLATEC')
    self.assertEqual(indx.index[1]['Command'], 'sh[[ow]] ve[[rsion]]')
    self.assertIsInstance(indx.compiled[1]['Hostname'], re.Pattern)
    self.assertFalse(indx.compiled[1]['Command'])

  def testGetRowMatch(self):
    """Tests retreiving rows from table."""
    file_path = os.path.join('testdata', 'parseindex_index')
    indx = clitable.IndexTable(file_path=file_path)
    self.assertEqual(1, indx.GetRowMatch({'Hostname': 'abc'}))
    self.assertEqual(2, indx.GetRowMatch({'Hostname': 'abc',
                                          'Vendor': 'VendorB'}))

  def testCopy(self):
    """Tests copy of IndexTable object."""
    file_path = os.path.join('testdata', 'parseindex_index')
    indx = clitable.IndexTable(file_path=file_path)
    copy.deepcopy(indx)


class UnitTestCliTable(unittest.TestCase):
  """Tests the CliTable class."""

  def setUp(self):
    super(UnitTestCliTable, self).setUp()
    clitable.CliTable.INDEX = {}
    self.clitable = clitable.CliTable('default_index', 'testdata')
    self.input_data = ('a b c\n'
                       'd e f\n')
    self.template = ('Value Key Col1 (.)\n'
                     'Value Col2 (.)\n'
                     'Value Col3 (.)\n'
                     '\n'
                     'Start\n'
                     '  ^${Col1} ${Col2} ${Col3} -> Record\n'
                     '\n')
    self.template_file = io.StringIO(self.template)

  def testCompletion(self):
    """Tests '[[]]' syntax replacement."""
    indx = clitable.CliTable()
    self.assertEqual('abc', re.sub(r'(\[\[.+?\]\])', indx._Completion, 'abc'))
    self.assertEqual('a(b(c)?)?',
                     re.sub(r'(\[\[.+?\]\])', indx._Completion, 'a[[bc]]'))
    self.assertEqual('a(b(c)?)? de(f)?',
                     re.sub(r'(\[\[.+?\]\])', indx._Completion,
                            'a[[bc]] de[[f]]'))

  def testRepeatRead(self):
    """Tests that index file is read only once at the class level."""
    new_clitable = clitable.CliTable('default_index', 'testdata')
    self.assertEqual(self.clitable.index, new_clitable.index)

  def testCliCompile(self):
    """Tests PreParse and PreCompile."""

    self.assertEqual('sh(o(w)?)? ve(r(s(i(o(n)?)?)?)?)?',
                     self.clitable.index.index[1]['Command'])
    self.assertIsNone(self.clitable.index.compiled[1]['Template'])
    self.assertTrue(
        self.clitable.index.compiled[1]['Command'].match('sho vers'))

  def testParseCmdItem(self):
    """Tests parsing data with a single specific template."""
    t = self.clitable._ParseCmdItem(self.input_data,
                                    template_file=self.template_file)
    self.assertEqual(t.table, 'Col1, Col2, Col3\na, b, c\nd, e, f\n')

  def testParseCmd(self):
    """Tests parsing data with a mocked template."""
    # Stub out the conversion of filename to file handle.
    self.clitable._TemplateNamesToFiles = lambda t: [self.template_file]
    self.clitable.ParseCmd(self.input_data, attributes={'Command': 'sh vers'})
    self.assertEqual(
        self.clitable.table, 'Col1, Col2, Col3\na, b, c\nd, e, f\n')

  def testParseWithTemplate(self):
    """Tests parsing with an explicitly declared the template."""
    self.clitable.ParseCmd(self.input_data,
                           attributes={'Command': 'sh vers'},
                           templates='clitable_templateB')
    self.assertEqual(
        self.clitable.table, 'Col1, Col4\na, b\nd, e\n')

  def testParseCmdFromIndex(self):
    """Tests parsing with a template found in the index."""
    self.clitable.ParseCmd(self.input_data,
                           attributes={'Command': 'sh vers',
                                       'Vendor': 'VendorB'})
    self.assertEqual(
        self.clitable.table, 'Col1, Col2, Col3\na, b, c\n')
    self.clitable.ParseCmd(self.input_data,
                           attributes={'Command': 'sh int',
                                       'Vendor': 'VendorA'})
    self.assertEqual(
        self.clitable.table, 'Col1, Col2, Col3\nd, e, f\n')

    self.assertRaises(clitable.CliTableError, self.clitable.ParseCmd,
                      self.input_data,
                      attributes={'Command': 'show vers',
                                  'Vendor': 'bogus'})
    self.assertRaises(clitable.CliTableError, self.clitable.ParseCmd,
                      self.input_data,
                      attributes={'Command': 'unknown command',
                                  'Vendor': 'VendorA'})

  def testParseWithMultiTemplates(self):
    """Tests that multiple matching templates extend the table."""
    self.clitable.ParseCmd(self.input_data,
                           attributes={'Command': 'sh ver',
                                       'Vendor': 'VendorA'})
    self.assertEqual(
        self.clitable.table,
        'Col1, Col2, Col3, Col4\na, b, c, b\nd, e, f, e\n')
    self.clitable.ParseCmd(self.input_data,
                           attributes={'Command': 'sh vers'},
                           templates='clitable_templateB:clitable_templateA')
    self.assertEqual(
        self.clitable.table,
        'Col1, Col4, Col2, Col3\na, b, b, c\nd, e, e, f\n')
    self.assertRaises(IOError, self.clitable.ParseCmd,
                      self.input_data,
                      attributes={'Command': 'sh vers'},
                      templates='clitable_templateB:clitable_bogus')

  def testRequireCols(self):
    """Tests that CliTable expects a 'Template' row to be present."""
    self.assertRaises(clitable.CliTableError, clitable.CliTable,
                      'nondefault_index', 'testdata')

  def testSuperKey(self):
    """Tests that superkey is derived from the template and is extensible."""
    # Stub out the conversion of filename to file handle.
    self.clitable._TemplateNamesToFiles = lambda t: [self.template_file]
    self.clitable.ParseCmd(self.input_data, attributes={'Command': 'sh ver'})
    self.assertEqual(self.clitable.superkey, ['Col1'])
    self.assertEqual(
        self.clitable.LabelValueTable(),
        '# LABEL Col1\n'
        'a.Col2 b\n'
        'a.Col3 c\n'
        'd.Col2 e\n'
        'd.Col3 f\n')

    self.clitable.AddKeys(['Col2'])
    self.assertEqual(
        self.clitable.LabelValueTable(),
        '# LABEL Col1.Col2\n'
        'a.b.Col3 c\n'
        'd.e.Col3 f\n')

  def testAddKey(self):
    """Tests that new keys are not duplicated and non-existant columns."""
    self.assertEqual(self.clitable.superkey, [])
    # Stub out the conversion of filename to file handle.
    self.clitable._TemplateNamesToFiles = lambda t: [self.template_file]
    self.clitable.ParseCmd(self.input_data, attributes={'Command': 'sh ver'})
    self.assertEqual(self.clitable.superkey, ['Col1'])
    self.clitable.AddKeys(['Col1', 'Col2', 'Col3'])
    self.assertEqual(self.clitable.superkey, ['Col1', 'Col2', 'Col3'])
    self.assertRaises(KeyError, self.clitable.AddKeys, ['Bogus'])

  def testKeyValue(self):
    """Tests retrieving row value that corresponds to the key."""
    # Stub out the conversion of filename to file handle.
    self.clitable._TemplateNamesToFiles = lambda t: [self.template_file]
    self.clitable.ParseCmd(self.input_data, attributes={'Command': 'sh ver'})
    self.assertEqual(self.clitable.KeyValue(), ['a'])
    self.clitable.row_index = 2
    self.assertEqual(self.clitable.KeyValue(), ['d'])
    self.clitable.row_index = 1
    self.clitable.AddKeys(['Col3'])
    self.assertEqual(self.clitable.KeyValue(), ['a', 'c'])
    # With no key it falls back to row number.
    self.clitable._keys = set()
    for rownum, row in enumerate(self.clitable, start=1):
      self.assertEqual(row.table.KeyValue(), ['%s' % rownum])

  def testTableSort(self):
    """Tests sorting of table based on superkey."""
    self.clitable._TemplateNamesToFiles = lambda t: [self.template_file]
    input_data2 = ('a e c\n'
                   'd b f\n')
    self.clitable.ParseCmd(self.input_data + input_data2,
                           attributes={'Command': 'sh ver'})
    self.assertEqual(
        self.clitable.table,
        'Col1, Col2, Col3\na, b, c\nd, e, f\na, e, c\nd, b, f\n')
    self.clitable.sort()
    # Key was non-unique, columns outside of the key do not count.
    self.assertEqual(
        self.clitable.table,
        'Col1, Col2, Col3\na, b, c\na, e, c\nd, e, f\nd, b, f\n')

    # Create a new table with no explicit key.
    self.template = ('Value Col1 (.)\n'
                     'Value Col2 (.)\n'
                     'Value Col3 (.)\n'
                     '\n'
                     'Start\n'
                     '  ^${Col1} ${Col2} ${Col3} -> Record\n'
                     '\n')
    self.template_file = io.StringIO(self.template)
    self.clitable._TemplateNamesToFiles = lambda t: [self.template_file]
    self.clitable.ParseCmd(self.input_data + input_data2,
                           attributes={'Command': 'sh ver'})
    # Add a manual key.
    self.clitable.AddKeys(['Col2'])
    self.clitable.sort()
    self.assertEqual(
        self.clitable.table,
        'Col1, Col2, Col3\na, b, c\nd, b, f\nd, e, f\na, e, c\n')
    # Clear the keys.
    self.clitable._keys = set()
    # With no key, sort based on whole row.
    self.clitable.sort()
    self.assertEqual(
        self.clitable.table,
        'Col1, Col2, Col3\na, b, c\na, e, c\nd, b, f\nd, e, f\n')

  def testCopy(self):
    """Tests copying of clitable object."""
    copy.deepcopy(self.clitable)


class UnitTestJoinInterfaceTraffic(unittest.TestCase):
  """Tests evidence-based interface inventory/traffic joins."""

  def setUp(self):
    super(UnitTestJoinInterfaceTraffic, self).setUp()
    self.inventory = (
        'Interface, Serial, InventoryTime\n'
        '0/1, SN-A, 2026-11-14 10:00:00\n'
        '0/2, SN-B, 2026-11-14 10:00:00\n'
    )

  def _Join(self, traffic, inventory=None, hot_swaps=None, **kwargs):
    return clitable.JoinInterfaceTraffic(
        inventory or self.inventory, traffic, hot_swaps=hot_swaps, **kwargs
    )

  def testSingleRowIsClaimedWhenWindowHasOneOwner(self):
    result = self._Join(
        'Interface, CallStartWindow, CallEndWindow, Octets\n'
        '0/1, 2026-11-14 10:05:00, 2026-11-14 10:06:00, 100\n'
    )
    self.assertEqual(1, result.claimed_count)
    self.assertEqual(1, result.table.size)
    attribution = result.rows[0]
    self.assertEqual(clitable.CLAIMED, attribution.status)
    self.assertEqual('SN-A', attribution.serial)
    self.assertIn('inventory row 1', attribution.evidence)

  def testOneRowCanRemainUnclaimed(self):
    result = self._Join(
        'Interface, CallStartWindow, CallEndWindow, Octets\n'
        '0/1, 2026-11-14 09:00:00, 2026-11-14 09:01:00, 100\n'
    )
    self.assertEqual(0, result.claimed_count)
    self.assertEqual(1, result.unclaimed_count)
    self.assertFalse(result.complete)
    self.assertEqual(0, result.table.size)
    self.assertEqual('unclaimed', result.rows[0].status)

  def testUnknownTrafficSerialDoesNotUseTimeToHideConflict(self):
    """Traffic serial absent from inventory must not be guessed from time."""
    result = self._Join(
        'Interface, CallStartWindow, CallEndWindow, Octets, Serial\n'
        '0/1, 2026-11-14 10:04:00, 2026-11-14 10:05:00, 100, SN-Z\n'
    )
    self.assertEqual(0, result.claimed_count)
    self.assertEqual(1, result.unclaimed_count)
    self.assertEqual('unclaimed', result.rows[0].status)

  def testUnclaimedRowsCanBeKeptAsDiagnostics(self):
    result = self._Join(
        'Interface, CallStartWindow, CallEndWindow, Octets\n'
        '0/1, 2026-11-14 09:00:00, 2026-11-14 09:01:00, 100\n',
        include_unclaimed=True,
    )
    self.assertEqual(1, result.unclaimed_count)
    self.assertEqual(1, result.table.size)
    self.assertEqual('unclaimed', result.table[1]['JoinStatus'])
    self.assertEqual('', result.table[1]['JoinSerial'])

  def testExplicitTrafficSerialColumn(self):
    result = self._Join(
        'Interface, CallStartWindow, CallEndWindow, Board ID\n'
        '0/1, 2026-11-14 10:04:00, 2026-11-14 10:05:00, SN-A\n',
        traffic_serial_column='Board ID',
    )
    self.assertEqual(1, result.claimed_count)
    self.assertEqual('SN-A', result.table[1]['JoinTrafficSerial'])

  def testOverlappingOwnersProduceTwoInterpretations(self):
    inventory = (
        'Interface, Serial, InventoryTime\n'
        '0/1, SN-A, 2026-11-14 10:00:00\n'
        '0/1, SN-B, 2026-11-14 10:03:00\n'
    )
    result = self._Join(
        'Interface, CallStartWindow, CallEndWindow, Octets\n'
        '0/1, 2026-11-14 10:01:00, 2026-11-14 10:05:00, 100\n',
        inventory=inventory,
    )
    self.assertEqual(1, result.ambiguous_count)
    self.assertEqual(0, result.claimed_count)
    self.assertEqual(2, result.table.size)
    self.assertEqual(
        ('SN-A', 'SN-B'), tuple(c.serial for c in result.rows[0].candidates)
    )

  def testTrafficSerialAndTimeConflictKeepsBoth(self):
    inventory = (
        'Interface, Serial, InventoryTime\n'
        '0/1, SN-A, 2026-11-14 10:00:00\n'
        '0/1, SN-B, 2026-11-14 10:03:00\n'
    )
    result = self._Join(
        'Interface, CallStartWindow, CallEndWindow, Octets, Serial\n'
        '0/1, 2026-11-14 10:04:00, 2026-11-14 10:05:00, 100, SN-A\n',
        inventory=inventory,
    )
    self.assertEqual(1, result.ambiguous_count)
    self.assertEqual(2, len(result.rows[0].candidates))
    self.assertEqual(
        ('SN-A', 'SN-B'), tuple(c.serial for c in result.rows[0].candidates)
    )

  def testInsertEventWithoutRemoveLeavesBothOwners(self):
    inventory = (
        'Interface, Serial, InventoryTime\n'
        '0/1, SN-A, 2026-11-14 10:00:00\n'
    )
    hot_swaps = (
        'Interface, EventTime, Action, Serial\n'
        '0/1, 2026-11-14 10:03:00, insert, SN-B\n'
    )
    result = self._Join(
        'Interface, CallStartWindow, CallEndWindow, Octets\n'
        '0/1, 2026-11-14 10:04:00, 2026-11-14 10:05:00, 100\n',
        inventory=inventory,
        hot_swaps=hot_swaps,
    )
    self.assertEqual(1, result.ambiguous_count)
    self.assertEqual(
        ('SN-A', 'SN-B'), tuple(c.serial for c in result.rows[0].candidates)
    )

  def testReplaceEventResolvesReusedNumber(self):
    inventory = (
        'Interface, Serial, InventoryTime\n'
        '0/1, SN-A, 2026-11-14 10:00:00\n'
    )
    hot_swaps = (
        'Interface, EventTime, Action, OldSerial, NewSerial\n'
        '0/1, 2026-11-14 10:03:00, replace, SN-A, SN-B\n'
    )
    result = self._Join(
        'Interface, CallStartWindow, CallEndWindow, Octets\n'
        '0/1, 2026-11-14 10:04:00, 2026-11-14 10:05:00, 100\n',
        inventory=inventory,
        hot_swaps=hot_swaps,
    )
    self.assertEqual(1, result.claimed_count)
    self.assertEqual('SN-B', result.rows[0].serial)
    self.assertIn('hot swap replace', result.rows[0].evidence[0])

  def testReplaceBeforeTrafficAndNoLaterInventory(self):
    inventory = (
        'Interface, Serial, InventoryTime\n'
        '0/1, SN-A, 2026-11-14 10:00:00\n'
    )
    hot_swaps = (
        'Interface, EventTime, Action, Serial\n'
        '0/1, 2026-11-14 10:03:00, replace, SN-B\n'
    )
    result = self._Join(
        'Interface, CallStartWindow, CallEndWindow, Octets\n'
        '0/1, 2026-11-14 10:04:00, 2026-11-14 10:05:00, 100\n',
        inventory=inventory,
        hot_swaps=hot_swaps,
    )
    self.assertEqual(1, result.claimed_count)
    self.assertEqual('SN-B', result.rows[0].serial)
    self.assertIn('hot swap replace', result.rows[0].evidence[0])

  def testRepeatedIdenticalSnapshotsAreOneOwner(self):
    inventory = (
        'Interface, Serial, InventoryTime\n'
        '0/1, SN-A, 2026-11-14 10:00:00\n'
        '0/1, SN-A, 2026-11-14 10:05:00\n'
    )
    result = self._Join(
        'Interface, CallStartWindow, CallEndWindow, Octets\n'
        '0/1, 2026-11-14 10:01:00, 2026-11-14 10:09:00, 100\n',
        inventory=inventory,
    )
    self.assertEqual(1, result.claimed_count)
    self.assertEqual(1, len(result.rows[0].candidates))

  def testSerialOutsideWindowIsNotClaimedFromTime(self):
    inventory = (
        'Interface, Serial, InventoryTime\n'
        '0/1, SN-A, 2026-11-14 10:00:00\n'
        '0/1, SN-B, 2026-11-14 10:03:00\n'
    )
    result = self._Join(
        'Interface, CallStartWindow, CallEndWindow, Octets, Serial\n'
        '0/1, 2026-11-14 10:04:00, 2026-11-14 10:05:00, 100, SN-A\n',
        inventory=inventory,
    )
    self.assertEqual(1, result.ambiguous_count)
    self.assertEqual(0, result.claimed_count)

  def testCollidingTrafficColumnsArePreserved(self):
    inventory = (
        'Interface, Serial, InventoryTime\n'
        '0/1, SN-A, 2026-11-14 10:00:00\n'
    )
    result = self._Join(
        'Interface, Serial, CallStartWindow, CallEndWindow\n'
        '0/1, SN-A, 2026-11-14 10:05:00, 2026-11-14 10:06:00\n',
        inventory=inventory,
    )
    self.assertIn('Serial', result.table.header)
    self.assertIn('Traffic.Serial', result.table.header)
    self.assertEqual(1, result.claimed_count)

  def testRemoveEventLeavesNumberUnclaimed(self):
    inventory = (
        'Interface, Serial, InventoryTime\n'
        '0/1, SN-A, 2026-11-14 10:00:00\n'
    )
    hot_swaps = (
        'Interface, EventTime, Action, Serial\n'
        '0/1, 2026-11-14 10:03:00, remove, SN-A\n'
    )
    result = self._Join(
        'Interface, CallStartWindow, CallEndWindow, Octets\n'
        '0/1, 2026-11-14 10:04:00, 2026-11-14 10:05:00, 100\n',
        inventory=inventory,
        hot_swaps=hot_swaps,
    )
    self.assertEqual(1, result.unclaimed_count)
    self.assertEqual(0, result.claimed_count)
    self.assertEqual(0, result.table.size)

  def testSummaryDoesNotHideUnresolvedRows(self):
    result = self._Join(
        'Interface, CallStartWindow, CallEndWindow, Octets\n'
        '0/1, 2026-11-14 10:05:00, 2026-11-14 10:06:00, 100\n'
        '0/1, 2026-11-14 09:00:00, 2026-11-14 09:01:00, 200\n'
    )
    self.assertEqual(1, result.claimed_count)
    self.assertEqual(1, result.unclaimed_count)
    self.assertEqual(2, result.total_count)
    summary = result.Summary()
    self.assertEqual(2, summary['traffic_rows'])
    self.assertEqual(1, summary['claimed'])
    self.assertEqual(1, summary['unclaimed'])
    self.assertFalse(summary['complete'])
    self.assertEqual(1, result.table.size)

  def testPartialWindowCoverageIsNotClaimed(self):
    inventory = (
        'Interface, Serial, InventoryTime\n'
        '0/1, SN-A, 2026-11-14 10:00:00\n'
        '0/1, SN-B, 2026-11-14 10:10:00\n'
    )
    result = self._Join(
        'Interface, CallStartWindow, CallEndWindow, Octets\n'
        '0/1, 2026-11-14 09:55:00, 2026-11-14 10:05:00, 100\n',
        inventory=inventory,
    )
    self.assertEqual(1, result.ambiguous_count)
    self.assertEqual(0, result.claimed_count)

  def testCliTableJoinTrafficUpdatesTable(self):
    clitable.CliTable.INDEX = {}
    table = clitable.CliTable()
    table.header = ('Interface', 'Serial', 'InventoryTime')
    table.Append(('0/1', 'SN-A', '2026-11-14 10:00:00'))
    result = table.JoinTraffic(
        'Interface, CallStartWindow, CallEndWindow, Octets\n'
        '0/1, 2026-11-14 10:05:00, 2026-11-14 10:06:00, 100\n'
    )
    self.assertEqual(1, result.claimed_count)
    self.assertIn('JoinStatus', table.header)
    self.assertEqual(1, table.size)
    self.assertEqual('claimed', table[1]['JoinStatus'])

  def testJoinTablesDoesNotMutateInputs(self):
    inventory = clitable.CliTable()
    inventory.header = ('Interface', 'Serial', 'InventoryTime')
    inventory.Append(('0/1', 'SN-A', '2026-11-14 10:00:00'))
    traffic = clitable.CliTable()
    traffic.header = ('Interface', 'CallStartWindow', 'CallEndWindow')
    traffic.Append(('0/1', '2026-11-14 10:05:00', '2026-11-14 10:06:00'))
    result = clitable.CliTable.JoinTables(inventory, traffic)
    self.assertEqual(1, result.claimed_count)
    self.assertEqual(1, inventory.size)
    self.assertEqual(1, traffic.size)

  def testExplicitColumnNames(self):
    inventory = (
        'Port Name, Board SN, Snapshot At\n'
        '0/1, SN-A, 2026-11-14 10:00:00\n'
    )
    result = self._Join(
        'Port Name, Window Start, Window End, Octets\n'
        '0/1, 2026-11-14 10:05:00, 2026-11-14 10:06:00, 100\n',
        inventory=inventory,
        interface_column='Port Name',
        serial_column='Board SN',
        inventory_time_column='Snapshot At',
    )
    self.assertEqual(1, result.claimed_count)

  def testEmptyTrafficReturnsEmptyResult(self):
    result = self._Join('')
    self.assertEqual(0, result.total_count)
    self.assertEqual(0, result.table.size)
    self.assertTrue(result.complete)

  def testInvalidInventoryLacksSerial(self):
    with self.assertRaises(clitable.JoinError):
      self._Join(
          'Interface, CallStartWindow, CallEndWindow\n'
          '0/1, 2026-11-14 10:05:00, 2026-11-14 10:06:00\n',
          inventory='Interface, InventoryTime\n0/1, 2026-11-14 10:00:00\n',
      )


if __name__ == '__main__':
  unittest.main()
