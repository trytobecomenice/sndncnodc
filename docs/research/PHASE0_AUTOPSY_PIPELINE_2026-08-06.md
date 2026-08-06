# Phase 0 Autopsy Pipeline — 2026-08-06

Status: offline research tool only. It does not import the production DB, mutate the trader roster,
call Polymarket, place orders, or run on the AWS trading service.

## What it answers

`phase0_autopsy.py` converts the recorder JSONL into one narrow row per signal, copy-size tier, and
observation checkpoint. It measures executable-price deterioration at T0 and every delayed
checkpoint present in the journal (currently T+100 ms and T+500 ms). Positive deterioration always
means worse for the copier:

- BUY: later executable ask VWAP minus the comparison price;
- SELL: comparison price minus later executable bid VWAP.

The output contains two distinct comparisons: source reported fill price -> our observed executable
price, and our T0 executable price -> delayed executable price. They must not be conflated. The first
contains source visibility/impact and local delay; the second isolates post-detection book movement
more closely.

## Causal contract

The tool does **not** perform a nearest-timestamp join. Delayed market observations are joined to
signals only by the recorder's exact `correlation_id`, scoped to the source journal. A delayed book
enters the decay distribution only when the online recorder set
`book_known_by_capture_deadline=true`. Late captures and books that cannot be proven known at the
target remain in the dataset as censored observations with `causal_valid=false`.

This prevents obvious future leakage but does not manufacture an exchange sequence number. Public
WS local generation proves recorder ordering only; it cannot prove queue priority, a source
pre-trade book, or that a hypothetical FOK/FAK would fill.

## Runbook

Normalize with no extra dependencies:

```bash
python3 phase0_autopsy.py data/phase0-soak.jsonl \
  --output-dir data/phase0-autopsy --normalize-only
```

Install the isolated research dependencies in a research environment, then create compressed
Parquet, grouped coverage/decay JSON, and an interactive curve:

```bash
python3 -m pip install -r requirements-phase0-analysis.txt
python3 phase0_autopsy.py data/phase0-soak.jsonl \
  --output-dir data/phase0-autopsy --plot
```

No decision threshold appears in this pipeline. It reports counts, causal coverage, liquidity
failures, medians, and distribution quantiles. Promotion/muting rules belong to a later, separately
versioned research policy after out-of-sample evidence exists.

## Safe-Build boundary for trader research

The proposed Sybil, trader-archetype, and regime work must start as evidence features, not verdicts:

- a common centralized-exchange funding address is weak evidence because unrelated customers share
  exchange hot wallets; funding links require provenance labels and conservative weighting;
- public activity does not reliably expose maker/taker role for every arbitrary wallet, so a
  `maker_ratio` must remain unknown unless a documented source proves it; execution patterns may
  support a probabilistic `maker_like` or `copyability` feature, not a fabricated fact;
- synchronized trades need timestamp-uncertainty bands, obscure-market overlap, direction, size,
  and exit similarity plus a null/permutation baseline before addresses are clustered as one entity;
- short holding periods are not automatically noise. They may represent real but uncopyable fast
  alpha, so strategy archetype and copyability are separate labels;
- executable entry/exit edge and realized outcomes are separate axes. Bayesian shrinkage,
  walk-forward evaluation, minimum sample support, and regime/change-point evidence are required
  before any roster action.

Any future tracker output must first be a static, versioned research artifact such as
`trader_profile_v1.json`. It must not write the bot DB or alter the 24-hour Phase 0 test without a
separate reviewed promotion step.
