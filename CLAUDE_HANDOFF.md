# Claude Handoff — Polymarket Copybot

**Purpose:** this is the short, current handoff for Claude (or any next agent). It does not replace
the rule ledgers. Read this file first, then use `docs/copy-trading/RISK_MANAGEMENT.md` and
`docs/copy-trading/SAFETY.md` for the full rationale and implementation history.

## Project maturity — honest benchmark

Using **10/10** to mean a world-leading Wall Street/prop-shop platform, the project is currently
approximately **4/10 overall**:

- Architecture thinking: **6/10**
- Risk-control thinking: **6.5/10**
- Operational/system maturity: **4/10**
- Demonstrated net alpha: **2/10**

This is advanced for a personal project but not institution-grade. Its strongest evidence is the
risk/deployment discipline, deterministic replay foundation, paper/shadow isolation, passive
latency measurement, and explicit failure handling. Its largest unresolved question is still the
only one that economically matters: after source market impact, detection delay, spread, fees,
slippage, and execution uncertainty, does any followed trader retain positive **copyable net
alpha**?

Do not raise the score because more features were added. Raise it only at evidence gates:

```text
4/10  current engineering foundation
5/10  credible shadow attribution from real captured data
6/10  sequence-safe WS/REST resync and real-burst resource safety
7/10  small live executions agree with the shadow model
8/10  sustained positive net alpha after all observable friction
9/10  institutional redundancy, independent risk, reconciliation, and compliance
10/10 world-leading prop infrastructure and research operation
```

If real evidence disproves high-frequency copy alpha and the system pivots early to a slower,
high-conviction strategy, that is good quant research rather than failure.

**Last live verification:** 2026-08-05 15:38 HKT / 2026-08-05 07:38 UTC.

## Urgent P0 — 2026-08-07 Paper-ledger integrity quarantine

An external quant review produced strong evidence that historical replayed/post-event Paper
entries were closed as near-immediate `resolved` winners, materially inflating reported PnL and
potentially contaminating wallet EV caps, circuit breakers, category ranking, promotions, and the
equity HWM. Read
`docs/research/PAPER_LEDGER_INTEGRITY_INCIDENT_2026-08-07.md` before taking any economic action.

Codex has now independently reproduced and factually extended the failure on AWS. The authoritative
row ledger had 641 normal closes / raw `+$620.40`; causal v1 identified 455 candidates carrying
`+$669.00`. A Gamma resolution-time audit of all 186 remaining rows then classified 31 more as
factual phantom, 155 legit, and zero unknown. The fact-clean row ledger is now 155 rows /
`-$78.50` / `-9.07%` ROI. Do not quote any old positive total as demonstrated alpha, and do not
call `-$78.50` canonical until partial-close event reconciliation is complete.

Immediate operator rules:

- remain Paper-only;
- do not add wallets/strategies, raise size, alter roster status, or reseed HWM by hand;
- preserve the completed row-level read-only reproduction and SHA-256 manifest;
- preserve historical rows and use a versioned/auditable classification;
- deploy the tested mark-only isolation only after DB backup, migration dry-run, manifest match,
  and a controlled Paper restart; and
- repair TTP observability and exit coverage before strategy expansion.

Evidence gates completed on 2026-08-07:

- 52/52 open positions belonged to markets still active; zero open resolution verdicts were
  unknown or already closed.
- The 31-row v2 manifest SHA-256 is
  `a2b4c7c20cd16aaa70092402a72d51b4886c9f57f8491d1458574f4f210dd820`; v2 only marked metadata,
  deleted zero rows, changed no historical PnL, and did not alter HWM.
- All-row wallet-clustered ROI CI is negative on this snapshot, but current-eligible clustered CI
  crosses zero and effective wallet N is only six. No trader replacement is authorized.
- `pnl_snapshot` has started writing (22 rows by 02:17 HKT), but event-ledger realized `+$28.02`
  and row-ledger realized totals do not reconcile. This is now the first P0 investigation.

The 2026-08-07 P0 incident queue at the top of `docs/TODO_2026-08-06.md` supersedes older feature
priorities until the forensic and feedback-isolation gates pass.

## Non-negotiable operating rules

- This repository is Copy Bot only. Other bots live in separate workspaces; do not add their code,
  schema, dependencies, documentation, capital, PnL, or deployment controls here.
- Copy Bot remains paper-only. Do not flip `LIVE_MODE` or introduce private-key custody.
- TypeScript/Drizzle owns schema and migrations; Python performs CRUD only.
- Preserve unrelated dirty-worktree changes. Do not bundle them into a task commit.
- Never commit or push `.env`, API tokens, SSH/private keys, or credentials. Stage explicit file
  names only; do not use `git add .` in this dirty worktree.
- A local `data/app.db` is not production truth. For live trading status, query the EC2 DB at
  `/home/ubuntu/polymarket-copybot/data/app.db` over the `polymarket-copybot` SSH host.
- Every completed change entry in this file must state: **When**, **Files adjusted**, and
  **What changed**. Also record tests, commit, GitHub push, and AWS verification when applicable.

## Current production truth

### Deployment and control plane

- EC2 host alias: `polymarket-copybot` (`/home/ubuntu/polymarket-copybot`).
- Git commit live: `328a22d` (`fix(copybot): tolerate blank last trade prices`); single paper bot
  PID `83881` after the controlled 15:30 HKT restart.
- `LIVE_MODE=False`.
- EC2 `config.py` has two intentional-but-uncommitted overrides:
  - `TRACKED_TRADERS_SOURCE="db"`
  - `ENABLE_OMS_SHADOW_MIRROR=True`
- Do not run `git reset --hard` on EC2: it would erase those production overrides.
- `watchdog.py` cron is installed every 2 minutes.
- `autodeploy.py` cron is installed every 5 minutes.
- Watchdog is active. `data/autodeploy.lock` remains intentionally present because `origin/main`
  also contains the not-yet-production-active quant-controls checkpoint; do not remove the lock
  until that larger deployment passes `docs/TODO_2026-08-06.md`.
- Daily wallet scan/score/category-discovery/send-approval cron is installed at 20:00 UTC.
- `telegram-approval-listener.service` is installed, enabled, and active.
- `omsd.service` is installed, enabled, active on `127.0.0.1:8090`, and the shadow mirror is on.
- EC2 filesystem was 58% used with 13 GB free; `data/app.db` was already 4.6 GB.

### Copy Bot roster

- DB `status='track'`: **17 wallets**.
- Circuit-breaker muted: **5 wallets**.
- Eligible for normal copying: **12 wallets**.
- Muted wallets at the live check:
  - `fed-warren-buffett`
  - `geo-pako`
  - `political-whale-1`
  - `strict-10`
  - `strict-4`
- Muted wallets remain polled so Shadow Rehab can collect evidence; muting blocks normal BUYs,
  never exits.
- Approval queue contained zero rows at the live check. The infrastructure is deployed, but no
  successful new-wallet approval/replacement was evidenced yet.
- `get_tracked_traders()` reads the DB only once at bot startup. A Telegram approval changes DB
  status but does not enter the running poll set until a safe bot restart.

### Copy Bot book and PnL

- `bot_filtered` closed at the earlier AWS check: **634**, raw ledger realized PnL **+$639.74**.
  This figure is quarantined and must not be described as economic PnL or alpha; see the urgent
  2026-08-07 incident section and its canonical incident document.
