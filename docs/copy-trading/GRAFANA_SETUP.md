# Personal Grafana dashboard — setup

Read-only observability on the shared SQLite DB (`data/app.db`). Doesn't
touch bot.py, dashboard.py, or the Next.js dashboard — safe to stop or
remove at any time without affecting the trading system itself.

## 1. Start Grafana

```bash
docker compose -f docker-compose.grafana.yml up -d
```

Open `http://localhost:3001` (port 3001, not 3000 — that's already the
Next.js dashboard). First login is `admin`/`admin`; you'll be prompted to
change it.

## 2. Add the SQLite data source

Grafana has no built-in SQLite support — the compose file auto-installs
the community `frser-sqlite-datasource` plugin on first start.

1. **Connections → Data sources → Add data source → search "SQLite"**
2. **Path**: `/data/app.db` — this is the path *inside the container*
   (from the `./data:/data:ro` mount in `docker-compose.grafana.yml`), not
   your host machine's path.
3. **Save & test.**

The whole `data/` directory is mounted (not just `app.db`) because the DB
runs in WAL mode — recent writes can sit in `app.db-wal` until
checkpointed, and mounting only the main file would show Grafana a real
but stale view.

## 3. Panels

### Equity Curve & High Watermark

Line panel, two series (`total_equity`, `high_watermark`) against `time`:

```sql
SELECT
  datetime(snapshot_at, 'unixepoch') AS time,
  total_equity,
  MAX(total_equity) OVER (ORDER BY date ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS high_watermark
FROM daily_portfolio_snapshots
ORDER BY date;
```

### Sharpe & Win-Rate Tracker

Two separate panels, both against the *real* tables this project already
uses (not a generic "trades" table — there is no table by that name here).

**Win rate**, from `paper_trade` (the actual copy-trade ledger — filtered
to `strategy = 'bot_filtered'` to exclude the shadow-rehab/benchmark rows
that live in the same table):

```sql
SELECT
  date(closed_at, 'unixepoch') AS day,
  COUNT(*) AS trades_closed,
  SUM(CASE WHEN realized_pnl_usd > 0 THEN 1 ELSE 0 END) * 1.0 / COUNT(*) AS win_rate
FROM paper_trade
WHERE status = 'closed' AND strategy = 'bot_filtered' AND closed_at IS NOT NULL
GROUP BY day
ORDER BY day;
```

**Sharpe proxy**, deliberately the *same formula* `scoreWallets.ts`
already uses to score external wallets (`meanDelta / stdevDelta` over
portfolio-value deltas) — applied here to your own equity curve instead,
for methodological consistency rather than inventing a second definition
of "Sharpe" for the same system. Plain SQLite has no built-in `STDDEV`,
so the population-variance identity (`E[x²] − E[x]²`) is spelled out:

```sql
WITH daily_returns AS (
  SELECT
    date,
    total_equity - LAG(total_equity) OVER (ORDER BY date) AS daily_pnl
  FROM daily_portfolio_snapshots
),
stats AS (
  SELECT
    AVG(daily_pnl) AS mean_pnl,
    AVG(daily_pnl * daily_pnl) - AVG(daily_pnl) * AVG(daily_pnl) AS variance_pnl
  FROM daily_returns
  WHERE daily_pnl IS NOT NULL
)
SELECT
  mean_pnl,
  SQRT(variance_pnl) AS stdev_pnl,
  CASE WHEN SQRT(variance_pnl) > 0 THEN mean_pnl / SQRT(variance_pnl) ELSE 0 END AS sharpe_proxy
FROM stats;
```

Needs a handful of `daily_portfolio_snapshots` rows (i.e., a few days of
the bot running with the snapshot feature live) before this is meaningful
— a single day gives one delta, not a distribution.

### System Throttle & Tiering Efficiency

Three small panels — this is the one place worth checking *today's real
numbers* against, since the tiering system was just verified live:
first `scan:wallets` run after shipping saw 692/692 candidates due
(expected — the due-date column didn't exist before that run); the very
next run saw **10/692 due (98.6% reduction)**. These queries are how
you'd watch that number stay low going forward, not just a one-time check.

**Tier 1 (live-copied) count** — the real, ground-truth number, not an
approximation. `json_each` over the JSON object already works in this
DB (the same JSON1 extension `db.realized_pnl_total()` already relies on
via `json_extract`):

```sql
SELECT COUNT(*) AS tier1_count
FROM json_each(
  (SELECT value_json FROM bot_risk_state WHERE key = 'tracked_traders')
);
```

**Tier 2/3 distribution** (the scorer's own `status` column — Tier 1
isn't derivable from this alone, see above; a wallet manually kept in
Tier 1 despite a poor score would still show here under whatever `status`
the scorer gave it):

```sql
SELECT status, COUNT(*) AS wallet_count
FROM wallet_profile
GROUP BY status;
```

**Actual re-scoring volume over time** — the direct, honest measurement
of whether the self-throttle is working, not a guess:

```sql
SELECT
  datetime(last_scored_at, 'unixepoch', 'start of day') AS time,
  COUNT(*) AS wallets_rescored
FROM wallet_profile
WHERE last_scored_at IS NOT NULL
GROUP BY time
ORDER BY time;
```

A healthy pattern: one large spike the first time this ran (the bootstrap
cost, expected), then small, steady counts afterward — a sudden sustained
jump back up would mean something is forcing full re-scores again (a
`rule_set` version bump does this on purpose; anything else is worth
investigating).

## Notes

- Nothing here writes to the trading DB — the mount is `:ro`, and every
  query above is a `SELECT`.
- `daily_portfolio_snapshots` only gets a row once `bot.py`'s
  `maybe_snapshot_daily_portfolio()` fires (past 23:00 UTC, once per UTC
  day) — the equity-curve and Sharpe panels will be empty until the bot's
  been running past that trigger at least once.
