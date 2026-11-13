"""Contract samples for complete and interrupted inventory streams.

The samples pin two observable outcomes for every parse:

* `ParseText()` returns the rows that were actually committed.
* `TextFSM.pending` returns the current uncommitted tail. It is empty after a
  committed row or a clear, and contains the values assigned since then when
  the stream stops part way through a record.

`TextFSM.complete` distinguishes an input that reached its end condition from
one that stopped early. Templates that adopt a trailer rule (a transition to
`End` or `EOF`) are sealed only when the trailer is seen. Templates without a
trailer rule treat `eof=True` as acceptance of the end of the stream, provided
no uncommitted tail remains.

The carried-value cases use `Filldown,Required` values for warehouse context
and ordinary `Required` values for the inventory row. This combination is the
one that previously allowed a previous row's carry to masquerade as a final
successful row.
"""

import io
import unittest

import textfsm


INVENTORY_VALUES = (
    'Value Filldown,Required Warehouse (\\S+)\n'
    'Value Filldown,Required Batch (\\S+)\n'
    'Value Required Sku (\\S+)\n'
    'Value Required Quantity (\\d+)\n'
    'Value Required Note (\\S+)\n'
)


def inventory_template(trailer=False):
  """Builds the same inventory reader with or without the END trailer rule."""
  template = (
      INVENTORY_VALUES
      + '\nStart\n'
      + '  ^warehouse: ${Warehouse}\n'
      + '  ^batch: ${Batch}\n'
      + '  ^sku: ${Sku}\n'
      + '  ^qty: ${Quantity}\n'
      + '  ^note: ${Note} -> Record\n'
  )
  if trailer:
    template += '  ^END$$ -> End\n\nEOF\n'
  return template


IMPLICIT_INVENTORY_TEMPLATE = (
    INVENTORY_VALUES
    + '\nStart\n'
    + '  ^warehouse: ${Warehouse}\n'
    + '  ^batch: ${Batch}\n'
    + '  ^sku: ${Sku}\n'
    + '  ^qty: ${Quantity}\n'
    + '  ^note: ${Note}\n'
)


TWO_ROWS = (
    'warehouse: dc1\n'
    'batch: B1\n'
    'sku: router\n'
    'qty: 2\n'
    'note: putaway\n'
    'sku: switch\n'
    'qty: 3\n'
    'note: pick\n'
)

ROW_ONE = ['dc1', 'B1', 'router', '2', 'putaway']
ROW_TWO = ['dc1', 'B1', 'switch', '3', 'pick']

TAIL_AFTER_ROW_ONE = ['dc1', 'B1', 'switch', '3', '']


