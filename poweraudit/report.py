"""Text and JSON reporting for audit results."""

import json

from poweraudit.engine import ALIAS, AMBIGUOUS, DUAL, EVENT_CHANGE, NO_FIT


_RELATION_TEXT = {
    ALIAS: 'one sensor, two names (count one column)',
    DUAL: 'two independent rails (count both columns)',
    NO_FIT: 'no relation can explain the recorded totals',
}


def format_range(value_range):
  if value_range is None:
    return 'unbounded'
  low, high = value_range
  return '[%.3f, %.3f] W' % (low, high)


def render_text(result):
  """Render an AuditResult as a human-readable report."""
  lines = []
  lines.append('POWER METERING AUDIT')
  lines.append('=' * 40)
  lines.append('')
  lines.append('Per-model verdicts:')
  for model in result.models:
    verdict = result.verdicts[model]
    if verdict == AMBIGUOUS:
      lines.append('  %s: AMBIGUOUS (both relations explain all totals)'
                   % model)
    else:
      lines.append('  %s: %s - %s'
                   % (model, verdict, _RELATION_TEXT[verdict]))

  lines.append('')
  lines.append('Assignments that explain every chassis total: %d'
               % len(result.fits))
  for fit in result.fits:
    relations = ', '.join(
        '%s=%s' % (model, fit.assignment[model]) for model in result.models)
    lines.append('  - %s | chassis overhead %s'
                 % (relations, format_range(fit.base_range)))

  if result.discriminating:
    lines.append('')
    lines.append('Discriminating recorded changes:')
    for model in result.models:
      evidence = result.discriminating.get(model)
      if evidence is None:
        continue
      kind = ('card insert/remove event'
              if evidence.kind == EVENT_CHANGE
              else 'load change')
      lines.append('  %s: %s at %s leaves only %s'
                   % (model, kind, evidence.time, evidence.surviving))
      lines.append('      %s' % evidence.detail)

  ambiguous = [model for model in result.models
               if result.verdicts[model] == AMBIGUOUS
               and model not in result.discriminating]
  if ambiguous:
    lines.append('')
    lines.append('No recorded change separates: %s'
                 % ', '.join(ambiguous))
    lines.append('  Full-load snapshots alone cannot do so; a load change or'
                 ' reseat with readings on both sides is required.')

  if result.unverifiable:
    lines.append('')
    lines.append('Chassis samples kept unverifiable (missing channel, not'
                 ' zero filled): %s'
                 % ', '.join(str(time) for time in result.unverifiable))
  return '\n'.join(lines)


def render_json(result):
  """Render an AuditResult as JSON."""
  payload = {
      'verdicts': {model: result.verdicts[model]
                   for model in result.models},
      'feasible_assignments': [
          {
              'assignment': fit.assignment,
              'chassis_overhead_w': _json_range(fit.base_range),
          }
          for fit in result.fits],
      'discriminating_evidence': {
          model: {
              'time': evidence.time,
              'kind': evidence.kind,
              'surviving_relation': evidence.surviving,
              'detail': evidence.detail,
          }
          for model, evidence in result.discriminating.items()},
      'unverifiable_chassis_times': result.unverifiable,
  }
  return json.dumps(payload, indent=2, sort_keys=True)


def _json_range(value_range):
  if value_range is None:
    return None
  low, high = value_range
  if low == float('-inf'):
    low = None
  if high == float('inf'):
    high = None
  return [low, high]
