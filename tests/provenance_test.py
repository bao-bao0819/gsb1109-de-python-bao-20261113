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
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied. See the License for the specific language governing
# permissions and limitations under the License.

"""Unittest for TextFSM line and cell provenance."""

import io
import unittest

import textfsm
from textfsm import provenance


class UnitTestProvenance(unittest.TestCase):
  """Tests the line-level provenance ledger."""

  def Parse(self, template, data):
    fsm = textfsm.TextFSM(io.StringIO(template))
    result, trace = fsm.ParseTextWithTrace(data)
    return fsm, result, trace

  def testParseTextWithTraceMatchesParseText(self):
    template = (
        'Value boo (.*)\n\n'
        'Start\n'
        '  ^$boo -> Record\n'
    )
    data = 'one\ntwo'
    fsm = textfsm.TextFSM(io.StringIO(template))
    self.assertEqual(fsm.ParseText(data), [['one'], ['two']])

    fsm = textfsm.TextFSM(io.StringIO(template))
    result, trace = fsm.ParseTextWithTrace(data)
    self.assertEqual(result, [['one'], ['two']])
    self.assertIsInstance(trace, provenance.ParseTrace)
    self.assertEqual(trace, fsm.trace)

  def testUnmatchedLine(self):
    template = (
        'Value boo (A.)\n\n'
        'Start\n'
        '  ^$boo -> Record\n'
    )
    _, result, trace = self.Parse(template, 'junk\nA1')

    self.assertEqual(result, [['A1']])
    self.assertEqual(trace.where(1), provenance.LineDisposition.UNMATCHED)
    self.assertFalse(trace.line(1).matched)
    self.assertEqual(trace.line(1).control_actions, [])
    self.assertEqual(trace.line(1).data, [])
    self.assertEqual(trace.where(2), provenance.LineDisposition.DATA)

  def testControlOnlyLine(self):
    """A line used only for a state change is not counted as data."""
    template = (
        'Value a (A.)\n\n'
        'Start\n'
        '  ^-- -> State1\n'
        '  ^$a -> Record\n\n'
        'State1\n'
        '  ^-- -> Start\n'
    )
    _, result, trace = self.Parse(template, '--\nA1')

    self.assertEqual(result, [])
    line = trace.line(1)
    self.assertEqual(line.disposition, provenance.LineDisposition.CONTROL)
    self.assertEqual(
        [action.kind for action in line.control_actions], ['state']
    )
    self.assertEqual(line.control_actions[0].detail, {'state': 'State1'})
    self.assertEqual(line.data, [])
    self.assertEqual(line.discarded_assignments, [])

  def testDiscardedValueLine(self):
    """Values assigned then cleared are reported as discarded, not data."""
    template = (
        'Value Required boo (on.)\n'
        'Value Filldown,Required hoo (tw.)\n\n'
        'Start\n'
        '  ^$boo -> Next.Record\n'
        '  ^$hoo -> Next.Clear\n'
    )
    _, result, trace = self.Parse(template, 'one\ntwo\nonE\ntwO')

    self.assertEqual(result, [['onE', 'two']])
    self.assertEqual(trace.where(1), provenance.LineDisposition.DISCARDED)
    self.assertEqual(trace.where(2), provenance.LineDisposition.DATA)
    self.assertEqual(trace.where(3), provenance.LineDisposition.DATA)
    self.assertEqual(trace.where(4), provenance.LineDisposition.DISCARDED)
    self.assertEqual(
        [(a.value_name, a.value) for a in trace.line(1).discarded_assignments],
        [('boo', 'one')],
    )
    self.assertEqual(
        [action.kind for action in trace.line(4).control_actions], ['clear']
    )

  def testContinueUsesOneLineInSeveralRules(self):
    template = (
        'Value a (A.)\n'
        'Value b (A.)\n\n'
        'Start\n'
        '  ^$a -> Continue\n'
        '  ^$b -> Continue.Record\n'
    )
    _, result, trace = self.Parse(template, 'A1')

    self.assertEqual(result, [['A1', 'A1']])
    line = trace.line(1)
    self.assertEqual(len(line.applications), 2)
    self.assertEqual(
        [app.rule_line_number for app in line.applications], [5, 6]
    )
    self.assertEqual([app.line_op for app in line.applications], ['Continue',
                                                                  'Continue'])
    self.assertEqual([action.kind for action in line.control_actions],
                     ['continue', 'record', 'continue'])
    self.assertEqual([cell.column for cell in line.data], ['a', 'b'])

  def testContinueStateIsRecordedPerApplication(self):
    template = (
        'Value a (A.)\n'
        'Value b (B.)\n\n'
        'Start\n'
        '  ^A -> State1\n'
        '  ^$a -> Continue\n'
        '  ^$b -> Record\n\n'
        'State1\n'
        '  ^A -> Continue\n'
        '  ^$b -> Record\n'
    )
    _, result, trace = self.Parse(template, 'A1\nB1')

    self.assertEqual(result, [['', 'B1']])
    first = trace.line(1).applications
    self.assertEqual(first[0].state, 'Start')
    self.assertEqual(first[0].state_after, 'State1')
    self.assertTrue(first[0].changed_state)
    self.assertEqual(trace.where(2), provenance.LineDisposition.DATA)

  def testListCellAccumulatesSeveralLines(self):
    template = (
        'Value List boo (on.)\n'
        'Value hoo (tw.)\n\n'
        'Start\n'
        '  ^$boo\n'
        '  ^$hoo -> Next.Record\n\n'
        'EOF\n'
    )
    _, result, trace = self.Parse(template, 'one\ntwo\non0\ntw0')

    self.assertEqual(result, [[['one'], 'two'], [['on0'], 'tw0']])
    self.assertEqual(trace.cell_sources(0, 'boo'), [1])
    self.assertEqual(trace.cell_sources(1, 'boo'), [3])
    self.assertEqual(trace.cell(0, 'boo').value, ['one'])
    self.assertEqual(trace.cell(1, 'boo').value, ['on0'])

  def testListFilldownCellRetainsAllContributors(self):
    template = (
        'Value List,Filldown boo (on.)\n'
        'Value hoo (on.)\n\n'
        'Start\n'
        '  ^$boo -> Continue\n'
        '  ^$hoo -> Next.Record\n\n'
        'EOF\n'
    )
    _, result, trace = self.Parse(template, 'one\non0\non1')

    self.assertEqual(result[2], [['one', 'on0', 'on1'], 'on1'])
    cell = trace.cell(2, 'boo')
    self.assertEqual(cell.value, ['one', 'on0', 'on1'])
    self.assertEqual(cell.source_lines, [1, 2, 3])
    self.assertEqual(
        [(a.element_index, a.line_number) for a in cell.contributions],
        [(0, 1), (1, 2), (2, 3)],
    )
    self.assertEqual(trace.lines_for_cell(2, 'boo'), [1, 2, 3])

  def testFillupUpdatesCommittedCellProvenance(self):
    template = (
        'Value Required Col1 ([^-]+)\n'
        'Value Fillup Col2 ([^-]+)\n'
        'Value Fillup Col3 ([^-]+)\n\n'
        'Start\n'
        '  ^$Col1 -- -- -> Record\n'
        '  ^$Col1 $Col2 -- -> Record\n'
        '  ^$Col1 -- $Col3 -> Record\n'
        '  ^$Col1 $Col2 $Col3 -> Record\n'
    )
    _, result, trace = self.Parse(template, '\n1 -- B1\n2 A2 --\n3 -- B3\n')

    self.assertEqual(
        result, [['1', 'A2', 'B1'], ['2', 'A2', 'B3'], ['3', '', 'B3']]
    )
    # Row 0 was committed on line 2, but Col2 was filled up by line 3.
    self.assertEqual(trace.record(0).commit_line, 2)
    self.assertEqual(trace.cell_sources(0, 'Col2'), [3])
    self.assertEqual(trace.cell(0, 'Col2').value, 'A2')
    self.assertEqual(trace.cell_sources(0, 'Col3'), [2])
    self.assertEqual(trace.cell_sources(1, 'Col3'), [4])

  def testPhysicalLineShowsControlAndDataSeparately(self):
    template = (
        'Value Required boo (on.)\n'
        'Value Filldown,Required hoo (tw.)\n\n'
        'Start\n'
        '  ^$boo -> Next.Record\n'
        '  ^$hoo -> Next.Clear\n'
    )
    _, _, trace = self.Parse(template, 'one\ntwo\nonE\ntwO')

    line2 = trace.line(2)
    self.assertEqual(line2.disposition, provenance.LineDisposition.DATA)
    self.assertEqual([action.kind for action in line2.control_actions],
                     ['clear'])
    self.assertEqual([cell.column for cell in line2.data], ['hoo'])
    # The line's final contribution survives the later Clear on line 4.
    self.assertEqual(line2.data[0].record_index, 0)

    line4 = trace.line(4)
    self.assertEqual(line4.disposition, provenance.LineDisposition.DISCARDED)
    self.assertEqual([action.kind for action in line4.control_actions],
                     ['clear'])
    self.assertEqual(line4.data, [])

  def testRecordCommitLineIsDistinctFromContributionLines(self):
    template = (
        'Value a (A.)\n'
        'Value b (B.)\n\n'
        'Start\n'
        '  ^$a -> State1\n\n'
        'State1\n'
        '  ^$b -> Record\n'
    )
    _, result, trace = self.Parse(template, 'A1\nB1')

    self.assertEqual(result, [['A1', 'B1']])
    record = trace.record(0)
    self.assertEqual(record.commit_line, 2)
    self.assertEqual(record.source_lines, [1, 2])
    self.assertEqual(trace.cell_sources(0, 'a'), [1])
    self.assertEqual(trace.cell_sources(0, 'b'), [2])

  def testEofCommitHasNoPhysicalLine(self):
    template = (
        'Value boo (.*)\n\n'
        'Start\n'
        '  ^$boo -> Next\n'
    )
    _, result, trace = self.Parse(template, 'Matching text')

    self.assertEqual(result, [['Matching text']])
    record = trace.record(0)
    self.assertIsNone(record.commit_line)
    self.assertEqual(record.commit.reason, 'eof')
    self.assertTrue(record.commit.eof)
    self.assertEqual(trace.cell_sources(0, 'boo'), [1])

  def testExplicitEndMarksRemainingLinesUnparsed(self):
    template = (
        'Value boo (.*)\n\n'
        'Start\n'
        '  ^$boo -> End\n'
    )
    _, result, trace = self.Parse(template, 'a\nb\nc')

    self.assertEqual(result, [])
    self.assertEqual(len(trace), 3)
    self.assertEqual(trace.where(1), provenance.LineDisposition.DISCARDED)
    self.assertEqual(trace.where(2), provenance.LineDisposition.UNPARSED)
    self.assertEqual(trace.where(3), provenance.LineDisposition.UNPARSED)

  def testErrorRecordsLineError(self):
    template = (
        'Value boo (A.)\n\n'
        'Start\n'
        '  ^$boo -> Error "boom"\n'
    )
    fsm = textfsm.TextFSM(io.StringIO(template))
    with self.assertRaises(textfsm.TextFSMError):
      fsm.ParseText('A1')

    trace = fsm.trace
    self.assertTrue(trace.error)
    self.assertTrue(trace.line(1).error)
    self.assertEqual([action.kind for action in trace.line(1).control_actions],
                     ['error'])
    self.assertEqual(trace.line(1).control_actions[0].detail,
                     {'message': '"boom"'})
    self.assertEqual(trace.line(1).disposition,
                     provenance.LineDisposition.DISCARDED)

  def testReverseLookups(self):
    template = (
        'Value a (A.)\n'
        'Value b (B.)\n\n'
        'Start\n'
        '  ^$a\n'
        '  ^$b -> Record\n\n'
        'EOF\n'
    )
    _, _, trace = self.Parse(template, 'A1\nB1')

    self.assertEqual(trace.cell_sources(0, 0), [1])
    self.assertEqual(trace.cell_sources(0, 'a'), [1])
    self.assertEqual(trace.cell_sources(0, 'b'), [2])
    self.assertEqual(
        [(cell.record_index, cell.column) for cell in trace.cells_for_line(1)],
        [(0, 'a')],
    )
    self.assertEqual(
        [(cell.record_index, cell.column) for cell in trace.cells_for_line(2)],
        [(0, 'b')],
    )
    self.assertEqual([r.record_index for r in trace.records_for_line(2)], [0])
    self.assertEqual(trace.lines_for_record(0), [1, 2])

  def testFsmConvenienceLookups(self):
    template = (
        'Value a (A.)\n'
        'Value b (B.)\n\n'
        'Start\n'
        '  ^$a\n'
        '  ^$b -> Record\n\n'
        'EOF\n'
    )
    fsm, _, _ = self.Parse(template, 'A1\nB1')

    self.assertEqual(fsm.CellProvenance(0, 'a').source_lines, [1])
    self.assertEqual(fsm.LineProvenance(2).text, 'B1')

  def testLooksUpBeforeParseFail(self):
    template = 'Value boo (.*)\n\nStart\n  ^$boo\n'
    fsm = textfsm.TextFSM(io.StringIO(template))
    self.assertIsNone(fsm.trace)
    self.assertRaises(textfsm.TextFSMError, fsm.CellProvenance, 0, 'boo')
    self.assertRaises(textfsm.TextFSMError, fsm.LineProvenance, 1)

  def testBadLookups(self):
    template = (
        'Value boo (.*)\n\n'
        'Start\n'
        '  ^$boo -> Record\n'
    )
    _, _, trace = self.Parse(template, 'value-one')

    self.assertRaises(IndexError, trace.line, 2)
    self.assertRaises(IndexError, trace.record, 1)
    self.assertRaises(IndexError, trace.cell, 0, 'nope')
    self.assertRaises(IndexError, trace.cell, 0, 5)

  def testTraceIsSerialisable(self):
    template = (
        'Value List boo (on.)\n'
        'Value hoo (tw.)\n\n'
        'Start\n'
        '  ^$boo\n'
        '  ^$hoo -> Next.Record\n\n'
        'EOF\n'
    )
    _, _, trace = self.Parse(template, 'one\ntwo')

    data = trace.to_dict()
    self.assertEqual(len(data['lines']), 2)
    self.assertEqual(len(data['records']), 1)
    self.assertEqual(data['records'][0]['cells']['boo']['source_lines'], [1])
    self.assertEqual(data['lines'][0]['disposition'], 'data')

  def testSummaryCountsDispositions(self):
    template = (
        'Value a (A.)\n\n'
        'Start\n'
        '  ^-- -> State1\n'
        '  ^$a -> Record\n\n'
        'State1\n'
        '  ^-- -> Start\n'
    )
    _, _, trace = self.Parse(template, '--\n--\nA1\njunk')

    self.assertEqual(
        trace.summary(),
        {
            'unmatched': 1,
            'control': 2,
            'data': 1,
            'discarded': 0,
            'unparsed': 0,
        },
    )


if __name__ == '__main__':
  unittest.main()