- `bot_filtered` open: **56**, cost basis **$356.26** at 03:50 HKT.
- HKT 2026-08-05 realized PnL at 03:50: **$0.00** (zero fully closed rows today).
- All 56 normal open positions had a successful recent mark at the live check.
- Positions with no successful mark for more than 24 hours: **0**.
- The three apparently-broken Israel/Iran positions seen in the developer laptop DB were stale
  local data, not live AWS positions. Do not force-close them from that evidence.
- Shadow Rehab is excluded from real Copy Bot exposure/PnL, but its table is large: 297 open rows
  and approximately $132,566 recorded cost basis at the live check. Investigate storage/accounting
  growth separately; do not mix it into normal portfolio PnL.

### PnL snapshots and TTP

- Daily Copy Bot snapshots were current through UTC date 2026-08-03, written at approximately
  07:00 HKT on 2026-08-04. The next row is not due until the 23:00 UTC trigger
  (07:00 HKT next morning).
- The laptop DB's apparent snapshot stoppage was not a production outage.
- At 12:31 HKT Polymarket began returning `last_trade_price=""` on otherwise-valid order books.
  The old parser called `float("")`; all 53 open-position marks failed every sweep, producing
  1,018 repeated errors by 15:17 HKT. This was an upstream optional-field shape change, not a
  trader signal.
- Production hotfix `328a22d` treats blank, malformed, and non-finite optional last-trade prices
  as unavailable while preserving valid live bid/ask data. Its first two post-restart sweeps
  marked all **53/53** open positions at 15:30:39 and 15:36:03 HKT (5m24s apart), with zero
  post-start errors.
- AWS open-position numeric columns were all stored as numeric SQLite types.
- Stale order books remain a real market-quality condition, but there were zero generic error
  events in the one-hour production sample immediately before P0.

## P0 live action — 2026-08-05

### Outcome — complete

- Verified the production commit, services, crons, DB source, roster, PnL, snapshots, position
  marks, and zombie count directly on EC2.
- Corrected two false conclusions caused by inspecting the developer laptop DB: production
  snapshots are current, and production has no three broken zombie positions.
- Performed a controlled paper-bot restart using the existing watchdog pause sentinel:
  old PID `28738` -> new PID `74194` at 03:11 HKT.
- Confirmed exactly one `python3 -u bot.py` process, commit `7983a79`, DB roster republished as
  17 tracked / 12 eligible / 5 muted, and watchdog unpaused.
- First post-restart TTP sweep marked all 56 normal open positions at 03:14:29 HKT with zero
  errors. Prometheus immediately after the sweep reported equity `$1,798.53`, 56 open positions,
  and `copybot_kill_switch_active=0`.
- The second TTP mark arrived at 03:22:31 HKT (8m02s later, not the configured 5m). By 03:25:48,
  the restarted process had emitted 2,530 resolved-market stale skips, 1,119 sell-with-no-position
  skips, and 16 paper sells, with zero exceptions. This was a live-loop workload failure, not a
  crash.
- Root cause: each `limit=20` wallet fetch can auto-page 10 times (200 rows), while per-wallet
  dedup retained only 100. The two halves repeatedly evicted each other and replayed forever.
- At 03:26 HKT the bot was gracefully stopped behind `data/watchdog_paused` to prevent further
  paper-ledger contamination. Telegram approval listener and OMS remained running.
- P0 fix stops live pagination at the first page overlapping known history and raises the
  per-wallet dedup cap 100 -> 500. Local Python suite: **662 passed**; tracked AWS suite:
  **655 passed**.
- Commit `93c193b` was pushed to GitHub `main`, fast-forwarded onto AWS, and the two EC2-only
  `config.py` overrides were preserved via a temporary stash/pop. `.env` and keys were neither
  staged nor pushed.
- The fixed paper bot started as the single PID `75742` at 03:44:42 HKT. The first cycle had a
  one-time catch-up from IDs already evicted before the fix: 78 stale-market skips and 2 dust
  `paper_sell` events. Those two sells covered only `$0.0133` cost basis and rounded to `$0.00`
  PnL. Across the broken restart and this catch-up, all 18 replay sells together covered `$0.0882`
  cost basis / `$0.00` rounded PnL. No DB row was manually rewritten; treat all 18 as
  restart-contaminated observations.
- After that catch-up, steady-state replay stopped: between the first and second TTP sweeps there
  were only 2 ordinary `skip_extreme_tail_entry_price` events, zero sells, and zero errors.
- TTP marks completed at 03:45:13 and 03:50:38 HKT, a 5m25s interval versus the broken 8m02s.
  Final Prometheus check: equity `$1,801.36`, 56 open positions, kill switch `0`.
- The watchdog pause and manual autodeploy lock were removed after the final documentation sync;
  normal watchdog/autodeploy operation resumed.

## Open risks and next decisions

1. Reconcile the EC2-only `config.py` overrides into a deliberate deployment configuration;
   autodeploy currently runs from a dirty production worktree.
2. Add an explicit, low-cost health marker for each successful TTP/equity sweep. Today success is
   inferred from `last_priced_at` and snapshot movement, while failures log directly.
3. Deploy and live-verify the new five-minute roster refresh; the implementation now picks up a
   Telegram-approved wallet without restart, but is not production-active yet.
4. Investigate why Shadow Rehab has grown the 4.6 GB DB so quickly and enforce retention/compaction
   without changing normal Copy Bot risk accounting.
5. Review concentration: historical PnL can still be dominated by a small number of wallets even
   when the roster count looks diversified.

### 2026-08-06 — Execution uncertainty and honest Paper PnL decision record

1. **When:** 2026-08-06 HKT.
2. **Files adjusted:**
   - `docs/research/SHADOW_REPLAY_ARCHITECTURE_2026-08-06.md` — added the jointly accepted
     off-chain priority/toxicity, provisional settlement, depth-survival, Paper PnL, and scale-gate
     decisions.
   - `CLAUDE_HANDOFF.md` — added this exact handoff entry.
3. **What changed:**
   - Recorded FOK reject/adverse-markout monitoring without claiming unproved operator
     front-running, VIP priority, MEV, or spoofing. Toxicity is a versioned research output with
     controls, minimum samples, uncertainty, TTL, hysteresis, and temporary quarantine—not an
     automatic permanent blacklist.
   - Split economic exposure from settlement finality: `MATCHED` freezes capital and counts full
     risk; only `CONFIRMED` settles the final ledger. User WS + authenticated REST are primary
     lifecycle sources and Polygon RPC is independent corroboration. `RETRYING` stays frozen;
     `FAILED` is reversed through append-only reconciliation.
   - Kept raw bounded L2 objective and moved depth survival/fill probability to strategy/research.
     FOK requires enough surviving eligible depth for the full size; FAK uses a fill-size
     distribution; maker simulation conservatively accounts for queue ahead.
   - Rejected a single deterministic Paper PnL. Reports must separate optimistic,
     latency-adjusted, persistence-qualified, and stress results, with the conservative result as
     headline. Journal gaps and missing volatile periods remain visible and can invalidate a
     positive claim.
   - Paper evidence can reject a strategy but cannot prove operator priority or live execution
     alpha. Any later micro-live canary requires separate approval, tiny caps, and agreement of
     actual fills/rejects/markouts/settlement with predicted intervals before AWS or capital scale.
   - Documentation only: no code, strategy, order, AWS, DB, `.env`, key, commit, push, or deploy
     was performed for this entry.

