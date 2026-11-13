"""Core inference engine for the chassis power metering audit.

Two readings are published for every card.  For some models they are two
independent power rails (both must be counted towards the chassis total),
for others they are two names of the same sensor (counted once).  The
engine enumerates every per-model interpretation and keeps exactly the
interpretations that can explain every recorded chassis total within the
combined meter tolerances, while honouring card insert/remove events and
never inventing a value for a missing channel.

A single constant chassis overhead (fans, backplane, ...) is treated as
one shared unknown.  Because any static snapshot can be absorbed into
that overhead, interpretations can only be separated by recorded *load
changes*; the engine reports the earliest change that does so.
"""

import itertools

ALIAS = 'ALIAS'
DUAL = 'DUAL'
AMBIGUOUS = 'AMBIGUOUS'
NO_FIT = 'NO_FIT'
RELATIONS = (ALIAS, DUAL)

INSERT = 'INSERT'
REMOVE = 'REMOVE'

EVENT_CHANGE = 'EVENT'
LOAD_CHANGE = 'LOAD_CHANGE'

MISSING = None

_EPSILON = 1e-9


def time_key(value):
  """Sort key that keeps numeric-like time labels ordered numerically."""
  try:
    return (0, float(value))
  except (TypeError, ValueError):
    return (1, value)


class ChassisSample(object):
  """One timed chassis-wide total reading."""

  __slots__ = ('time', 'total', 'tolerance')

  def __init__(self, time, total, tolerance):
    self.time = time
    self.total = float(total)
    self.tolerance = float(tolerance)

  def __repr__(self):
    return 'ChassisSample(%r, %r, %r)' % (
        self.time, self.total, self.tolerance)


class CardSample(object):
  """One timed pair of card power readings for a slot.

  Rail values are floats or MISSING.  A missing channel stays missing:
  it is never treated as zero.
  """

  __slots__ = ('time', 'slot', 'model', 'rail_a', 'rail_b', 'tolerance')

  def __init__(self, time, slot, model, rail_a, rail_b, tolerance):
    self.time = time
    self.slot = slot
    self.model = model
    self.rail_a = self._as_float(rail_a)
    self.rail_b = self._as_float(rail_b)
    self.tolerance = float(tolerance)

  @staticmethod
  def _as_float(value):
    if value is None or value == '' or str(value).upper() == 'N/A':
      return MISSING
    return float(value)

  @property
  def both_present(self):
    return self.rail_a is not MISSING and self.rail_b is not MISSING

  def __repr__(self):
    return 'CardSample(%r, %r, %r, %r, %r)' % (
        self.time, self.slot, self.model, self.rail_a, self.rail_b)


class Event(object):
  """A card insertion or removal at a point in time."""

  __slots__ = ('time', 'slot', 'action', 'model')

  def __init__(self, time, slot, action, model=None):
    action = action.upper()
    if action not in (INSERT, REMOVE):
      raise ValueError('unknown event action %r' % action)
    self.time = time
    self.slot = slot
    self.action = action
    self.model = model

  def __repr__(self):
    return 'Event(%r, %r, %r, %r)' % (
        self.time, self.slot, self.action, self.model)


class Segment(object):
  """A half-open time interval [start, end) one slot holds one model."""

  __slots__ = ('slot', 'model', 'start', 'end')

  def __init__(self, slot, model, start, end=None):
    self.slot = slot
    self.model = model
    self.start = start
    self.end = end

  def covers(self, time):
    key = time_key(time)
    if key < time_key(self.start):
      return False
    return self.end is None or key < time_key(self.end)

  def __repr__(self):
    return 'Segment(%r, %r, %r, %r)' % (
        self.slot, self.model, self.start, self.end)


class FitResult(object):
  """Outcome of testing one global per-model relation assignment."""

  __slots__ = ('assignment', 'feasible', 'base_range',
               'unverifiable', 'violations')

  def __init__(self, assignment, feasible, base_range=None,
               unverifiable=None, violations=None):
    self.assignment = dict(assignment)
    self.feasible = feasible
    self.base_range = base_range
    self.unverifiable = unverifiable or []
    self.violations = violations or []

  def __repr__(self):
    return 'FitResult(%r, feasible=%r)' % (self.assignment, self.feasible)


class Evidence(object):
  """The earliest recorded change that resolves one model's relation."""

  __slots__ = ('model', 'time', 'kind', 'event', 'surviving', 'detail')

  def __init__(self, model, time, kind, surviving, detail, event=None):
    self.model = model
    self.time = time
    self.kind = kind
    self.event = event
    self.surviving = surviving
    self.detail = detail

  def __repr__(self):
    return 'Evidence(%r, %r, %r, %r)' % (
        self.model, self.time, self.kind, self.surviving)


class AuditResult(object):
  """Full audit verdict."""

  __slots__ = ('models', 'fits', 'verdicts', 'discriminating',
               'unverifiable', 'base_range')

  def __init__(self, models, fits, verdicts, discriminating,
               unverifiable, base_range):
    self.models = tuple(models)
    self.fits = fits
    self.verdicts = verdicts
    self.discriminating = discriminating
    self.unverifiable = unverifiable
    self.base_range = base_range


