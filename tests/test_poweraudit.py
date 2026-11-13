"""Unittest for the power metering audit toolkit."""

import io
import json
import os
import unittest
from contextlib import redirect_stdout

from poweraudit import cli
from poweraudit import parser as audit_parser
from poweraudit.engine import (
    ALIAS,
    AMBIGUOUS,
    DUAL,
    EVENT_CHANGE,
    LOAD_CHANGE,
    NO_FIT,
    CardSample,
    ChassisSample,
    Event,
    MISSING,
    alias_consistent,
    audit,
    build_segments,
    card_contribution,
)

EXAMPLES = os.path.join(os.path.dirname(__file__), '..', 'examples')


def sample(time=1, slot='1', model='X', rail_a=10.0, rail_b=10.0,
           tolerance=0.2):
  return CardSample(time, slot, model, rail_a, rail_b, tolerance)


class UnitTestContribution(unittest.TestCase):
  """Tests per-card contribution semantics."""

  def testAliasCountsOneColumn(self):
    row = sample(rail_a=10.0, rail_b=10.2)
    power, uncertainty = card_contribution(ALIAS, row)
    self.assertEqual(power, 10.0)
    self.assertEqual(uncertainty, 0.2)

  def testAliasFallsBackToSecondName(self):
    row = sample(rail_a=MISSING, rail_b=11.0)
    power, _ = card_contribution(ALIAS, row)
    self.assertEqual(power, 11.0)

  def testDualSumsBothRailsAndTolerances(self):
    row = sample(rail_a=10.0, rail_b=12.0, tolerance=0.3)
    power, uncertainty = card_contribution(DUAL, row)
    self.assertEqual(power, 22.0)
    self.assertEqual(uncertainty, 0.6)

  def testDualMissingChannelIsNotZeroFilled(self):
    row = sample(rail_a=10.0, rail_b=MISSING)
    self.assertIs(card_contribution(DUAL, row), MISSING)
    self.assertIs(card_contribution(ALIAS, CardSample(
        1, '1', 'X', MISSING, MISSING, 0.2)), MISSING)

  def testAliasConsistencyUsesCombinedTolerance(self):
    self.assertTrue(alias_consistent(
        [sample(rail_a=10.0, rail_b=10.4, tolerance=0.2)]))
    self.assertFalse(alias_consistent(
        [sample(rail_a=10.0, rail_b=10.5, tolerance=0.2)]))


class UnitTestSegments(unittest.TestCase):
  """Tests event driven segment construction."""

  def testBuildSegmentsAndRemoval(self):
    events = [
        Event(1, '1', 'INSERT', 'X'),
        Event(3, '1', 'REMOVE'),
        Event(4, '1', 'INSERT', 'Y'),
    ]
    segments_by_slot = build_segments(
        events, [sample(1, model='X'), sample(4, model='Y')])
    segments = segments_by_slot['1']
    self.assertEqual(len(segments), 2)
    self.assertEqual(segments[0].model, 'X')
    self.assertEqual(segments[0].end, 3)
    self.assertTrue(segments[1].covers(4))

  def testSampleOutsideSegmentRejected(self):
    with self.assertRaises(ValueError):
      build_segments([Event(2, '1', 'INSERT', 'X')], [sample(1)])

  def testSampleModelMismatchRejected(self):
    with self.assertRaises(ValueError):
      build_segments([Event(1, '1', 'INSERT', 'X')],
                     [sample(1, model='Z')])


class UnitTestAudit(unittest.TestCase):
  """End to end inference on hand built scenarios."""

  def testLoadChangeDiscriminatesRelation(self):
    chassis, cards, events = audit_parser.load_audit_file(
        os.path.join(EXAMPLES, 'power_audit_decisive.audit'))
    result = audit(chassis, cards, events)
    self.assertEqual(result.verdicts['X'], DUAL)
    self.assertEqual(result.verdicts['Y'], DUAL)
    self.assertEqual(len(result.fits), 1)
    evidence = result.discriminating['X']
    self.assertEqual(evidence.time, '2')
    self.assertEqual(evidence.kind, LOAD_CHANGE)
    self.assertEqual(evidence.surviving, DUAL)

  def testStaticFullLoadSnapshotsStayAmbiguous(self):
    chassis, cards, events = audit_parser.load_audit_file(
        os.path.join(EXAMPLES, 'power_audit_ambiguous.audit'))
    result = audit(chassis, cards, events)
    self.assertEqual(result.verdicts['Z'], AMBIGUOUS)
    self.assertEqual(len(result.fits), 2)
    self.assertNotIn('Z', result.discriminating)

  def testMissingChannelStaysUnverifiable(self):
    chassis, cards, events = audit_parser.load_audit_file(
        os.path.join(EXAMPLES, 'power_audit_ambiguous.audit'))
    result = audit(chassis, cards, events)
    self.assertIn('3', result.unverifiable)
    dual_fit = next(fit for fit in result.fits
                    if fit.assignment['Z'] == DUAL)
    self.assertEqual([row.time for row in dual_fit.unverifiable], ['3'])

  def testRemovalEventDiscriminatesRelation(self):
    chassis, cards, events = audit_parser.load_audit_file(
        os.path.join(EXAMPLES, 'power_audit_event.audit'))
    result = audit(chassis, cards, events)
    self.assertEqual(result.verdicts['W'], ALIAS)
    evidence = result.discriminating['W']
    self.assertEqual(evidence.time, '2')
    self.assertEqual(evidence.kind, EVENT_CHANGE)

  def testNoRelationFitsImpossibleTotals(self):
    chassis = [ChassisSample(1, 100.0, 0.1),
               ChassisSample(2, 105.0, 0.1)]
    cards = [CardSample(1, '1', 'P', 5.0, 5.0, 0.1),
             CardSample(2, '1', 'P', 5.0, 5.0, 0.1)]
    events = [Event(1, '1', 'INSERT', 'P')]
    result = audit(chassis, cards, events)
    self.assertEqual(result.verdicts['P'], NO_FIT)
    self.assertEqual(result.fits, [])

  def testInconsistentColumnsRuleOutAliasDirectly(self):
    chassis = [ChassisSample(1, 130.0, 0.5)]
    cards = [CardSample(1, '1', 'Q', 10.0, 20.0, 0.5)]
    events = [Event(1, '1', 'INSERT', 'Q')]
    result = audit(chassis, cards, events)
    self.assertEqual(result.verdicts['Q'], DUAL)
    self.assertTrue(all(fit.assignment['Q'] == DUAL for fit in result.fits))

  def testNoFitWhenEvenDualCannotExplainInconsistentCard(self):
    chassis = [ChassisSample(1, 100.0, 0.1),
               ChassisSample(2, 120.0, 0.1)]
    cards = [CardSample(1, '1', 'R', 10.0, 20.0, 0.1),
             CardSample(2, '1', 'R', 10.0, 20.0, 0.1)]
    events = [Event(1, '1', 'INSERT', 'R')]
    result = audit(chassis, cards, events)
    self.assertEqual(result.verdicts['R'], NO_FIT)
    self.assertEqual(result.fits, [])

  def testSameModelKeepsOneRelationAcrossReseat(self):
    events = [Event(1, '1', 'INSERT', 'P'),
              Event(3, '1', 'REMOVE'),
              Event(4, '1', 'INSERT', 'P')]
    cards = [
        CardSample(1, '1', 'P', 10.0, 10.0, 0.2),
        CardSample(4, '1', 'P', 10.0, 14.0, 0.2),
    ]
    chassis = [ChassisSample(1, 110.0, 0.5),
               ChassisSample(4, 114.0, 0.5)]
    result = audit(chassis, cards, events)
    self.assertEqual(result.verdicts['P'], DUAL)
    for fit in result.fits:
      self.assertEqual(set(fit.assignment), {'P'})


