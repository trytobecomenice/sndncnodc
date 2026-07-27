#!/usr/bin/env python3
"""
Token Registry sync worker — populates `token_registry` (see
packages/db/src/schema.ts) by fetching Polymarket's active markets from the
Gamma API, so wss_listener.py's on-chain `token_id`s (see `live_whale_event`)
can be resolved to a `market_slug`/`outcome` locally, with zero added
request latency, instead of a live API call per detected trade.

ENDPOINT — deliberately NOT what was originally asked for. `GET
/markets?active=true` (offset/limit pagination) is DEPRECATED: a live check
of that endpoint's response headers today (2026-07-24) shows
`deprecation: true`, `sunset: Fri, 01 May 2026` (already past), and
`warning: 299 - "use /markets/keyset"`. It still technically responds, but
building new code against a past-sunset endpoint in 2026-07 would be bad
advice — this script uses `GET /markets/keyset` instead, confirmed live to
have none of those deprecation headers.

PAGINATION — cursor-based, not offset/limit, and the correct query
parameter took several live guesses to find (`cursor` and `after` are both
silently ignored — the response just repeats page 1 — the response body
itself doesn't document the request param at all). The correct parameter,
confirmed working across 3 consecutive live pages while building this, is
`after_cursor`, taking the PREVIOUS response's `next_cursor` value. Note for
whoever runs this: a GitHub issue (Polymarket/agents#227) reports this exact
cursor being broken/ignored server-side for some users — it worked
correctly in my own live testing today, but if pages start silently
repeating, that's a known, previously-reported failure mode on Polymarket's
end, not a bug in this script's request-building.

FIELD SHAPES — verified live, not assumed: `outcomes` and `clobTokenIds` in
each market object are both JSON-STRINGIFIED arrays (e.g.
'["Yes", "No"]', not a real array), parallel-indexed (outcomes[i] pairs
with clobTokenIds[i]). A market missing/null clobTokenIds (seen on some
non-orderbook markets) is skipped, not force-inserted with a placeholder.

RATE LIMITING: a small fixed delay between pages (REQUEST_DELAY_SECONDS) —
simple and sufficient for a periodic sync job, not a full token-bucket
limiter. A page whose request fails is retried a bounded number of times
with backoff before the worker gives up on that page and logs an error
(never crashes the whole run over one bad page).

SCHEDULING (2026-07-25): `main()` is a long-running daemon — runs a full
sync, sleeps `SYNC_INTERVAL_SECONDS` (15 min default, env-overridable), and
repeats forever, matching wss_listener.py's own persistent-process
structure rather than being a one-shot script scheduled externally (this
repo has no existing cron/launchd setup for its own processes to plug a
one-shot script into). One sync cycle raising is logged and followed by the
next scheduled run, not a crash.
"""

import asyncio
import json
import logging
import os
import sqlite3
import time

import aiohttp

import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] token_sync_worker: %(message)s",
)
logger = logging.getLogger("token_sync_worker")

GAMMA_KEYSET_URL = "https://gamma-api.polymarket.com/markets/keyset"
PAGE_LIMIT = 100  # server silently clamps anything higher to 100 — verified live, not just documented
REQUEST_DELAY_SECONDS = 0.25
MAX_PAGE_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2.0
REQUEST_TIMEOUT_SECONDS = 30

# 2026-07-25: turned into a long-running daemon (matching wss_listener.py's
# own structure) rather than a one-shot script scheduled externally via
# cron/launchd — this repo has no existing cron/launchd setup for bot.py's
# own process that a one-shot script could plug into, so a self-scheduling
# loop avoids depending on infra this session couldn't verify exists.
# 15 minutes: explicit balance Joey specified between token_registry
# freshness (a full sync takes tens of seconds to a few minutes depending
# on active-market count, based on the ~100-market-per-~1-2s pace observed
# live while building this) and Gamma API load — not derived from a
# documented rate limit (none was found for this endpoint).
SYNC_INTERVAL_SECONDS = int(os.environ.get("SYNC_INTERVAL_SECONDS", str(15 * 60)))


def _connect():
    conn = sqlite3.connect(config.SQLITE_PATH)
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def upsert_token_registry_rows(rows):
    """rows: list of (token_id, market_slug, outcome) tuples. One
    transaction per page — a page's rows all land together or (on a DB
    error) none do, rather than a partial page silently applying."""
    if not rows:
        return
    conn = _connect()
    try:
        now = int(time.time())
        conn.executemany(
            "INSERT INTO token_registry (token_id, market_slug, outcome, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(token_id) DO UPDATE SET "
            "market_slug = excluded.market_slug, outcome = excluded.outcome, "
            "updated_at = excluded.updated_at",
            [(token_id, market_slug, outcome, now) for token_id, market_slug, outcome in rows],
        )
        conn.commit()
    finally:
        conn.close()