def build_segments(events, card_samples):
  """Group card samples into per-slot model segments from event times."""
  events_by_slot = {}
  for event in events:
    events_by_slot.setdefault(event.slot, []).append(event)

  segments_by_slot = {}
  for slot, slot_events in events_by_slot.items():
    slot_events.sort(key=lambda item: time_key(item.time))
    segments = []
    current = None
    for event in slot_events:
      if event.action == INSERT:
        if current is not None and current.end is None:
          current.end = event.time
        current = Segment(slot, event.model, event.time)
        segments.append(current)
      else:
        if current is not None and current.end is None:
          current.end = event.time
        current = None
    segments_by_slot[slot] = segments

  for sample in card_samples:
    segments = segments_by_slot.get(sample.slot, [])
    owners = [part for part in segments if part.covers(sample.time)]
    if not owners:
      raise ValueError(
          'card sample at %r slot %r falls outside any declared segment'
          % (sample.time, sample.slot))
    if len(owners) != 1:
      raise AssertionError('overlapping segments for slot %r at %r'
                           % (sample.slot, sample.time))
    owner = owners[0]
    if owner.model != sample.model:
      raise ValueError(
          'sample model %r does not match segment model %r for slot %r'
          % (sample.model, owner.model, sample.slot))
  return segments_by_slot


def active_segments(segments_by_slot, time):
  """Segments physically present in the chassis at ``time``."""
  active = []
  for segments in segments_by_slot.values():
    for segment in segments:
      if segment.covers(time):
        active.append(segment)
  return active


def card_contribution(relation, sample):
  """(power, uncertainty) a card sample contributes under one relation.

  Returns MISSING when the available channels cannot support the
  relation instead of silently substituting zero.
  """
  if relation == ALIAS:
    if sample.rail_a is not MISSING:
      return sample.rail_a, sample.tolerance
    if sample.rail_b is not MISSING:
      return sample.rail_b, sample.tolerance
    return MISSING
  if relation == DUAL:
    if sample.rail_a is MISSING or sample.rail_b is MISSING:
      return MISSING
    return (sample.rail_a + sample.rail_b,
            sample.tolerance + sample.tolerance)
  raise ValueError('unknown relation %r' % relation)


def alias_consistent(samples):
  """True when dual-named columns never differ beyond combined error.

  Two displays of one sensor may each deviate by their meter tolerance,
  so their gap may reach twice the stated tolerance.
  """
  for sample in samples:
    if not sample.both_present:
      continue
    if abs(sample.rail_a - sample.rail_b) > 2.0 * sample.tolerance + _EPSILON:
      return False
  return True


def _latest_samples(card_samples, segments_by_slot, time):
  """Latest card sample at or before ``time`` for each active segment."""
  chosen = {}
  cutoff = time_key(time)
  for sample in card_samples:
    if time_key(sample.time) > cutoff:
      continue
    for segment in active_segments(segments_by_slot, sample.time):
      if segment.model != sample.model or segment.slot != sample.slot:
        continue
      previous = chosen.get(segment)
      if previous is None or time_key(sample.time) >= time_key(
          previous.time):
        chosen[segment] = sample
  return chosen


def evaluate_assignment(assignment, chassis_samples, card_samples,
                        segments_by_slot):
  """Test one fixed per-model relation assignment against every total.

  Feasibility means one constant chassis overhead exists such that every
  recorded total is explained within the chassis meter tolerance plus
  the tolerances of the contributing card channels.
  """
  unverifiable = []
  lower_bounds = []
  upper_bounds = []

  for chassis in chassis_samples:
    latest = _latest_samples(card_samples, segments_by_slot, chassis.time)
    measured = 0.0
    uncertainty = 0.0
    blocked = False
    for segment in active_segments(segments_by_slot, chassis.time):
      sample = latest.get(segment)
      if sample is None:
        blocked = True
        break
      contribution = card_contribution(
          assignment[segment.model], sample)
      if contribution is MISSING:
        blocked = True
        break
      power, card_tolerance = contribution
      measured += power
      uncertainty += card_tolerance
    if blocked:
      unverifiable.append(chassis)
      continue
    band = chassis.tolerance + uncertainty
    lower_bounds.append(chassis.total - band - measured)
    upper_bounds.append(chassis.total + band - measured)

  violations = []
  if lower_bounds:
    base_low = max(lower_bounds)
    base_high = min(upper_bounds)
    if base_low > base_high + _EPSILON:
      violations.append(('empty-overhead', base_low, base_high))
  else:
    base_low, base_high = float('-inf'), float('inf')

  return FitResult(
      assignment,
      feasible=not violations,
      base_range=(base_low, base_high),
      unverifiable=unverifiable,
      violations=violations)


