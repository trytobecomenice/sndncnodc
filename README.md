# Polymarket Copybot

**What this is:** an automated system that watches a hand-picked list of profitable Polymarket
traders, and copies their trades — paper (simulated) money only, right now — while enforcing a
stack of independent risk controls (spread checks, exposure caps, a drawdown kill switch, a
pre-trade slippage ceiling) so that a future move to real money is something reviewed and opted
into, not something that happens by accident.

If you're new here and have zero context on the project, this file is the front door. Read it
top to bottom, then follow the links.

## Is this safe to poke around in?

Yes. As of today, `config.LIVE_MODE = False` — every trade is simulated, no real funds move, and
no code in this repository ever touches a private key (see `docs/SAFETY.md` §5). The riskiest
thing you can do by accident is edit `config.py`'s tuning numbers or flip `LIVE_MODE` without
understanding what you're changing — see `docs/RISK_MANAGEMENT.md` before touching either.

## Where to start

| I want to... | Read this |
|---|---|
| Understand what's running and why, from zero | `docs/SYSTEM_ARCHITECTURE.md` |
| Know exactly what risk rules are enforced, and why | `docs/RISK_MANAGEMENT.md` |
| Get the engineering-level detail (file/function names, DB ownership, runbooks) | `docs/SAFETY.md` |
| Get oriented as a new operator/contributor | `docs/OPERATOR_ONBOARDING.md` |
| See what's done, what's in flight, and what's explicitly on hold | `docs/CURRENT_STATE.md` |

## The two systems in this repo

1. **Copy Bot** (built, running today) — Python (`bot.py`, `config.py`, `db.py`,
   `bullpen_client.py`, `risk_manager.py`) plus a TypeScript "research brain"
   (`packages/copy-trading`) that scores candidate wallets. This is the system every doc above
   describes.
2. **Weather Bot** (planned, not started) — a separate, intentionally isolated arbitrage bot
   that will eventually share this repo's dashboard and database, but has no logic yet
   (`packages/weather/` is an empty scaffold). Never mix its logic with the Copy Bot's.

## Running it

- **The trading loop:** `python3 -u bot.py` (needs a logged-in `bullpen` CLI session — run
  `bullpen status` first). It polls for new trades from the tracked wallets every 30 seconds; see
  `docs/SYSTEM_ARCHITECTURE.md` §4 for the full cycle.
- **The built-in dashboard** (quick, no build step): `python3 dashboard.py`, then open
  `http://localhost:8787`.
- **The Next.js dashboard** (in progress, Overview page only): `pnpm dev` inside
  `apps/dashboard/` — see `apps/dashboard/README.md`.
- **The wallet-research scripts** (manual, one-off, TypeScript): `pnpm scan:leaderboard` then
  `pnpm scan:wallets` from the repo root — see `docs/SYSTEM_ARCHITECTURE.md` §4C for what each
  does. These currently only *score* wallets; the bot doesn't trade off their output yet
  (`docs/RISK_MANAGEMENT.md` Rule 2).

## Repo layout at a glance

```
polymarket-copybot/
├── bot.py, config.py, db.py, bullpen_client.py, risk_manager.py   ← Python trading bot
├── dashboard.py                                                    ← built-in dashboard (port 8787)
├── data/app.db                                                     ← shared SQLite DB (gitignored)
├── docs/                                                           ← all documentation (see table above)
├── apps/dashboard/                                                 ← Next.js dashboard (in progress)
└── packages/
    ├── db/            ← the database schema (Drizzle/TypeScript) — the one place table shape is defined
    ├── bullpen-client/ ← TypeScript twin of bullpen_client.py
    ├── copy-trading/   ← wallet-scanning/scoring research scripts
    ├── weather/        ← empty scaffold, not built
    └── shared/         ← empty scaffold, not built
```

See `docs/SYSTEM_ARCHITECTURE.md` §2 for why the repo is split this way (`apps/` vs. `packages/`,
and why Python and TypeScript both write to the exact same database file).