def extract_token_rows(market):
    """One Polymarket market -> zero or more (token_id, market_slug,
    outcome) rows, one per outcome. Returns [] (not an exception) for a
    market missing/malformed clobTokenIds/outcomes — a single bad market
    must never abort the whole sync."""
    slug = market.get("slug")
    outcomes_raw = market.get("outcomes")
    token_ids_raw = market.get("clobTokenIds")
    if not slug or not outcomes_raw or not token_ids_raw:
        return []

    try:
        outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
        token_ids = json.loads(token_ids_raw) if isinstance(token_ids_raw, str) else token_ids_raw
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning(f"market {slug!r}: could not parse outcomes/clobTokenIds ({e}), skipping")
        return []

    if len(outcomes) != len(token_ids):
        logger.warning(
            f"market {slug!r}: outcomes/clobTokenIds length mismatch "
            f"({len(outcomes)} vs {len(token_ids)}), skipping"
        )
        return []

    # token_id kept as a STRING throughout — never cast to int — matching
    # token_registry.token_id's schema (precision loss risk on a uint256).
    return [(str(token_id), slug, outcome) for outcome, token_id in zip(outcomes, token_ids)]


async def _fetch_page(session, after_cursor):
    params = {"active": "true", "limit": str(PAGE_LIMIT)}
    if after_cursor:
        params["after_cursor"] = after_cursor

    last_error = None
    for attempt in range(1, MAX_PAGE_RETRIES + 1):
        try:
            async with session.get(
                GAMMA_KEYSET_URL, params=params,
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS),
            ) as response:
                response.raise_for_status()
                return await response.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            last_error = e
            logger.warning(f"page fetch attempt {attempt}/{MAX_PAGE_RETRIES} failed: {e}")
            if attempt < MAX_PAGE_RETRIES:
                await asyncio.sleep(RETRY_BACKOFF_SECONDS * attempt)
    raise RuntimeError(f"page fetch failed after {MAX_PAGE_RETRIES} attempts: {last_error}")


async def sync_all_active_markets():
    """Pages through every active market via cursor-based pagination,
    upserting each page's extracted (token_id, market_slug, outcome) rows
    as it goes (not batched to the very end — a mid-run failure still keeps
    everything synced so far). Returns (markets_seen, rows_upserted,
    pages_failed).
    """
    markets_seen = 0
    rows_upserted = 0
    pages_failed = 0
    after_cursor = None

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                page = await _fetch_page(session, after_cursor)
            except RuntimeError as e:
                logger.error(f"giving up on a page, sync incomplete: {e}")
                pages_failed += 1
                break

            markets = page.get("markets") or []
            if not markets:
                break

            rows = []
            for market in markets:
                rows.extend(extract_token_rows(market))
            upsert_token_registry_rows(rows)

            markets_seen += len(markets)
            rows_upserted += len(rows)
            logger.info(
                f"page done: {len(markets)} market(s), {len(rows)} token row(s) "
                f"(running total: {markets_seen} markets, {rows_upserted} rows)"
            )

            after_cursor = page.get("next_cursor")
            if not after_cursor:
                break  # last page, per Gamma's documented keyset convention

            await asyncio.sleep(REQUEST_DELAY_SECONDS)

    return markets_seen, rows_upserted, pages_failed


async def run_one_sync():
    started = time.time()
    logger.info(f"starting sync against {GAMMA_KEYSET_URL}")
    markets_seen, rows_upserted, pages_failed = await sync_all_active_markets()
    elapsed = time.time() - started
    logger.info(
        f"sync complete in {elapsed:.1f}s: {markets_seen} market(s) seen, "
        f"{rows_upserted} token_registry row(s) upserted, {pages_failed} page(s) failed"
    )
    if pages_failed:
        logger.warning(
            "one or more pages failed — the registry is now partially stale/incomplete; "
            "re-run to retry (upserts are idempotent, safe to re-run in full)"
        )


async def main():
    """Long-running daemon (2026-07-25) — runs run_one_sync() every
    SYNC_INTERVAL_SECONDS (15 min default), forever, matching
    wss_listener.py's own persistent-process structure. A single sync
    raising (a bad/unexpected exception `sync_all_active_markets()` itself
    didn't already catch, e.g. a DB-level failure) is logged and followed
    by the next scheduled run rather than crashing the daemon — the whole
    point of running this unattended is that one bad cycle shouldn't need
    a human to notice and restart it.
    """
    while True:
        try:
            await run_one_sync()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"sync cycle failed unexpectedly: {e}", exc_info=True)
        logger.info(f"next sync in {SYNC_INTERVAL_SECONDS}s")
        await asyncio.sleep(SYNC_INTERVAL_SECONDS)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("token_sync_worker stopped.")