def _relation_prerequisites(models, card_samples):
  """Relations ruled out directly by the card's own columns."""
  allowed = {}
  for model in models:
    rows = [sample for sample in card_samples if sample.model == model]
    options = set(RELATIONS)
    if not alias_consistent(rows):
      options.discard(ALIAS)
    allowed[model] = options
  return allowed


def _enumerate_fits(models, allowed, chassis_samples, card_samples,
                    segments_by_slot):
  fits = []
  option_sets = [sorted(allowed[model]) for model in models]
  if any(not options for options in option_sets):
    return fits
  for choices in itertools.product(*option_sets):
    assignment = dict(zip(models, choices))
    fit = evaluate_assignment(
        assignment, chassis_samples, card_samples, segments_by_slot)
    if fit.feasible:
      fits.append(fit)
  return fits


def _change_description(previous_latest, latest, events_at_time, time):
  """Human-readable description of what changed at one sample time."""
  details = []
  for event in events_at_time:
    details.append('%s slot %s%s' % (
        event.action, event.slot,
        ' model %s' % event.model if event.model else ''))
  previous = {
      (segment.slot, segment.model): sample
      for segment, sample in previous_latest.items()}
  for segment, sample in latest.items():
    key = (segment.slot, segment.model)
    old_sample = previous.get(key)
    if old_sample is None:
      continue
    if (old_sample.rail_a != sample.rail_a or
        old_sample.rail_b != sample.rail_b):
      details.append(
          'slot %s model %s readings %s/%s -> %s/%s' % (
              segment.slot, segment.model,
              old_sample.rail_a, old_sample.rail_b,
              sample.rail_a, sample.rail_b))
  return '; '.join(details) if details else 'sample at %s' % time


def audit(chassis_samples, card_samples, events):
  """Run the full audit and return an :class:`AuditResult`."""
  segments_by_slot = build_segments(events, card_samples)
  models = sorted({sample.model for sample in card_samples})
  allowed = _relation_prerequisites(models, card_samples)
  fits = _enumerate_fits(
      models, allowed, chassis_samples, card_samples, segments_by_slot)

  verdicts = {}
  for model in models:
    relations_used = {fit.assignment[model] for fit in fits}
    if not relations_used:
      verdicts[model] = NO_FIT
    elif len(relations_used) == 1:
      verdicts[model] = next(iter(relations_used))
    else:
      verdicts[model] = AMBIGUOUS

  base_ranges = [fit.base_range for fit in fits
                 if fit.base_range[0] != float('-inf')]
  base_range = (
      (max(item[0] for item in base_ranges),
       min(item[1] for item in base_ranges))
      if base_ranges else None)

  discriminating = _discriminating_evidence(
      models, allowed, chassis_samples, card_samples, events)
  unverifiable = sorted(
      {sample.time
       for fit in fits for sample in fit.unverifiable},
      key=time_key)

  return AuditResult(
      models=models,
      fits=fits,
      verdicts=verdicts,
      discriminating=discriminating,
      unverifiable=unverifiable,
      base_range=base_range)


def _discriminating_evidence(models, allowed, chassis_samples,
                             card_samples, events):
  """Earliest recorded change that resolves each still-ambiguous model."""
  ordered_chassis = sorted(chassis_samples,
                           key=lambda sample: time_key(sample.time))
  ordered_events = sorted(events, key=lambda event: time_key(event.time))
  result = {}
  previous_relations = None
  previous_latest = {}

  for index, chassis in enumerate(ordered_chassis):
    cutoff = chassis.time
    cutoff_key = time_key(cutoff)
    prefix_events = [event for event in ordered_events
                     if time_key(event.time) <= cutoff_key]
    prefix_cards = [sample for sample in card_samples
                    if time_key(sample.time) <= cutoff_key]
    prefix_chassis = ordered_chassis[:index + 1]
    segments_by_slot = build_segments(prefix_events, prefix_cards)
    prefix_fits = _enumerate_fits(
        models, allowed, prefix_chassis, prefix_cards, segments_by_slot)
    relations_used = {
        (model, fit.assignment[model])
        for fit in prefix_fits for model in models}

    if previous_relations is not None:
      latest = _latest_samples(
          prefix_cards, segments_by_slot, cutoff)
      events_at_time = [event for event in ordered_events
                        if time_key(event.time) == cutoff_key]
      for model in models:
        if model in result:
          continue
        before = {relation for (name, relation) in previous_relations
                  if name == model}
        after = {relation for (name, relation) in relations_used
                 if name == model}
        if len(before) > 1 and len(after) == 1:
          kind = EVENT_CHANGE if events_at_time else LOAD_CHANGE
          result[model] = Evidence(
              model=model,
              time=cutoff,
              kind=kind,
              surviving=next(iter(after)),
              detail=_change_description(
                  previous_latest, latest, events_at_time, cutoff),
              event=events_at_time[0] if events_at_time else None)
    else:
      latest = _latest_samples(
          prefix_cards, segments_by_slot, cutoff)

    previous_latest = latest
    previous_relations = relations_used

  return result