### 2026-08-06 — Agreed strategy and expansion decision record

1. **When:** 2026-08-06 HKT.
2. **Files adjusted:**
   - `docs/research/STRATEGY_AND_EXPANSION_DECISIONS_2026-08-06.md` — created as the canonical
     record of agreed Copy strategy identity, residual-alpha gate, execution policy, market quality,
     portfolio/capital decisions, and evidence-gated expansion roadmap.
   - `docs/research/SHADOW_REPLAY_ARCHITECTURE_2026-08-06.md` — linked the new strategy record and
     clarified which document owns architecture versus strategy conclusions.
   - `docs/TODO_2026-08-06.md` — added P4 shadow evidence and expansion gates.
   - `CLAUDE_HANDOFF.md` — added this exact change entry.
3. **What changed:**
   - Reframed the target as a wallet-conditioned signal engine: wallet actions update a posterior
     but never command a trade. Every entry must retain a positive conservative residual edge after
     actual-size VWAP, current fees, chase/decay, settlement, capital, and portfolio risk.
   - Accepted proposed first-live Copy entries as price-bounded FOK/FAK only; a negative-Taker-EV
     signal is dropped rather than converted to a resting Maker entry. Exits remain asymmetric and
     urgency-based. No live execution was enabled.
   - Recorded size-aware entry/exit liquidity and executable liquidation equity as risk truth;
     fixed universal spread/depth/volume cutoffs and mid/last-price realizable PnL were rejected.
   - Added logical dependency, sparse factor/scenario, shrinkage covariance, fractional-Kelly,
     CTF/settlement, and Merge cash-band decisions without claiming those controls are implemented.
   - Ordered future expansion: prove current Copy economics; deepen wallet intelligence; shadow an
     event/payoff graph; test one niche primary-source vertical; then consider leg-risk execution,
     a strategy allocator, LP, and external hedging. Each future strategy requires isolated code,
     ledger, capital, risk, kill switch, and attribution.
   - Recorded unsupported assumptions explicitly, including precise wallet target inference,
     universal maker-toxicity percentages, batch atomicity, per-order CLOB V2 nonce, full-signature
     caching, TWAP-created capacity, tiny-Canary size extrapolation, and Paper-only proof of alpha.
   - Documentation only: no Python/TypeScript runtime, order, strategy parameter, AWS, DB, external
     bot, `.env`, key, commit, push, or deploy was changed.

### 2026-08-06 — Phase 0 attribution walking skeleton

1. **When:** 2026-08-06 14:40–15:05 HKT.
2. **Files adjusted:**
   - `phase0_attribution.py` and `test_phase0_attribution.py` — added pure fixed-point Phase 0
     attribution and focused evidence tests.
   - `shadow_capture.py`, `shadow_replay.py`, `bot.py`, and
     `polymarket_simulator.py` — carried same-request market/fee and existing wallet-model context
     into the replayable event and added an exact blocking/observation gate trace.
   - `test_shadow_capture.py`, `test_shadow_replay.py`, `test_bot_passive_shadow.py`, and
     `test_polymarket_simulator.py` — covered the new journal fields, thin exit liquidity, unknown
     model handling, metadata reuse, and deterministic gate trace.
   - `docs/research/STRATEGY_AND_EXPANSION_DECISIONS_2026-08-06.md`,
     `docs/research/SHADOW_REPLAY_ARCHITECTURE_2026-08-06.md`,
     `docs/TODO_2026-08-06.md`, and this handoff — recorded exact implementation truth and gaps.
3. **What changed:**
   - Added actual-copy-size BUY book walking, current fee observation, projected immediate SELL-side
     liquidation, visible two-sided depth, chase, and immediate round-trip executable value using
     integer fixed-point fields.
   - Recorded observed source position action without claiming economic intent; stored the existing
     wallet sizing/model snapshot and event/category factor IDs without a second API/model call.
   - Kept reported source-to-receive time separate from unavailable poll/clock bounds. Wallet point
     probability is labeled as a point observation; residual-alpha LCB and scenario model remain
     explicitly unknown until calibrated.
   - Shadow decisions now state each blocking/observation gate, status, and reason. The module can
     only return `shadow_buy`/`skip`; it has no key, network, DB, or order capability.
   - Scope limitation: capture covers BUYs that reach the existing passive-shadow seam; earlier
     skips are not yet complete counterfactual coverage.
   - Verification: focused Phase 0/shadow/integrity/simulator suite **82 passed**; full workspace
     suite **740 passed**; Python compile and `git diff --check` passed.
   - Local only and feature-disabled: no commit, GitHub push, AWS change, DB change, `.env`, key,
     external bot, live order, or deployment was performed.

### 2026-08-06 — Standalone Phase 0 wallet/WS soak and SELL tax-lot ledger

1. **When:** 2026-08-06 15:05–18:55 HKT.
2. **Files adjusted:**
   - `phase0_soak.py` and `test_phase0_soak.py` — added pure incremental conviction and per-tier
     BUY/SELL shadow tax-lot state.
   - `phase0_soak_recorder.py` and `test_phase0_soak_recorder.py` — added the standalone public
     wallet/market-WS recorder, causal ingress/deadline contract, restart/dedup handling, bounded
     asset cache, Linux memory limit, and sanity-only warm-up option.
   - `inspect_phase0_soak.py` and `test_inspect_phase0_soak.py` — added a streaming JSONL coverage,
     timing, reconnect, quality, visibility-proxy, and PnL report.
   - `polymarket_data_api.py` and `test_polymarket_data_api.py` — added a caller-bounded first-page
     bootstrap without changing normal production pagination defaults.
   - `deploy/phase0-soak-recorder.service` — added a separate paper/read-only systemd unit with
     384 MB process/512 MB cgroup memory limits, 35% CPU quota, a 64-asset LRU, low I/O priority,
     and protected paths; production bootstrap sample count remains zero.
   - `requirements-phase0-soak.txt` — isolated the recorder's pinned `aiohttp` dependency from the
     trading bot and the unrelated dirty `requirements.txt` changes.
   - `phase0_attribution.py` — exposed the existing pure exact-size bid-side SELL walk.
   - `docs/research/SHADOW_REPLAY_ARCHITECTURE_2026-08-06.md`,
     `docs/research/STRATEGY_AND_EXPANSION_DECISIONS_2026-08-06.md`,
     `docs/TODO_2026-08-06.md`, and this handoff — recorded implementation truth and unresolved
     inference boundaries.