class InventoryStreamContractTest(unittest.TestCase):
  """Executable contract for committed rows and uncommitted tails."""

  def parse(self, template, data, eof=True):
    fsm = textfsm.TextFSM(io.StringIO(template))
    committed = fsm.ParseText(data, eof=eof)
    return committed, fsm.pending, fsm.complete

  def testExplicitRecordWithTrailerSealsWholeBatch(self):
    template = inventory_template(trailer=True)

    with self.subTest('trailer and trailing newline'):
      committed, pending, complete = self.parse(template, TWO_ROWS + 'END\n')
      self.assertEqual(committed, [ROW_ONE, ROW_TWO])
      self.assertEqual(pending, [])
      self.assertTrue(complete)

    with self.subTest('trailer without trailing newline'):
      committed, pending, complete = self.parse(template, TWO_ROWS + 'END')
      self.assertEqual(committed, [ROW_ONE, ROW_TWO])
      self.assertEqual(pending, [])
      self.assertTrue(complete)

  def testExplicitRecordWithoutTrailerIsNotComplete(self):
    template = inventory_template(trailer=True)

    # Every inventory row has already been committed by an explicit Record,
    # but the device batch is still open because the END trailer was not seen.
    with self.subTest('last row committed, trailer missing'):
      committed, pending, complete = self.parse(template, TWO_ROWS)
      self.assertEqual(committed, [ROW_ONE, ROW_TWO])
      self.assertEqual(pending, [])
      self.assertFalse(complete)

    # The same rule applies when the last committed row has no line ending.
    with self.subTest('last row committed without newline, trailer missing'):
      committed, pending, complete = self.parse(
          template, TWO_ROWS.rstrip('\n')
      )
      self.assertEqual(committed, [ROW_ONE, ROW_TWO])
      self.assertEqual(pending, [])
      self.assertFalse(complete)

  def testExplicitRecordLeavesInterruptedTailPending(self):
    template = inventory_template(trailer=True)
    interrupted = TWO_ROWS.split('note: pick')[0]

    committed, pending, complete = self.parse(template, interrupted)
    self.assertEqual(committed, [ROW_ONE])
    self.assertEqual(pending, TAIL_AFTER_ROW_ONE)
    self.assertFalse(complete)

  def testInterruptedStreamCanResumeAndSeal(self):
    template = inventory_template(trailer=True)
    fsm = textfsm.TextFSM(io.StringIO(template))

    # The first chunk ends after a complete line but before the second row's
    # note. The second row must stay out of the committed result.
    committed = fsm.ParseText(TWO_ROWS.split('note: pick')[0], eof=False)
    self.assertEqual(committed, [ROW_ONE])
    self.assertEqual(fsm.pending, TAIL_AFTER_ROW_ONE)
    self.assertFalse(fsm.complete)

    # Resuming the same FSM completes that row and then consumes the trailer.
    committed = fsm.ParseText('note: pick\nEND\n', eof=True)
    self.assertEqual(committed, [ROW_ONE, ROW_TWO])
    self.assertEqual(fsm.pending, [])
    self.assertTrue(fsm.complete)

  def testImplicitEofCommitsLastRecordWithoutFinalNewline(self):
    template = (
        'Value Filldown,Required Site (\\S+)\n'
        'Value Required Item (\\S+)\n'
        'Value Required Qty (\\d+)\n\n'
        'Start\n'
        '  ^site: ${Site}\n'
        '  ^item: ${Item}\n'
        '  ^qty: ${Qty}\n'
    )

    with self.subTest('no final newline'):
      committed, pending, complete = self.parse(
          template, 'site: dc1\nitem: router\nqty: 2'
      )
      self.assertEqual(committed, [['dc1', 'router', '2']])
      self.assertEqual(pending, [])
      self.assertTrue(complete)

    with self.subTest('final newline'):
      committed, pending, complete = self.parse(
          template, 'site: dc1\nitem: router\nqty: 2\n'
      )
      self.assertEqual(committed, [['dc1', 'router', '2']])
      self.assertEqual(pending, [])
      self.assertTrue(complete)

  def testImplicitEofDoesNotPromoteCarriedRequiredValues(self):
    template = (
        'Value Filldown,Required Site (\\S+)\n'
        'Value Filldown,Required Lot (\\S+)\n'
        'Value Required Item (\\S+)\n'
        'Value Required Qty (\\d+)\n\n'
        'Start\n'
        '  ^site: ${Site}\n'
        '  ^lot: ${Lot}\n'
        '  ^item: ${Item}\n'
        '  ^qty: ${Qty}\n'
    )

    # Header context alone satisfies no row: both Required row fields are
    # carried down as blank values, so no final row may be committed.
    with self.subTest('carried header only'):
      committed, pending, complete = self.parse(
          template, 'site: dc1\nlot: L1\n'
      )
      self.assertEqual(committed, [])
      self.assertEqual(pending, ['dc1', 'L1', '', ''])
      self.assertFalse(complete)

    # A fresh item without quantity is an incomplete tail, not a row.
    with self.subTest('fresh item without quantity'):
      committed, pending, complete = self.parse(
          template, 'site: dc1\nlot: L1\nitem: router\n'
      )
      self.assertEqual(committed, [])
      self.assertEqual(pending, ['dc1', 'L1', 'router', ''])
      self.assertFalse(complete)

    # A complete row still works and uses the carried header context.
    with self.subTest('complete row'):
      committed, pending, complete = self.parse(
          template, 'site: dc1\nlot: L1\nitem: router\nqty: 2'
      )
      self.assertEqual(committed, [['dc1', 'L1', 'router', '2']])
      self.assertEqual(pending, [])
      self.assertTrue(complete)

  def testNoTrailerEofArgumentSeparatesEndOfInputFromInterruption(self):
    template = inventory_template(trailer=False)

    with self.subTest('implicit eof accepted'):
      committed, pending, complete = self.parse(template, TWO_ROWS)
      self.assertEqual(committed, [ROW_ONE, ROW_TWO])
      self.assertEqual(pending, [])
      self.assertTrue(complete)

    with self.subTest('implicit eof accepted without final newline'):
      committed, pending, complete = self.parse(
          template, TWO_ROWS.rstrip('\n')
      )
      self.assertEqual(committed, [ROW_ONE, ROW_TWO])
      self.assertEqual(pending, [])
      self.assertTrue(complete)

    with self.subTest('interrupted before last row'):
      interrupted = TWO_ROWS.split('note: pick')[0]
      committed, pending, complete = self.parse(
          template, interrupted, eof=False
      )
      self.assertEqual(committed, [ROW_ONE])
      self.assertEqual(pending, TAIL_AFTER_ROW_ONE)
      self.assertFalse(complete)

  def testImplicitEofRejectsTailMissingRequiredField(self):
    # Implicit EOF means the template has no per-row Record delimiter, so the
    # whole inventory entity is committed only once the final Required field
    # is present.
    committed, pending, complete = self.parse(
        IMPLICIT_INVENTORY_TEMPLATE,
        'warehouse: dc1\nbatch: B1\nsku: switch\nqty: 3\n',
    )
    self.assertEqual(committed, [])
    self.assertEqual(pending, ['dc1', 'B1', 'switch', '3', ''])
    self.assertFalse(complete)

  def testPartialRowAfterCommittedRowKeepsOnlyItsOwnValues(self):
    template = inventory_template(trailer=True)
    committed, pending, complete = self.parse(
        template, TWO_ROWS + 'sku: hub\n'
    )

    self.assertEqual(committed, [ROW_ONE, ROW_TWO])
    # Record cleared the previous row's quantity and note, so the pending tail
    # shows exactly what the interrupted row did and did not supply.
    self.assertEqual(pending, ['dc1', 'B1', 'hub', '', ''])
    self.assertFalse(complete)


if __name__ == '__main__':
  unittest.main()