class UnitTestParsers(unittest.TestCase):
  """Tests canonical file and TextFSM CLI echo parsing."""

  def testCanonicalFileRoundTrip(self):
    chassis, cards, events = audit_parser.load_audit_file(
        os.path.join(EXAMPLES, 'power_audit_decisive.audit'))
    self.assertEqual(len(chassis), 3)
    self.assertEqual(chassis[0].total, 170.2)
    self.assertEqual(len(cards), 5)
    self.assertEqual(events[-1].action, 'REMOVE')
    self.assertIsNone(events[-1].model)

  def testCanonicalFilePreservesMissingRail(self):
    _, cards, _ = audit_parser.load_audit_file(
        os.path.join(EXAMPLES, 'power_audit_ambiguous.audit'))
    self.assertIs(cards[-1].rail_b, MISSING)
    self.assertIsNotNone(cards[-1].rail_a)

  def testChassisEchoTemplate(self):
    with open(os.path.join(EXAMPLES, 'power_echo_chassis.txt')) as handle:
      rows = audit_parser.parse_chassis_echo(handle.read())
    self.assertEqual(len(rows), 3)
    self.assertEqual(rows[1].time, '2')
    self.assertEqual(rows[1].total, 180.1)

  def testCardEchoTemplate(self):
    with open(os.path.join(EXAMPLES, 'power_echo_cards.txt')) as handle:
      rows = audit_parser.parse_card_echo(handle.read())
    self.assertEqual(len(rows), 5)
    self.assertEqual(rows[0].slot, '1')
    self.assertEqual(rows[0].rail_b, 10.2)

  def testEventEchoTemplateHandlesBareRemoval(self):
    with open(os.path.join(EXAMPLES, 'power_echo_events.txt')) as handle:
      rows = audit_parser.parse_event_echo(handle.read())
    self.assertEqual(len(rows), 3)
    self.assertEqual(rows[-1].action, 'REMOVE')
    self.assertIsNone(rows[-1].model)

  def testEchoInputsFeedEndToEndAudit(self):
    with open(os.path.join(EXAMPLES, 'power_echo_chassis.txt')) as handle:
      chassis = audit_parser.parse_chassis_echo(handle.read())
    with open(os.path.join(EXAMPLES, 'power_echo_cards.txt')) as handle:
      cards = audit_parser.parse_card_echo(handle.read())
    with open(os.path.join(EXAMPLES, 'power_echo_events.txt')) as handle:
      events = audit_parser.parse_event_echo(handle.read())
    result = audit(chassis, cards, events)
    self.assertEqual(result.verdicts['X'], DUAL)
    self.assertEqual(result.verdicts['Y'], DUAL)


class UnitTestCli(unittest.TestCase):
  """Tests the command line entry point."""

  def testTextReport(self):
    path = os.path.join(EXAMPLES, 'power_audit_decisive.audit')
    buffer = io.StringIO()
    with redirect_stdout(buffer):
      exit_code = cli.main([path])
    self.assertEqual(exit_code, 0)
    self.assertIn('POWER METERING AUDIT', buffer.getvalue())
    self.assertIn('X: DUAL', buffer.getvalue())

  def testJsonReport(self):
    path = os.path.join(EXAMPLES, 'power_audit_event.audit')
    buffer = io.StringIO()
    with redirect_stdout(buffer):
      cli.main([path, '--json'])
    payload = json.loads(buffer.getvalue())
    self.assertEqual(payload['verdicts']['W'], ALIAS)
    self.assertEqual(
        payload['discriminating_evidence']['W']['kind'], EVENT_CHANGE)


if __name__ == '__main__':
  unittest.main()