3. **What changed:**
   - Research dedup preserves separate same-wallet scaling fills; it removes only an identical
     fill surrogate. Production Copy Bot dedup was not changed. A rolling one-hour trade-count and
     notional feature is observation-only and labels an incomplete startup window.
   - BUYs create immutable shadow entry lots for `$3/$5/$10`. Source SELLs reduce only lots the
     shadow tier actually acquired. Default closure is conservative worst-execution-first with
     deterministic FIFO/LIFO sensitivity; unmatched SELLs and unfilled shares create no realized
     PnL. Raw source token `size` is preferred over fee-affected `usdcSize/price`, and the basis is
     journaled.
   - `wallet_signal_ingress` is persisted before REST. Signal-time book truth is the warmed public
     WS state already known at that monotonic instant; later REST supplies fee/event enrichment but
     is never back-dated as T0.
   - T+100/T+500 observations are armed immediately and record exact deadline, callback lateness,
     reconnect epoch/local generation, and whether the captured generation was already known by
     the target deadline. The public WS exposes no compatible exchange SeqID, so that field remains
     null and no blockchain-to-book causal join is claimed.
   - `reported_visibility_lag_ms` is explicitly a proxy because the public activity timestamp's
     exact semantic is undocumented. It does not prove Private RPC/MEV use and cannot auto-
     quarantine a wallet.
   - Local public-data sanity with one explicit startup sample and a 3-second WS warm-up observed
     both delayed books, 0.860 ms T+100 lateness, 1.239 ms T+500 lateness, 8.574 ms signal-time WS
     book age, zero poll error, and a clean stop. This proves recorder plumbing only, not alpha.
   - Verification: focused Phase 0 suite **120 passed** before final deadline tests; full workspace
     suite after all changes **759 passed**. No `.env`, key, credential, external bot, live order,
     or strategy size was changed.
   - Commit, GitHub, and AWS service state are recorded in the next deployment entry only; do not
     infer deployment from this implementation entry.

### 2026-08-06 — Phase 0 AWS soak deployment

1. **When:** 2026-08-06 18:55–19:01 HKT.
2. **Files adjusted:**
   - `deploy/phase0-soak-recorder.service` — tightened final AWS limits after host preflight.
   - `CLAUDE_HANDOFF.md` and
     `docs/research/SHADOW_REPLAY_ARCHITECTURE_2026-08-06.md` — recorded exact deployment truth.
3. **What changed:**
   - Implementation commit `649c9d8` and resource-limit commit `0f97c4e` were pushed to GitHub
     `main`. AWS was safely fast-forwarded from `328a22d` to `0f97c4e` with the existing
     `config.py` overrides preserved (`TRACKED_TRADERS_SOURCE="db"`, OMS mirror enabled).
   - `data/autodeploy.lock` remained present. The existing paper bot was not restarted and stayed
     PID `83881`; its DB journal continued producing events after deployment.
   - AWS preflight found 1,905 MB RAM, about 906 MB available, no swap, Python 3.14.4, and aiohttp
     3.14.3. The research service was therefore capped at 384 MB address space, 512 MB cgroup,
     35% CPU, 64 active assets, `Nice=10`, and idle I/O priority rather than risking a 1 GB cap.
   - AWS focused suite **122 passed**, Python compile passed, and `systemd-analyze verify` passed
     before service activation.
   - `phase0-soak-recorder.service` started at 2026-08-06 11:00:36 UTC (19:00:36 HKT), PID `96800`.
     After two polls it was active with zero restart, about 39.7 MB RAM, one healthy WS connection,
     0 poll errors, 0 malformed lines, p50 poll 909 ms, and no historical fake signal. Empty
     T+100/T+500 counts are expected until the first genuinely new followed-wallet fill.
   - Follow-up live checkpoint captured three genuinely new BUYs. The first two used WS books last
     updated 1.857s and 2.462s before signal ingress and created one then two lots in every
     `$3/$5/$10` tier using `source_reported_share_size`. The third saw a 15.821s-old WS book,
     emitted `ws_book_stale_at_signal`, and correctly created no lot. At the preceding completed
     inspector checkpoint, T+100 and T+500 were both 2/2 available with p50 callback lateness
     0.785ms and 0.854ms. Service remained at zero restart and about 46 MB RAM. This is useful gate
     evidence, not an alpha/PnL conclusion.
   - The journal is `/home/ubuntu/polymarket-copybot/data/phase0_soak_v1.jsonl`; it is runtime data,
     untracked by Git, and contains no key/order capability. `.env` and credentials were not
     staged, pushed, copied, or printed.
   - **24-hour gate remains pending.** Inspect after 2026-08-07 19:00 HKT with
     `python3 inspect_phase0_soak.py data/phase0_soak_v1.jsonl`, then review coverage, poll gaps,
     WS reconnects, T+100/T+500 lateness/availability, RSS/CPU, malformed lines, BUY/SELL mix,
     tax-lot lifecycle, and ten manual source-fill/VWAP samples before making any alpha or AWS-
     sizing claim.

## Change log

### 2026-08-07 02:19 HKT — Factual resolution audit and v2 isolation

1. **When:** 2026-08-07 01:45–02:19 HKT.
2. **Files adjusted:**
   - `audit_resolution_timing.py` and `polymarket_simulator.py` — factual Gamma resolution-time
     capture with explicit `phantom / legit / unknown` verdicts.
   - `analyze_clean_cohort.py` — separate equal-weight and cost-weighted estimators plus deterministic
     trade and wallet-cluster bootstraps; can filter by a reviewed resolution audit.
   - `apply_resolution_audit.py` — dry-run-first, exact-SHA-gated, mark-only v2 application.
   - `audit_paper_ledger.py`, `bot.py`, and associated tests — permanent residual screen and removal
     of one duplicate full event-log scan per snapshot cycle.
   - `docs/CURRENT_STATE.md`, `docs/TODO_2026-08-06.md`, this handoff, and the incident review —
     recorded the factual cohort and new event/row reconciliation blocker.
3. **What changed:**
   - Factual audit classified 31 of 186 v1-clean rows as post-resolution entries; all 23 fast
     resolved residuals were confirmed, plus eight `source_sell` rows. Unknown count was zero.
   - v2 marked exactly 31 rows under SHA `a2b4c7…dd820`; zero rows/PnL were deleted or rewritten and
     HWM remained `$1,112.60`.
   - Fact-clean row ledger is 155 / `-$78.50` / `-9.07%`. Current eligible evidence has only six
     effective wallets and a clustered CI crossing zero, so no roster change was made.
   - The open-book audit found all 52 markets active. `pnl_snapshot` now has 22 rows, disproving the
     earlier “writer absent” hypothesis, but exposed an approximately `$76.62` event-versus-row
     realized-PnL mismatch. Event-level reconciliation is now P0.
   - Local full suite: 804 passed, 2 skipped. Commits `3315698`, `52c9d31`, and `cb8e366` were pushed
     to GitHub and fast-forwarded on AWS. The existing Paper PID was deliberately not restarted;
     it still has the earlier loaded code while the autodeploy lock remains present.

### 2026-08-07 — P0 Paper-ledger isolation deployed to AWS

1. **When:** 2026-08-07 01:17–01:44 HKT.
2. **Files/data adjusted:**
   - GitHub/AWS checkout advanced to `a7f7a00`.
   - AWS `paper_trade` received the four migration-0023 integrity columns; 455 closed rows were
     marked with classifier v1 metadata. No row or PnL value was deleted/rebased.
   - AWS `bot_risk_state.equity_hwm` was cleared only after backup and manifest match, then
     naturally reseeded by the restarted Paper bot.
   - Recovery evidence written under AWS `data/backups/`: 4.9 GB SQLite backup, dry-run manifest,
     and applied manifest. These runtime files are untracked.
