# poweraudit — chassis power metering audit

Every card publishes two power columns. For some card models the columns
are two independent rails, for others they are two names of one sensor.
This package determines which per-model relations can simultaneously
explain every recorded chassis total, using:

- timed chassis-wide totals with a meter tolerance,
- timed per-slot card readings with their own meter tolerance,
- card insert/remove events that bound each model's presence,
- missing channels recorded as `N/A` (never zero filled).

## Model

For card sample `s` of a card present at chassis sample time `t`:

- `ALIAS` — one sensor, two names. Contribution is one column (the first
  available), with uncertainty equal to the card meter tolerance.
- `DUAL` — two independent rails. Contribution is `rail1 + rail2`, with
  uncertainty equal to the sum of the two channel tolerances. If either
  channel is missing the sample cannot be evaluated instead of being
  treated as zero.

A card model keeps one relation for its whole lifetime: two seating
segments of the same model share the same interpretation. A single
constant chassis overhead `B` (fans, backplane, ...) is shared by every
chassis sample. An assignment of relations is feasible when one interval
for `B` satisfies every total:

```
total_t - tol_total - sum(uncertainty) <= B + sum(card power)
                                    <= total_t + tol_total + sum(...)
```

All per-model relation combinations are enumerated; only assignments
with a non-empty common `B` interval survive. A model whose two columns
ever differ by more than twice the card tolerance cannot be `ALIAS`.

Because a static full-load snapshot can always be absorbed into `B`,
snapshots alone often leave both relations feasible. The audit replays
the chassis samples in time order and reports the earliest recorded
change (a card insert/remove event or a load change in the card
readings) that leaves only one relation standing. If no recorded change
separates them, the model is reported as `AMBIGUOUS` rather than guessed.

## Canonical input file

```
# [chassis] time, total_w, tolerance_w
# [cards]   time, slot, model, rail1_w, rail2_w, tolerance_w
# [events]  time, slot, action, model   (model omitted for REMOVE)

[chassis]
1, 170.2, 0.5

[cards]
1, 1, X, 10.0, 10.2, 0.2
3, 1, Z, 12.0, N/A, 0.5

[events]
1, 1, INSERT, X
3, 1, REMOVE
```

CLI echoes can alternatively be parsed through the TextFSM templates in
`poweraudit/templates/` via `poweraudit.parser.parse_chassis_echo`,
`parse_card_echo` and `parse_event_echo`.

## Usage

```
python -m poweraudit examples/power_audit_decisive.audit
python -m poweraudit examples/power_audit_event.audit --json
```

The text report lists per-model verdicts, every feasible relation
assignment with its chassis overhead interval, the earliest
discriminating recorded change, and any chassis samples that stay
unverifiable because a channel is missing.

## Examples

- `examples/power_audit_decisive.audit` — full-load snapshots are
  ambiguous; the recorded load change settles the model as `DUAL`.
- `examples/power_audit_event.audit` — a card removal settles the model
  as `ALIAS`.
- `examples/power_audit_ambiguous.audit` — no recorded change separates
  the relations, and one missing rail keeps a chassis sample
  unverifiable rather than zero filled.
- `examples/power_echo_*.txt` — matching CLI echo inputs for the
  TextFSM templates.
