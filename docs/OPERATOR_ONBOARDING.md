# Operator Onboarding Guide

**Audience note:** this assumes zero prior context. If you've never seen this repo before, this
is where to start — it tells you what order to read things in, what's currently true about the
running system, and what you're and aren't allowed to touch without review.

## 1. Read these, in this order

1. `README.md` (repo root) — the front door: what this project is, how the pieces fit, how to
   run each part.
2. `docs/SYSTEM_ARCHITECTURE.md` — the full mental model: what's built, how Python and
   TypeScript share one database, and the exact step-by-step trading cycle.
3. `docs/RISK_MANAGEMENT.md` — every automated risk rule currently enforced, in detail: what it
   does, exactly how, what it costs, and why it exists. **This is the single source of truth for
   "what rules currently apply"** — check it before proposing any new restriction or threshold
   change, so an old decision doesn't get silently re-thought.
4. `docs/SAFETY.md` — the engineering companion to the above: database ownership boundaries,
   migration/cutover runbooks, and known residual risks not fully closed by any single rule.
5. `docs/CURRENT_STATE.md` — a snapshot of what's done, in flight, or explicitly on hold
   right now (check the "Last reviewed" date at the top — if it's more than a few days old,
   treat it as a starting point to verify, not gospel).

## 2. What the system does today, in one paragraph

The Copy Bot (`bot.py`) watches a fixed list of 20 tracked Polymarket wallets (configured in
`config.py`, `TRACKED_TRADERS_SOURCE = "static"`) via the external `bullpen` CLI, and mirrors
their buys/sells as its own $5-per-trade paper (simulated) positions — no real money moves yet.
Every decision (copy or skip, and why) is logged to a shared SQLite database
(`data/app.db`) that a small built-in dashboard (`dashboard.py`, port 8787) and a newer Next.js
dashboard (`apps/dashboard`, in progress) both read from live. A separate TypeScript research
pipeline (`packages/copy-trading`) scans the Polymarket leaderboard and scores candidate wallets,
but the bot doesn't trade off that scoring yet — see `docs/RISK_MANAGEMENT.md` Rule 2.

## 3. What is restricted — the short version

(Full detail, numbers, and reasoning: `docs/RISK_MANAGEMENT.md`.)

- **No live execution.** `config.LIVE_MODE = False`. Flipping it is a deliberate, separately
  reviewed decision — never a side effect of another change.
- **No private-key handling in app code, ever.** All signing/custody lives in the external
  `bullpen` CLI. If a feature seems to need direct key access, it belongs in `bullpen`, not here.
- **No schema changes from Python.** `db.py` only does `SELECT`/`INSERT`/`UPDATE`/`DELETE`.
  Table shape changes go through TypeScript/Drizzle (`pnpm db:migrate`) only, with `bot.py`
  stopped first.
- **Portfolio exposure is capped** ($250 total, $30 per event) and a drawdown kill switch halts
  new buys automatically if equity falls too far — both gate BUYs only, never exits.
- **A wallet can be muted** (per-trader circuit breaker) or gated out of scoring entirely (toxic-
  flow / recency filters) — see `docs/RISK_MANAGEMENT.md` Rules 5 and 12.

## 4. How to think about changes

- Before changing a threshold or adding a restriction, check `docs/RISK_MANAGEMENT.md` first —
  it's the ledger of what's already been decided and why; don't silently re-litigate it.
- If you change behavior, files, schema, or workflow, update the relevant doc **in the same
  change** — `docs/RISK_MANAGEMENT.md` and `docs/SAFETY.md` must be updated together whenever a
  risk rule changes (see the note at the top of each). `docs/CURRENT_STATE.md` should reflect any
  new "done" or "in flight" item.
- If a feature is planned but not implemented yet, mark it clearly as planned (see
  `docs/RISK_MANAGEMENT.md`'s roadmap section for the pattern) so it's easy to find later and
  doesn't get assumed-done.

## 5. Suggested first-day checklist

1. Read the docs in the order listed in §1.
2. Confirm the bot is actually running: `ps -p $(cat bot.pid)`, and tail `bot.out.log` for
   recent activity.
3. Confirm `bullpen` auth is healthy: `bullpen status`.
4. Open `dashboard.py` (port 8787) or run `apps/dashboard`'s `pnpm dev` and look at real current
   state — open positions, recent events, tracked wallet count — rather than assuming from docs
   alone.
5. Avoid making schema changes, flipping `LIVE_MODE`, or changing `TRACKED_TRADERS_SOURCE` unless
   the process has been clearly reviewed with whoever owns the project.