3. **What changed / verification:**
   - Source/backup page count, page size, full `sqlite_master` SHA-256, schema-object count, wallet
     count, and closed-row count matched. A full integrity scan was stopped after excessive EBS
     rereads; no source write was involved, and the completed SQLite backup remained intact.
   - Dry-run and apply both produced manifest SHA `7575595736b2fad08ecfee9043c3b39397f265d43076e8b5f9d3e2e65f640c33`:
     455 candidates / `+$668.998241`, 186 clean remainder / `-$48.599008`, zero open candidates.
   - Existing roster status remained exactly 12 unmuted track + 5 muted track; no automatic
     unmute/promotion/demotion occurred.
   - Paper PID `105229` started with `LIVE_MODE=False`, DB roster source, and OMS shadow enabled.
     HWM reseeded from `$2,173.60` contaminated to `$1,112.60`; 52/52 open rows marked fresh at
     01:44 HKT; post-restart error count was zero.
   - Phase-0 recorder stayed active throughout. Watchdog pause was removed after verification;
     autodeploy lock remains. `pnl_snapshot` still had zero rows and needs follow-up.
   - Migration 0022 would drop nine legacy Weather tables, so it was intentionally skipped. 0023
     was directly applied and journaled as the new baseline; all nine Weather tables remain.
   - Local full suite 795 passed / 2 skipped; AWS exact new P0 regressions 11/11 passed. The AWS
     full suite was stopped because an integration path scanned the 4.9 GB runtime DB and created
     unacceptable observer effect; tests need complete temp-DB isolation.

### 2026-08-07 — Independent P0 reproduction and local feedback isolation

1. **When:** 2026-08-07 HKT.
2. **Files adjusted:**
   - `audit_paper_ledger.py` and `test_audit_paper_ledger.py` — versioned, causal, dry-run-first,
     mark-only historical classifier with row manifest and HWM-reset option.
   - `packages/db/src/schema.ts`, migration `0023_lame_timeslip.sql`, and Drizzle metadata — added
     auditable phantom classification fields without deleting historical evidence.
   - `db.py`, `dashboard.py`, and `apps/dashboard/app/overview/page.tsx` — excluded only confirmed
     rows from economic decision inputs and operator totals; open confirmed rows are quarantined
     from loaded trading state.
   - `bot.py` and `test_bot_risk_checks.py` — normalized malformed/blank/non-finite price values at
     the TTP boundary so strings cannot abort a sweep comparison.
   - `test_db_quant_p0.py` and `test_db_wallet_ev_stats.py` — regression coverage for clean PnL and
     wallet EV inputs.
   - `docs/CURRENT_STATE.md`, `docs/TODO_2026-08-06.md`, the incident report, and this handoff —
     replaced pending-verification language with the independently reproduced AWS evidence.
3. **What changed:**
   - Verified the external review's central conclusion but rejected its exact blanket-duration
     classification as AWS truth. AWS causal v1 leaves 186 closes / `-$48.60` / `-4.60%` ROI.
   - Implemented a reversible classifier that never deletes or rebases rows and changes no wallet
     status automatically.
   - Full Python suite: 793 passed, 2 skipped. DB and dashboard TypeScript checks passed. Migration,
     dry-run, apply, manifest stability, and HWM reset passed against a copied local DB.
   - Local implementation only at this entry: no AWS DB mutation, process restart, GitHub push,
     `.env`, key, credential, or unrelated dirty-worktree file changed.

### 2026-08-07 — Paper-ledger integrity incident record and metric quarantine

1. **When:** 2026-08-07 HKT.
2. **Files adjusted:**
   - `docs/research/PAPER_LEDGER_INTEGRITY_INCIDENT_2026-08-07.md` — created.
   - `docs/CURRENT_STATE.md` — changed unqualified historical PnL into a quarantined raw-ledger
     figure and recorded the external candidate-clean estimate separately.
   - `docs/TODO_2026-08-06.md` — added a superseding P0 incident queue.
   - `CLAUDE_HANDOFF.md` — added this zero-context urgent handoff.
3. **What changed:**
   - Recorded all seven external-review findings: phantom PnL, feedback contamination, candidate
     negative strategy expectancy, ineffective Kelly mapping, TTP/error saturation, Shadow Rehab
     lifecycle failure, and unpriceable/capital-saturation risk gaps.
   - Recorded the positive controls that limited harm: Paper-only mode, stale guard, tests,
     comments, and auditability.
   - Separated allegations/candidate clean totals from independently verified AWS truth and made
     forensic reproduction the next P0 gate.
   - Documentation only: no trading code, DB, AWS process/config, wallet status, HWM, `.env`, key,
     credential, push, or deployment changed.

### 2026-08-06 — Process-isolation blueprint and repository-scope cleanup

1. **When:** 2026-08-06 HKT.
2. **Files adjusted:**
   - `docs/research/SHADOW_REPLAY_ARCHITECTURE_2026-08-06.md` — recorded the proposed C++ market-
     data sidecar, Python trading process, best-effort journal egress, independent risk supervisor,
     binary framing/precision contract, capital-opportunity capture, reconciliation states, and
     honest timestamp boundary.
   - `docs/TODO_2026-08-06.md` — added the protocol golden-vector, early-rejection, orphan-order
     reconciliation, and latency-naming implementation gates.
   - `config.py`, `packages/copy-trading/src/scanLeaderboard.ts`, and
     `apps/dashboard/app/overview/page.tsx` — removed stale secondary-project research comments;
     no trader address, discovery source, score, threshold, or runtime behavior changed.
   - `docs/polymarket-copybot.code-workspace` — verified the secondary project remains an external
     multi-root workspace link only. The link was briefly removed during cleanup, then restored
     immediately after Joey reported it missing from VS Code; no external file was deleted.
   - This handoff — recorded the exact boundary and verification state.
3. **What changed:**
   - Confirmed the active repository already has no secondary-project package, schema export,
     dependency, command, or documentation tree. Historical Drizzle migrations remain immutable:
     the latest forward migration removes the obsolete tables from old databases, and rewriting
     applied history could break existing AWS/local migration journals. The external workspace
     link is navigation only and does not reintroduce code, schema, dependency, Git history, or
     deployment coupling. A fresh migration test must confirm the final schema contains none of
     those obsolete tables.
   - The proposed Process M -> T transport is cross-platform Unix-domain `SOCK_STREAM` with
     length-prefixed, versioned frames. M uses event-armed coalesced push rather than a socket
     "mailbox" or permanent 1 kHz poll; a partial frame is completed while only the latest not-yet-
     started state may replace an older pending state.
   - M -> journal egress uses a preallocated lock-free SPSC ring. Network partial writes and TCP
     backpressure live outside the parser; overflow drops whole not-yet-enqueued journal frames and
     records sequence gaps rather than blocking market data.
   - Process S grants short-lived risk leases; T validates them before strategy work and again
     inside the transport adapter immediately before dispatch, with a measured safety margin.
     Expiry stops new exposure and triggers reconciliation, not blind liquidation. Local hard
     limits remain enforceable without S.
   - Protocol v1 uses canonical integer `e6` price/share/USD units, `__int128` intermediates in
     C++, explicit directional rounding, and no float at the execution boundary. C++/Python golden
     vectors are required before sidecar integration.
   - When capital/risk gates already forbid entry, production records only a compact raw signal/BBO
     rejection marker. Predicted EV, Kelly, and foregone-EV attribution are reconstructed offline;
     production must not run the full model merely to observe an unactionable opportunity.
   - Capital reservations require `orphaned` and `reconciled` transitions. An ambiguous timeout
     remains frozen until an idempotent REST position/open-order/trade reconciliation resolves it;
     `holding_time_p90` is research metadata, not an execution-finality timeout.
   - Python can honestly record local monotonic user-space spans and end-to-end RTT only.
     `socket.send()` return is not named kernel/NIC handoff, and exchange wall-clock match time is
     not subtracted from local monotonic time. Kernel timestamps/eBPF remain optional later
     instrumentation, not a prerequisite for the first recorder.
   - Python full suite after the scope cleanup: **734 passed**. TypeScript and fresh-migration
     verification are still pending at the time of this entry. No AWS deployment, DB mutation,
     `.env`, key, credential, live-mode, trader-roster, or strategy change was made.

