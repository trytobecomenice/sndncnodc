# Claude Handoff — Polymarket Copybot

**Purpose:** this is the short, current handoff for Claude (or any next agent). It does not replace
the rule ledgers. Read this file first, then use `docs/copy-trading/RISK_MANAGEMENT.md` and
`docs/copy-trading/SAFETY.md` for the full rationale and implementation history.

**Last live verification:** 2026-08-05 03:50 HKT / 2026-08-04 19:50 UTC.

## Non-negotiable operating rules

- Copy Bot and Weather Bot are separate systems. Never mix their code, runtime imports, tables,
  capital, PnL, or risk decisions.
- Both systems remain paper-only. Do not flip `LIVE_MODE` or introduce private-key custody.
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
- Git commit live: `93c193b` (`fix(copybot): stop paginated feed replay churn`).
- `LIVE_MODE=False`.
- EC2 `config.py` has two intentional-but-uncommitted overrides:
  - `TRACKED_TRADERS_SOURCE="db"`
  - `ENABLE_OMS_SHADOW_MIRROR=True`
- Do not run `git reset --hard` on EC2: it would erase those production overrides.
- `watchdog.py` cron is installed every 2 minutes.
- `autodeploy.py` cron is installed every 5 minutes.
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

- `bot_filtered` closed: **634**, cumulative realized PnL **+$639.74**.
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
- AWS showed zero `str`/`int` TTP type errors in the 24 hours before the P0 restart.
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
3. Decide how a newly Telegram-approved wallet triggers a safe roster reload; DB approval alone
   does not change the in-memory poll list.
4. Investigate why Shadow Rehab has grown the 4.6 GB DB so quickly and enforce retention/compaction
   without changing normal Copy Bot risk accounting.
5. Review concentration: historical PnL can still be dominated by a small number of wallets even
   when the roster count looks diversified.

## Change log

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
