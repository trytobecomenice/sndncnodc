# Copy-trading cross-component invariants

This register lists assumptions consumed by one component and maintained by
another. An invariant without an owner, a mechanical check and a failure
action is not an invariant.

| Invariant | Maintained by | Mechanical proof | Failure action |
|---|---|---|---|
| Every retained realized event E has exactly one matched allocation A | `db.append_log`, allocation backfill | hourly E-to-A anti-join | immediately latch Entry Interlock |
| Every unsealed A still has its E row | event retention | hourly A-to-E anti-join | immediately latch Entry Interlock |
| Pruned E history remains cryptographically bound to A | `prune_event_log`, `paper_trade_event_seal` | rederive canonical SHA-256/count/PnL micros/shares micros and the ordered previous-hash chain for every seal | abort prune or latch Entry Interlock |
| A sums equal each lot L | live allocator/backfill | per-lot event count and integer-micro PnL equality | immediately latch Entry Interlock |
| Acquired shares equal sold shares plus remaining shares | position persistence and allocation shares trail | `total_acquired_shares == SUM(shares_closed) + latest shares_remaining`; open DB shares equal remaining | immediately latch Entry Interlock; missing history is UNKNOWN |
| Retention seals cannot be edited or removed by normal application SQL | migration 0028 SQLite triggers | adversarial UPDATE/DELETE tests | SQLite abort; chain head is exported with each verified backup because a same-owner DBA can drop a trigger |
| Gate A measures the pricing pipeline, not dead inventory | TTP sweep schema v3 | denominator is `fetch_attempted_positions`; <10,000 attempts or <1,800 sweeps is UNKNOWN | fail qualification |
| Gate B exposes structural unpriceable inventory | persisted quarantine state and open ledger | count, cost/equity ratio with minimum USD 900 equity denominator, age/reconciliation and new-in-window count | fail/reset qualification; never invent a price |
| Unknown permanent API wording cannot block invisibly | behavioural TTP failure state | continuous failure age becomes `SUSPECTED_STRUCTURAL` | one operator alert; remains in Gate A pending review |
| One repeated error cannot suppress a different critical error | fingerprint alert state v2 | independent fingerprint OPEN/backoff/RECOVERED state | distinct alert; critical alerts bypass the generic manager |
| Evaluator and preflight cannot disagree about a gate | `qualification_gates.py` | both import the same pure functions | test/build failure |

Money and share conservation use integer micros at the proof boundary. SQLite
REAL remains for compatibility but is never compared by raw float equality.
The latest retention-seal chain head is an external recovery-manifest field;
SQLite append-only triggers alone are not treated as protection from a process
or operator with schema-owner privileges.