### 2026-08-06 — Pre-parse timing and deferred shadow materialization

1. **When:** 2026-08-06 01:35–02:00 HKT.
2. **Files adjusted:**
   - `polymarket_data_api.py` and `test_polymarket_data_api.py` — timestamp the completed raw HTTP
     body before UTF-8/`json.loads`, retain parse-start/parse-complete/body-size metadata, separate
     ingress age from post-normalization queue age, and defer raw-record canonicalization.
   - `polymarket_simulator.py` and `test_polymarket_simulator.py` — add the same opt-in pre-parse
     timing boundary to the CLOB order-book response used by shadow decisions.
   - `passive_integrity.py` and `test_passive_integrity.py` — include signal JSON parse duration,
     book parse duration, and full ingress-to-decision age; a simulated 200 ms GIL parse stall is
     now visible to the BUY interlock even when post-normalization queue age is only 15 ms.
   - `shadow_capture.py`, `shadow_replay.py`, `bot.py`, `test_shadow_capture.py`,
     `test_shadow_replay.py`, and `test_bot_passive_shadow.py` — enqueue a lightweight capture
     capsule and defer Decimal VWAP, canonical JSON, `asdict`, and `EventEnvelope` construction to
     the writer rather than doing them synchronously before the BUY gate.
   - `docs/research/SHADOW_REPLAY_ARCHITECTURE_2026-08-06.md`, `docs/TODO_2026-08-06.md`, and this
     handoff — record the corrected measurement boundary and remaining GIL limitation.
3. **What changed:**
   - Timing capture is opt-in and is passed by `bot.fetch_direct_feed()` only when the recorder or
     entry interlock feature is enabled. With both default-off flags disabled, the normal wallet
     normalization path does not copy raw records or collect parse checkpoints.
   - The earliest application-space timestamp is immediately after `response.read()` and before
     decode. It does not claim kernel packet-arrival accuracy, but it can no longer hide Python
     deserialization time behind a post-parse enqueue timestamp.
   - The writer still uses a Python thread. Heavy work is no longer synchronous in the BUY call
     stack, but it still shares the GIL and therefore is **not** sufficient proof for a future
     per-WebSocket-message burst recorder. Public WS capture must use a separate process/host and
     real captured frames before activation.
   - Verification: focused observer/shadow/risk suites **390 passed**; full workspace suite
     **734 passed**; compile and `git diff --check` passed. No AWS deployment, feature activation,
     `.env`, credential, dependency, or private-key change was made.
   - Implementation commit `942824f` (`fix(shadow): measure pre-parse latency off hot path`) was
     pushed to the new GitHub `main`. AWS deployment remains intentionally blocked pending the
     real-corpus, external-injector, and process-isolation gates above.

### 2026-08-06 — Passive signal capture, observer-safe integrity checks, and panic protocol

1. **When:** 2026-08-06 00:45–01:27 HKT.
2. **Files adjusted:**
   - `polymarket_data_api.py`, `polymarket_simulator.py`, and their tests — stamp source rows and
     REST books once at receipt with wall and monotonic clocks, retain canonicalized source input,
     and pass those values forward without adding a polling task.
   - `passive_integrity.py`, `entry_interlock.py`, and their tests — calculate queue age and book
     freshness only when a BUY decision is already being evaluated; restore malformed persisted
     interlock state fail-closed after restart.
   - `shadow_capture.py`, `shadow_replay.py`, and their tests — turn the real polling signal and the
     same already-fetched depth book into a replayable shadow event, including the actual continuous
     Kelly copy size as one bounded extra executable-VWAP tier.
   - `bot.py`, `config.py`, `risk_manager.py`, `db.py`, `test_bot_passive_shadow.py`,
     `test_bot_risk_checks.py`, `test_db_pending_execution.py`, and `test_risk_manager.py` — wire the
     opt-in recorder into the real polling BUY path, connect its passive samples to the optional
     entry interlock, validate local portfolio invariants, and add a persistent panic path.
   - `docs/research/SHADOW_REPLAY_ARCHITECTURE_2026-08-06.md`, `docs/TODO_2026-08-06.md`, and this
     handoff — record the exact completed boundary, production safety decision, and next evidence
     gate.
3. **What changed:**
   - There is no 10 ms watchdog, `asyncio.sleep()` sampler, active queue scan, or duplicate book
     request. Queue age is `decision_monotonic - signal_enqueued_monotonic`; book age combines
     server age at local receipt with monotonic local residence. The writer thread blocks on its
     queue when idle and the producer uses non-blocking submission.
   - Malformed optional execution-integrity state remains a recoverable BUY interlock. Malformed
     core position/kill-switch/equity-HWM state latches the persistent hard kill, invalidates every
     delayed BUY intent, preserves SELL exits, and sends a CRITICAL Telegram alert. The current
     FAK/market BUY path leaves no managed resting entry orders, so a dangerous cancel-all is not
     used; the alert requires manual venue position/order reconciliation.
   - `ENABLE_PASSIVE_SHADOW_RECORDER` and `ENABLE_PASSIVE_ENTRY_INTERLOCK` both default false.
     Disabled production keeps the old book-depth hot path; enabling capture reuses the exact one
     REST book already fetched for sizing. The shadow module has no order/key/network dependency.
   - Git remotes on both the developer workspace and AWS were changed from the redirecting old URL
     to `https://github.com/trytobecomenice/sndncnodc.git`; both resolved HEAD
     `4e6cd276522344bd0332bd42124d2db6a133f33c` at verification.
   - AWS read-only preflight found 53 open normal positions and zero invalid shares, cost bases, or
     entry prices; no pending BUY intents, no pending exits, no active kill/interlock row, and a
     numeric equity HWM. No AWS process, DB row, environment setting, or deployment was changed.
   - Verification: focused suite **471 passed**; full workspace suite **727 passed**; compile and
     `git diff --check` passed. `.env`, keys, and unrelated dirty-worktree files remain excluded.
   - Implementation commit `d5ae7a4` (`feat(shadow): wire passive capture and panic interlock`)
     was pushed to the new GitHub `main`. Post-push verification resolved GitHub HEAD to
     `d5ae7a40a31efce257e63b8c291d0bbce65a5cd8`; AWS intentionally remains on `328a22d` with
     `data/autodeploy.lock` present and one paper bot PID `83881`.

### 2026-08-06 — Phase A shadow journal/replay and BUY interlock foundation

