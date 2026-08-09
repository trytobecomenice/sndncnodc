# Epoch v3 backlog

Status: accepts non-completeness-blocking findings after the epoch-v2 build
freeze. Entries here do not authorize a v2 code change, deployment, restart,
parameter adjustment or clock reset.

## Admission rule

During epoch v2, add a finding here unless it proves one of the following:

- evidence required to reconstruct the frozen estimand is being lost,
  corrupted or systematically omitted;
- the durable accounting state cannot be reconciled;
- exits can be prevented, or the entry interlock can be bypassed.

An improvement to precision, convenience, observability, throughput, alert
wording, model choice or an additional defensive gate is not completeness
blocking. Review this backlog only after the v2 terminal decision or a formally
recorded system-integrity stop.

## Deferred findings

None at build-freeze creation.
