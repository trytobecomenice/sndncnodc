# Trader-selection input audit — 2026-08-15

Status: local safety remediation implemented and tested. This does not change the current wallet
roster, Paper/Live state, HWM, or any production database; deployment/migration is a separate
operator action.

## Corrected data inventory

- Laptop `leaderboard_scan`: 2,924 rows, 792 case-normalized wallets, 2026-07-16 through
  2026-07-28. Typed `pnl_30d` is NULL on every row; typed `volume_30d` is present on 2,000 rows.
- Laptop and AWS `observed_trade`: zero rows. No writer for this table was found in the current
  repository.
- AWS Phase-0 journal: 1,191 `wallet_signal` records; 1,166 unique valid source fills after
  deduplication, covering 11 wallets. All 1,166 have the original Polymarket token quantity in
  `raw_signal_payload.size`.
- Laptop roster: three `track` profiles (`balthazar`, `fed-qmg-core`, `fed-warren-buffett`).
  AWS production roster: 17 `track` profiles. Balthazar is not in the AWS roster.

## Leaderboard finding

The old conclusion that `PnL > volume` is mathematically impossible was wrong. If volume is traded
USDC notional, a low-priced binary token can return many times its purchase notional when it pays
out. The ratio cannot classify a wallet as directional, maker, farmer, or bad data by itself.

There is nevertheless a real input-quality problem in the archived Bullpen payloads:

- many `realized_pnl_30d` and `realized_pnl_90d` values are byte-for-byte equal;
- some window values differ radically from `realized_pnl_all`; and
- `scanLeaderboard.ts` wrote the requested window PnL into typed `pnl_all_time`, leaving typed
  `pnl_30d` NULL.

The local mapper now keeps 30-day and all-time PnL separate for future scans. Historical rows are
not rewritten. `is_farmer` is not promoted to a typed decision field because all 199 inspected
values were the same default; missing model evidence must not become a negative farmer verdict.

Polymarket's official endpoint documents `vol` as trader trading volume and `pnl` as trader profit
and loss, but does not document enough accounting detail to derive a farmer classifier from their
ratio: <https://docs.polymarket.com/api-reference/core/get-trader-leaderboard-rankings>.

## 5,000-share observation

`research/wallet_archetype_evidence.py` v2 reads original `raw_signal_payload.size` and emits a
5,000-share large-fill proxy. It does not infer maker/taker or liquidity-reward status.

| Wallet | Nickname | Unique signals | Fills >= 5,000 shares | Ratio |
|---|---|---:|---:|---:|
| `0x56ac...77e` | strict-10 | 115 | 33 | 28.70% |
| `0x4478...2a4` | fed-warren-buffett | 100 | 12 | 12.00% |
| `0xcaab...4dd` | fed-qmg-core | 268 | 7 | 2.61% |
| `0x011f...122` | sports-scalper-1 | 299 | 6 | 2.01% |
| `0x71ed...338` | geo-pako | 47 | 4 | 8.51% |
| `0xe154...7a0` | quant-generalist-2 | 87 | 2 | 2.30% |
| `0x1465...072` | expand-1 | 19 | 1 | 5.26% |
| `0x5109...e9c` | political-whale-1 | 2 | 1 | 50.00% (insufficient sample) |

The remaining three observed wallets had zero fills at or above the threshold. A large-fill rate is
only an observation: price, side, two-way inventory cycling, rewards eligibility and causally
ordered maker/taker evidence are still required before labelling liquidity farming.

## Decision

Do not use archived Bullpen PnL, `is_farmer`, `PnL / volume`, or the 5,000-share proxy alone for
roster selection. Retain the large-fill evidence as a preregistered feature for later out-of-sample
testing against clean Ledger-v2 copyable net PnL.

## Implemented non-regression boundary

- `scanLeaderboard.ts` now uses only Polymarket `/v1/leaderboard`, with the correct
  `timePeriod=MONTH|ALL` parameter, explicit 50-row pagination, and a frozen documented category
  enum. The former Bullpen discovery/holder/smart-money paths were removed.
- The legacy global `scoreWallets.ts` command is safety-stopped until an auditable official raw
  global scorer exists. It makes no Bullpen calls and does not alter roster or sizing fields.
- `wallet_profile.status` is approved roster state. Research writes `recommended_status` and
  recommendation provenance separately; its conflict-update set cannot change approved status.
- Derived metrics now carry source/version/readiness. Python sizing and refill readers fail closed:
  old or unknown-provenance Bullpen values remain visible for audit but return no decision signal.
- Official raw category scoring marks only its category evidence ready; that does not implicitly
  bless a legacy global composite score.
- Bullpen has a separate read-only daily `status` canary. It writes JSON logs only and cannot touch
  the DB or submit/cancel an order.
- Python and Go execution adapters consume one shared response-contract vector file.
- Boundary tests fail if discovery/scoring imports Bullpen again or if the scorer's update set
  regains authority over roster columns.

The daily research cron must invoke `pnpm run research:wallets-daily` so the entire fail-fast chain
is covered by one log redirection. The Bullpen canary runs separately via
`pnpm run canary:bullpen-execution`; canary health never becomes a trader-selection input.

## Post-review sizing correction

A complete read-only scan of AWS `decision_journal` found 1,039 decisions carrying a
`sizing_tier`: 995 `base` (95.77%) and 44 `limit_order_frozen_at_signal_time`. Every one recorded
`trade_size_usd=5.0`; there were no category/composite decisions in that evidence window. Therefore
turning legacy global metrics off while retaining the historical $5 base fallback would have
silently increased the weakest former $3 Kelly allocations.

The no-provenance path now returns `UNPROVENANCED_TRADE_USD == MIN_TRADE_USD == $3` and journals
`sizing_tier="unprovenanced"`. Rule 25's composite tier and rule-set-v7 capital multiplier remain
dormant until an allow-listed official global scorer exists. This is intentional: no derived score
is safer than manufacturing one from official fields whose accounting semantics do not support it.