1. **When:** 2026-08-06 HKT, after the architecture decision below.
2. **Files adjusted:**
   - `entry_interlock.py` and `test_entry_interlock.py` — added a pure execution-integrity state
     machine that trips on one bad sample and requires both consecutive healthy samples and a
     recovery window before reopening entries.
   - `shadow_replay.py` and `test_shadow_replay.py` — added the v1 language-neutral envelope,
     exact raw signal text, integer BBO/top-three and `$3/$5/$10` VWAP, four named checkpoints,
     bounded non-blocking JSONL writing, virtual time, and deterministic decision digests.
   - `risk_manager.py`, `bot.py`, `db.py`, and `test_risk_manager.py` — connected a persisted
     `entry_interlock` value to the sole BUY gate, Prometheus/startup visibility, and skip-decision
     journal while keeping SELL/reduce-only paths open.
   - `polymarket_simulator.py` and `test_polymarket_simulator.py` — retained CLOB book timestamp
     and hash in the existing public REST adapter for later attribution.
   - `docs/research/SHADOW_REPLAY_ARCHITECTURE_2026-08-06.md`, `docs/TODO_2026-08-06.md`, and this
     handoff — recorded exact implementation status and remaining Phase B work.
3. **What changed:**
   - The recorder producer path is bounded: submission never waits for disk; queue drops and write
     errors are counted and mark the minimum audit trail unavailable instead of failing silently.
   - Replay consumes receive order with a monotonic virtual clock and rejects time regression; the
     same journal produces the same decision digest.
   - The shadow policy can only return `shadow_buy`/`skip`; it has no execution dependency or key
     access and cannot submit an order.
   - Verification: **704 Python tests passed** in the full workspace, including malformed metric,
     stale/missing book, queue-overflow, writer-loss, JSON round-trip, time-regression, deterministic
     digest, BUY-gate, decision-journal, and direct REST metadata regressions.
   - This is a foundation, not a live watchdog. Event-loop/queue/book-freshness samplers, numeric
     copy-alpha attribution, decoder benchmarks, public WS shadow capture, and 3-7 day evidence
     collection remain pending.
   - AWS production remains on `328a22d`; `data/autodeploy.lock` was verified present and watchdog
     active before push. No `.env`, key, AWS resize, live recorder, or production deployment was
     made.

### 2026-08-06 — Lean shadow recorder / replay architecture decision

1. **When:** 2026-08-06 HKT.
2. **Files adjusted:**
   - `docs/research/SHADOW_REPLAY_ARCHITECTURE_2026-08-06.md` — recorded the bounded recorder,
     compact copy-alpha schema, self-inflicted latency instrumentation, shadow-first rollout, and
     evidence-gated AWS/C++ path.
   - `docs/TODO_2026-08-06.md` and this handoff — added the implementation/evidence checklist.
3. **What changed:**
   - Verified production is an AWS `t3.small` with 2 vCPU, 1.9 GiB RAM, no swap, 910 MiB
     available at the sample, and a roughly 96 MiB RSS / 3.2% CPU Copy Bot process. Quiet-time
     load was low, but CPU credits and burst-time event-loop lag remain unmeasured tail risks.
   - Rejected continuous all-market full-depth Python capture on the current host. The first
     design is signal-scoped and fixed-cost: BBO/top three, `$3/$5/$10` VWAP/depth, bounded
     pre/post-signal windows, asynchronous bounded writing, and an explicit degradation ladder.
   - Moved copy-alpha data requirements and one minimal replay/attribution walking skeleton into
     the first phase, then placed 3-7 days of real shadow collection before broader replay work.
   - Preserved a future C++ hot-path boundary and AWS upgrade path without assuming that a
     language rewrite, bare metal, or kernel bypass creates edge before profiling proves the
     bottleneck.
   - Refined `CRITICAL` into a recoverable, hysteretic entry interlock for execution-integrity
     failures versus the existing persistent capital/invariant hard kill. Optional recorder
     backpressure degrades capture first; stale/late/unauditable execution blocks every new BUY
     while keeping exits available.
   - Added benchmark gates for stdlib `json` versus `orjson`/typed decoding rather than assuming a
     drop-in speed claim, and expanded executable VWAP capture to source-pre-trade, signal-visible,
     decision-commit, and execution checkpoints for `$3/$5/$10`.
   - Documentation/research only: no recorder, AWS resize, C++ service, strategy change, key,
     `.env`, or production deployment was made.

### 2026-08-05 — Blank `last_trade_price` production hotfix

1. **When:** 2026-08-05 15:16–15:38 HKT.
2. **Files adjusted:**
   - `polymarket_simulator.py` — normalized an optional blank/malformed/non-finite
     `last_trade_price` to `None` without discarding a valid bid/ask book.
   - `test_polymarket_simulator.py` — added the exact live empty-string regression plus
     whitespace, malformed, NaN, and infinity coverage.
   - `CLAUDE_HANDOFF.md`, `docs/TODO_2026-08-06.md`, and
     `docs/copy-trading/RISK_MANAGEMENT.md` — recorded cause, isolated deployment, and follow-up.
3. **What changed:**
   - Confirmed against a raw affected CLOB response that `timestamp`, bids, and asks were valid;
     only optional `last_trade_price` was `""`.
   - Built production-only commit `328a22d` from the prior live commit `736d3e0`, so the urgent
     deploy did not activate the larger quant-controls checkpoint.
   - Focused simulator tests: **42 passed** locally and on AWS. Normal-workspace full Python suite:
     **676 passed**.
   - AWS fast-forwarded `736d3e0 -> 328a22d`; paper bot restarted cleanly from PID `75742` to
     single PID `83881`. Two sweeps refreshed 53/53 marks at 15:30:39 and 15:36:03 HKT, with
     zero new errors and zero recurrence of the empty-price exception.
   - `.env`, credentials, EC2's intentional dirty `config.py` overrides, and unrelated local
     worktree changes were not committed or overwritten.

### 2026-08-05 — Quant controls 1–4 implementation checkpoint

1. **When:** 2026-08-05 04:51–05:20 HKT.
2. **Files adjusted:**
   - `bot.py`, `risk_manager.py`, `db.py`, `config.py` — startup circuit-breaker audit, clean
     evaluation epoch, five-minute PnL snapshots, drawdown warning gate, dynamic roster routing,
     and isolated challenger/retiring execution modes.
   - `manage_challengers.py`, `send_wallet_approvals.py`, `packages/db/src/schema.ts` — clean
     shadow evidence, Telegram one-in-one-out proposal detail, and lifecycle documentation.
   - `test_quant_p0.py`, `test_db_quant_p0.py`, three existing Python test files — regression
     coverage for zero variance, hysteresis, epoch persistence, isolated ledgers, and atomic swaps.
   - `docs/copy-trading/RISK_MANAGEMENT.md`, `docs/TODO_2026-08-06.md`, and this handoff.
3. **What changed:**
   - Corrected the exact strict-5 zero-variance failure and added startup re-evaluation.
   - Added immutable clean-cohort reporting without deleting/reclassifying historical PnL.
   - Added a BUY-only 75% drawdown soft pause with 60% recovery hysteresis; hard kill unchanged.
   - Added paper-only challengers requiring 7 days, 20 closes, and positive lower confidence bound
     before Telegram can approve a transactional one-in-one-out replacement.
   - Kept `$3–$10` trade sizing unchanged, per Joey's instruction.
   - Local verification at checkpoint: `python3 -m unittest discover` — **674 passed**; Python
     compile checks and the package TypeScript checks also passed.
   - **Not deployed to AWS yet**: follow `docs/TODO_2026-08-06.md`; EC2 remains paper-only on the
     previous production process until that controlled preflight is complete.
   - Before the GitHub push, EC2 `data/autodeploy.lock` was intentionally installed so the push
     cannot replace the running production process overnight. Remove it only inside tomorrow's
     controlled deployment window.
   - `.env`, keys, credentials, and unrelated dirty-worktree files were not staged.

