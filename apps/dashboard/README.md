# Copybot Dashboard (Next.js)

**Audience note:** zero prior context assumed. For the full system this dashboard is one piece
of, start at the repo root `README.md`, then `docs/SYSTEM_ARCHITECTURE.md`.

## What this is

The newer of two dashboards in this repo (the other is `dashboard.py`, a small built-in
Flask-style page on port 8787). This one is a proper Next.js app that reads live from the same
shared SQLite database (`data/app.db`) every other part of the system writes to — via Drizzle
(`@copybot/db`), not a copy or a cache. It's meant to eventually replace the built-in dashboard,
once it covers everything that one does.

**Status:** only the Overview page is built (PnL, open positions, tracked wallets, recent
activity, all live). The other planned pages — Wallet Rankings, Trade Signals, Decision Journal,
Performance, Rules, Reports, and more — don't exist yet. Its one write action (start/stop the
bot) never touches `data/app.db` directly — it only signals a PID file, matching the
"dashboard stays near-read-only" boundary documented in `docs/SAFETY.md` §2.

## Running it

From the repo root (this is part of a pnpm monorepo — one `pnpm install` at the root installs
everything, including this app's dependencies):

```bash
pnpm install
cd apps/dashboard
pnpm dev
```

Open [http://localhost:3000](http://localhost:3000). It reads `data/app.db` directly, so the
data you see matches whatever `bot.py` and the scoring scripts have written — no separate setup
or seeding needed if the rest of the system has already run at least once.

## Where the data comes from

This app never talks to `bullpen` or Polymarket directly — it only reads the database that
`bot.py` (Python) and the wallet-scoring scripts (`packages/copy-trading`) write to. See
`docs/SYSTEM_ARCHITECTURE.md` §3 for exactly how a Python process and a TypeScript app safely
share one SQLite file, and §4 for the full trading/scoring cycle whose output this dashboard
displays.

## Learn more about Next.js itself

This app was bootstrapped with `create-next-app` and uses [`next/font`](https://nextjs.org/docs/app/building-your-application/optimizing/fonts)
to load the Geist font family. For framework-level questions unrelated to this project:
[Next.js Documentation](https://nextjs.org/docs) · [Learn Next.js](https://nextjs.org/learn).
