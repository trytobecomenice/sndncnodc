# Offline Quant Research Toolkit

This directory is isolated from the copy bot's database, risk manager, keys, and order path. The
tools report evidence; they do not promote/mute wallets or authorize trades.

## Environment

```bash
python3 -m venv .venv-research
.venv-research/bin/python -m pip install -r research/requirements.txt
```

Do not install these dependencies into the production bot service.

## Hostile autopsy test

```bash
python3 tools/autopsy_stress_tester.py \
  --output-dir research/output/autopsy-stress
```

The test covers a 50% SELL-side flash move, a five-wallet information cascade, and an unknown/empty
book. Signal clusters have two independent limits: maximum adjacent gap and maximum first-to-last
diameter. A cluster is a candidate dependent observation, not proof that its wallets share an owner.
Activity/message-rate termination is not estimated from the present signal-triggered schema; it
requires a continuous monotonic-time message-count series plus a hard safety diameter.

## Dashboard

After normalizing a Phase-0 journal:

```bash
streamlit run research/quant_dashboard.py
```

The cohort tab separates realized SELL-paired PnL from open committed cost basis and lot age. Open
lots are never silently excluded, but they also receive no invented point-in-time MtM. The aging
warning threshold and US-hours reporting stratum are UI parameters, not trading gates.

## Virtual displayed CLOB

```bash
python3 research/virtual_matching_engine.py phase0-soak.jsonl \
  --tiers 3,5,10 --order-type FAK \
  --output research/output/virtual-fak.jsonl

python3 research/virtual_matching_engine.py phase0-soak.jsonl \
  --tiers 3,5,10 --order-type FOK \
  --output research/output/virtual-fok.jsonl
```

The engine uses fixed-point integers, sorts/walks recorded levels, returns partial FAK fills, kills
the entire FOK result when displayed depth is insufficient, and reports side-adjusted VWAP
slippage. Its scope is deliberately narrow: it does not model cancels, queue priority, hidden size,
operator ordering, latency, or fill probability. Therefore every result is a displayed-depth
counterfactual upper bound, not a simulated venue fill.

BUY supports `ORDER_NOTIONAL` (default) and conservative `ALL_IN_CAPITAL` budget modes. The
documented nonlinear fee formula is evaluated independently at every consumed level and floored to
the declared 0.00001-USDC precision. Missing fee rates remain unknown, never silently zero. The
model remains an assumption until checked against the deployed venue/SDK version's golden vectors:

```bash
python3 research/validate_matching_engine.py research/fixtures/venue-golden-vectors.jsonl
```

The validator fails closed for aggregate public trade tape. Such a tape mixes other participants,
queue priority, cancels, and replenishment, so a one-second volume comparison cannot prove that our
hypothetical FAK would fill.

## Event topology

```bash
python3 tools/build_event_graph.py \
  --output research/output/event-knowledge-graph-v1.json
```

Pass `--previous old-graph.json` to emit a topology-change audit. Every event carries its sync time,
observed market count, membership hash, source update time, and a conservative completeness state.
An active event is `OPEN_MUTABLE`; a closed event is still
`CLOSED_SNAPSHOT_NOT_PROVEN_IMMUTABLE`. Any membership change invalidates downstream research until
it is recomputed. The graph contains no pricing or margin logic.