### 2026-08-05 — Copy Bot repository separation

1. **When:** 2026-08-05 HKT.
2. **Files adjusted:**
   - Removed the secondary-bot package and its dedicated documentation from this repository.
   - `packages/db/src/schema.ts` and the generated forward migration — removed the obsolete
     secondary-domain table definitions; the destructive SQL was not applied to AWS.
   - `pnpm-lock.yaml`, `README.md`, `.claudeprompt`, `ROADMAP.md`,
     `apps/dashboard/next.config.ts`, two Copy Bot research comments, `docs/CURRENT_STATE.md`, and
     `CLAUDE_HANDOFF.md` — made repository scope Copy-Bot-only.
3. **What changed:**
   - Upgraded isolation from separate processes to separate source, dependencies, docs, schema,
     database ownership, and deployment scope.
   - Preserved historical Drizzle migrations because rewriting applied migration history would
     break existing journals; the latest forward migration removes the obsolete tables for
     databases where it is deliberately applied.
   - Did not change the separately maintained bot workspace or any of its code, DB, logs,
     scheduler, credentials, or runtime.
   - Verification: Python `unittest discover` 662/662; Copy Trading Vitest 259/259; Copy Trading
     and DB `tsc --noEmit` clean; migration SQL parsed successfully in an in-memory SQLite DB;
     dashboard ESLint clean. Dashboard production build reached compilation but could not fetch
     Google Geist fonts because the sandbox has no Google Fonts network access.
   - Did not stage `.env`, keys, credentials, or unrelated dirty-worktree files.

### 2026-08-05 — External GitHub strategy audit (research only)

1. **When:** 2026-08-05 HKT.
2. **Files adjusted:**
   - `docs/research/GITHUB_POLYMARKET_REPO_AUDIT_2026-08-05.md` — created.
   - `CLAUDE_HANDOFF.md` — added this handoff entry.
3. **What changed:**
   - Source-audited exact commits from `Drakkar-Software/OctoBot-Prediction-Market`,
     `Anmoldureha/polymarket-trading-bot-strategies`, and
     `llogiq33/Polymarket-Copy-Trade`.
   - Found that OctoBot's repository is a GPL wrapper whose advertised profile-follow/arbitrage
     features are still documented as in progress; llogiq33 contains only a README; and the
     PolyHFT-style repo has no license plus material execution/test weaknesses.
   - Imported no external source code and made no bot, AWS, DB, roster, `.env`, key or strategy
     change. Proposed only a future clean-room, shadow-only arbitrage research path.
   - External narrow tests: `python3 -m pytest tests/test_single_arbitrage.py
     test_new_strategies.py -q` — **3 passed, 4 warnings**; these tests cover detection, not safe
     execution.

### 2026-08-05 03:11 HKT — Codex P0 audit and Claude handoff

1. **When:** 2026-08-05 02:40-03:11 HKT.
2. **Files adjusted:**
   - `CLAUDE_HANDOFF.md` — created.
   - `docs/CURRENT_STATE.md` — added a current production correction pointing here.
   - EC2 `data/watchdog_paused` — created temporarily for the controlled restart, then removed.
   - No trading data, schema, strategy parameter, wallet status, or position was manually edited.
3. **What changed:**
   - Added a zero-context handoff with live AWS truth and explicit local-vs-production DB guidance.
   - Verified P0 rather than acting on stale laptop data.
   - Gracefully restarted the paper bot and confirmed a single new process.
   - Verified the first post-restart TTP/equity cycle: 56/56 positions marked, equity `$1,798.53`,
     kill switch inactive, zero errors.
   - Tests before the restart: `python3 -m unittest discover` — **658 passed** locally.
   - Commit / GitHub / final AWS deploy: pending at the time this entry was created; update after
     post-restart verification.

### 2026-08-05 03:26 HKT — P0 replay root cause, containment, and code fix

1. **When:** 2026-08-05 03:11-03:26 HKT; local fix/tests continued after containment.
2. **Files adjusted:**
   - `polymarket_data_api.py` — added an optional known-trade pagination boundary and one shared
     raw-record trade-ID helper.
   - `bot.py` — passes the running dedup set into both bootstrap and normal direct-feed polls.
   - `config.py` — raised per-wallet dedup retention from 100 to 500.
   - `test_polymarket_data_api.py` — added boundary-stop and forwarding regression tests.
   - `test_bot_risk_checks.py` — verifies `fetch_direct_feed()` forwards the exact dedup set.
   - `test_db_seen_trade.py` — pins dedup capacity >= the largest paginated production poll.
   - `docs/copy-trading/RISK_MANAGEMENT.md` — added Rule 50 root cause/trading impact/design.
   - `docs/copy-trading/SAFETY.md` — added Sec.69 containment and restart gate.
   - `CLAUDE_HANDOFF.md` — recorded this live finding and the mandatory change log.
   - `docs/CURRENT_STATE.md` — production correction/handoff pointer updated for the P0 state.
   - EC2 `data/watchdog_paused` — created; must remain until post-deploy restart verification.
3. **What changed:**
   - Stopped a 200-row feed from endlessly rotating through a 100-ID per-wallet dedup window.
   - Preserved multi-page coverage for a genuine >20-trade burst; research/backfill pagination is
     unchanged.
   - Contained active paper-ledger pollution by gracefully stopping only the Copy Bot.
   - Targeted tests: `python3 -m unittest test_polymarket_data_api test_db_seen_trade
     test_bot_risk_checks` — **310 passed**.
   - Full tests: `python3 -m unittest discover` — **662 passed**.
   - P0 implementation commit: `93c193b`; pushed to GitHub `main` and deployed to AWS.

### 2026-08-05 03:50 HKT — P0 deployment and live restart gate

1. **When:** 2026-08-05 03:38-03:50 HKT.
2. **Files adjusted:**
   - `CLAUDE_HANDOFF.md` — changed P0 from pending to live-verified and recorded final metrics.
   - `docs/copy-trading/RISK_MANAGEMENT.md` — recorded the deployment outcome and dust-event
     contamination boundary.
   - `docs/copy-trading/SAFETY.md` — recorded the passed restart gate.
   - EC2 `config.py` — no new strategy change; the existing DB-source and OMS overrides were
     temporarily stashed, then restored unchanged around the fast-forward pull.
   - EC2 `data/watchdog_paused` and `data/autodeploy.lock` — temporary deployment controls,
     removed after verification.
3. **What changed:**
   - Pushed and deployed `93c193b` without committing `.env`, `.env.example`, keys, or unrelated
     dirty-worktree files.
   - AWS tests: `python3 -m unittest discover -p 'test_*.py'` — **655 passed**.
   - Restarted one paper-mode process (PID `75742`) with DB roster, OMS mirror, and dedup cap 500.
   - Verified TTP at 03:45:13 and 03:50:38 HKT, equity `$1,801.36`, 56 positions, kill switch off,
     and zero post-start errors.
   - Replay churn stopped after one bounded catch-up. Its 2 dust sells had `$0.0133` cost basis;
     all 18 replay sells across both restarts had `$0.0882` combined cost basis and `$0.00`
     rounded PnL. They were left auditable, not manually hidden.
