# Ledger-v2 forensic stop rule and reconciliation tie-out

Status: preregistered engineering boundary. This document does not authorize
Live, reset HWM, change a wallet roster, or convert unknown history into zero.

## Purpose

Historical reconstruction has two decision-relevant jobs:

1. prevent phantom/event-truncated PnL from feeding wallet EV, caps, mute,
   rehabilitation, portfolio equity or research evidence; and
2. establish a clean, versioned cutoff after which epoch-v2 evidence is fully
   causal, sequenced and auditable.

It is not required to manufacture exact economics from evidence that never
recorded causal order. `UNKNOWN` is an allowed result; silent guessing is not.

## Fixed forensic budget

One final additive sequence migration and one stopped-state reconciliation-v4
are authorized. Re-running the identical v4 report after a transactional
rollback does not consume a second attempt; changing the allocator, evidence
population or causal assumptions does.

After v4:

- an implementation, schema, current-live-event, reader, seal or post-cutoff
  integrity failure remains completeness-blocking and must be fixed;
- a pre-cutoff lot that cannot be uniquely reconstructed from immutable source
  evidence is labelled `HISTORICAL_UNRECONSTRUCTABLE` with its reason and
  coverage, and is excluded from PnL, EV, ranking, promotion, mute/rehab,
  protocol power and strategy evidence;
- unresolved history is never assigned zero PnL, carried at cost, allocated by
  UUID order, or made clean by changing a threshold;
- no automatic HWM reset follows. Any clean-epoch baseline is a separate,
  operator-reviewed action with its own manifest and tie-out.

If exclusion would leave a decision reader mixing clean cumulative values with
legacy final-row values, readiness must remain absent. A historical stop rule
cannot be used to promote a partially authoritative ledger.

## Durable causal-order rule

`bot_event_log.event_sequence` is source evidence. Migration 0030 performs the
only permitted conversion from the then-current insertion `rowid`, before any
future table rebuild/dump reload can change it. Runtime allocation uses the
one-row `bot_event_sequence_counter` in the same `BEGIN IMMEDIATE` transaction
as the event insert.

After migration:

- reconciliation and audit never read implicit `rowid` as authority;
- `event_sequence` is unique, positive and immutable;
- the counter must exist and `next_value > MAX(event_sequence)`;
- allocation sequence must equal the retained source-event sequence;
- same-second order is `(timestamp,event_sequence)`, never UUID lexical order.

## Tie-out table

These figures are different estimators/snapshots, not successive values of one
unchanged ledger. Do not subtract them without matching population and cutoff.

| Label | Population / method | Reported result | Authority |
|---|---|---:|---|
| Legacy reported | final `paper_trade` row, phantom-contaminated | `+$620.40` | superseded |
| Classifier v1 | final-row PnL after first 455 phantom flags | about `-$48.60` | superseded |
| Resolution audit v2 | final-row PnL after 31 additional factual phantoms | about `-$78.50` | superseded; partial closes truncated |
| Early event reconstruction | 14,614 exact allocated realized events at its recorded cutoff | clean `-$20.369734`; phantom `+$717.385620` | forensic evidence, not final reader |
| AWS reconciliation-v3 | 35,490 events at a later/wider cutoff | 14,645 `bot_filtered`; 20,845 `shadow_rehab`; 0 challenger | apply rolled back on quantity invariant |
| Reconciliation-v4 | 35,579 stopped-state events; durable source sequence; SHA `1f22fbf2bf5cbef16fbf0c400d7ce27b301fbd1ec44ad4510ad4a680734f62c2` | 31,970 matched; 3,609 events / 488 lots explicitly historical-unreconstructable | candidate authority only after copy and production invariants pass |

The 14,614-to-35,490 change is not “v3 to v4.” It mixes different generation
times and populations. Reconciliation-v4 must emit, in-file:

- snapshot cutoff and source DB identity/hash evidence;
- event count by strategy and event type;
- clean/phantom, open/closed and termination-cause breakdown;
- row PnL, allocated event PnL and their difference;
- realized cost basis and quantity conservation;
- unmatched, ambiguous and historical-unreconstructable coverage.

## Stop decision

After v4 there are only three outcomes:

1. `PROMOTABLE`: exact report SHA, zero blocking failures, readiness written
   last in the same transaction.
2. `HISTORICAL_UNRECONSTRUCTABLE`: affected pre-cutoff history is explicitly
   excluded and epoch-v2 starts only after a separately approved clean reader
   boundary; no legacy decision input survives by accident.
3. `SYSTEM_BLOCKED`: source/current data, implementation, reader or seal
   integrity is unproven. Paper entry remains halted/fallback-only as specified;
   the issue cannot be waived as “historical.”

No fourth outcome permits weakening an invariant to make deployment pass.

## 2026-08-16 classification result

The 35,490 events initially missing allocations split into three disjoint
populations before backfill:

- 14,614 `bot_filtered` events at or before the v1 generation cutoff, tying
  exactly to the old v1-all-lots count: 13,390 now fact-clean and 1,224 phantom;
- 20,838 same-window `shadow_rehab` events that v1 never included; and
- 38 genuinely later events (31 `bot_filtered`, 7 `shadow_rehab`).

The final stopped snapshot contains 35,579 realized events because 89 runtime
allocations arrived before the stop. There are zero unmatched and zero
ambiguous events. Of the 1,251 lots with realized events, 763 have a complete
share trail (15 pre-existing acquisition authorities plus 748 historically
reconstructable lots); 488 old bot-filtered lots lack share fields and are
quarantined as `historical_unreconstructable` rather than guessed.

The sole apparent conservation mismatch was a one-micro rounding artifact:
`7.097458333766858 = 6.972629815412295 + 0.12482851835456277`. The old audit
rounded both reductions separately before summing. Ledger-v4 sums Decimal
quantities first and quantizes once; it does not alter the source acquisition.
