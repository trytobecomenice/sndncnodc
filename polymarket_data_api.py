#!/usr/bin/env python3
"""Direct Polymarket Data API client (2026-07-21) — replaces `bullpen tracker
feed` for WALLET TRACKING only. Trade EXECUTION (buy/sell) is unaffected and
stays on bullpen's signing path either way — self-custodying a key to also
remove bullpen from execution would contradict an existing project principle
(docs/copy-trading/SAFETY.md §6, "why private keys never belong in this app").

Verified live before writing any of this, not assumed from docs:
- data-api.polymarket.com requires NO authentication at all — confirmed via
  direct curl against both a random wallet and one of our own real tracked
  wallets (strict-1), both returned real trade data with a plain 200.
- Its /activity?type=TRADE server-side filter genuinely filters server-side
  (confirmed: a live call returned only {'TRADE'} in the type set).
- No bulk/multi-wallet query exists — both a comma-separated `user` value
  and a repeated `user=A&user=B` param were tested live; the former 400s,
  the latter silently keeps only the last value. Per-wallet requests are
  required; concurrency is the only available lever on request count.
- Its field names line up 1:1 with bullpen's own `tracker feed` trade shape
  — confirmed by comparing a live `bullpen tracker feed` call directly:
  bullpen's `transaction_hash` field is the exact same value Polymarket's
  API calls `transactionHash`.
- IMPORTANT, discovered during validation: bullpen's own `size_usd` is the
  PRE-FEE nominal trade value (shares * price) — Polymarket's own `usdcSize`
  is the actual on-chain settled amount, which includes Polymarket's real
  taker fee (makers pay zero). Confirmed exactly against docs.polymarket.com
  /trading/fees's documented formula, fee = shares * feeRate * price *
  (1-price): two independent real trades both back out to feeRate=0.05000,
  exactly matching the documented Sports category rate. This means
  `usdcSize` (used here) is the MORE accurate figure — it reflects what
  actually happened on-chain, not bullpen's pre-fee approximation. See
  docs/copy-trading/RISK_MANAGEMENT.md Rule 10 for the implication.
- No rate-limiting observed across an 8-request burst (a short test, not
  proof it never rate-limits at higher sustained volume — see the parallel
  validation script, which is meant to build confidence over a longer
  window before any live cutover).

RATE LIMITS & RETRIES (added 2026-07-22, reviewed against Polymarket's official docs, not
assumed): Polymarket's own docs (docs.polymarket.com/api-reference/core/get-user-activity) don't
publish a specific numbers-based rate limit for this endpoint at all. A credible third-party source
(not Polymarket's own docs) claims this API sits behind a Cloudflare-enforced, GLOBAL (cross-
account) limit of 1,000 requests/10s that throttles/queues before outright rejecting, and that an
eventual 429 carries no `Retry-After`/`X-RateLimit-*` headers — treated here as the worst-case
assumption to defend against, not a confirmed fact, since it's not in Polymarket's own
documentation. `fetch_wallet_trades` now retries HTTP 429 and 5xx (server-side, plausibly
transient) with exponential backoff (RATE_LIMIT_BACKOFF_BASE_SECONDS, doubling each attempt, up to
RATE_LIMIT_MAX_RETRIES) — since no Retry-After header can be trusted, this is a generic backoff,
not one driven by a server hint. Other 4xx (400, 404, etc — genuine client-side/permanent errors)
are NOT retried at all, since backing off and retrying an inherently-wrong request just wastes
time without changing the outcome.

PAGINATION (added 2026-07-22; live-poll boundary added 2026-08-05): Polymarket's own docs confirm
`/activity` supports `offset`-based pagination (0-500 per page via `limit`, offset 0-5000 —
"requests past the cap are rejected with a 400"; beyond 5000 total, docs say to page inside
additional `start`/`end` time windows, each with its own offset budget). `fetch_wallet_trades`
pages automatically while pages are full, up to `MAX_PAGES_PER_FETCH`. A caller may additionally
pass `known_trade_ids`: once a fetched page overlaps that already-processed boundary, pagination
stops after that page. `bot.py` uses this on every live poll, preserving multi-page burst capture
when a wallet really made 21+ new trades while avoiding a deep replay of the same old 200 rows on
every ordinary cycle. Research/backfill callers omit the boundary and retain the original full
pagination behavior.

SPEED: per-request latency is dominated by TLS/connection setup, not
request count or thread-pool size — confirmed live: bumping max_workers
from 10 to 30 barely moved total time (3.34s -> 3.68s, noise-level), while
reusing one HTTPS connection across repeated requests to the same host cut
each subsequent request from ~0.7-0.9s to ~0.2-0.4s. This module keeps one
persistent HTTPS connection PER WORKER THREAD (threading.local), which only
pays off across MULTIPLE poll cycles — callers that want the real benefit
must create ONE long-lived ThreadPoolExecutor (make_persistent_executor())
and pass it into every call via `executor=`, rather than letting a fresh
one be created (and its connections discarded) every cycle.
**Live-confirmed across 3 simulated poll cycles against all 20 real tracked
wallets**: a fresh executor every cycle held steady at ~2.3-2.4s/cycle
(every cycle pays the cold-connection cost); a persistent executor paid a
higher first-cycle cost (3.78s) then dropped to 0.40s and 0.46s on cycles 2
and 3 — meaningfully FASTER than bullpen's own single-call ~1.4s once
connections are warm. fetch_all_wallets_concurrent still works standalone
(creates and tears down its own executor) for simple one-off use like
validate_direct_feed.py — it just won't show this cross-cycle speedup,
since a fresh executor means fresh worker threads means cold connections
again every time.

Stdlib only (http.client, not urllib/requests) — matches bot.py/dashboard.py's
existing zero-third-party-package policy (see requirements.txt). Deliberately
NOT using `requests` even though its Session/HTTPAdapter would give the same
connection pooling for less custom code — the project has explicitly stated
this zero-dependency policy rather than it being an oversight, so this stays
consistent with that rather than introducing a new dependency for it.
"""

import http.client
import json
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

DATA_API_HOST = "data-api.polymarket.com"
DEFAULT_TIMEOUT_SECONDS = 10
DEFAULT_FETCH_LIMIT = 100
DEFAULT_MAX_WORKERS = 10

# Retryable: 429 (rate limited) and 5xx (plausibly transient server-side issues). NOT retryable:
# other 4xx (400, 404, etc) — these are permanent/logic errors a retry can't fix.
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
RATE_LIMIT_MAX_RETRIES = 4
RATE_LIMIT_BACKOFF_BASE_SECONDS = 1.0  # 1s, 2s, 4s, 8s

# Polymarket's own docs: limit is 0-500 per page (offset-based pagination), offset itself capped
# at 5000 total ("requests past the cap are rejected with a 400"). MAX_PAGES_PER_FETCH bounds how
# many pages ONE fetch_wallet_trades() call will follow before stopping — a generous safety cap
# for a live tracking poll (not a deep-history backfill), not something normal usage should ever
# actually hit.
MAX_PAGES_PER_FETCH = 10

REQUEST_HEADERS = {
    "User-Agent": "polymarket-copybot/1.0 (+personal research bot)",
    "Accept": "application/json",
}

# One HTTPSConnection per worker THREAD, reused across every request that
# thread makes (not per-call) — this is what actually captures the
# connection-reuse speedup. http.client.HTTPSConnection is not thread-safe
# to share across threads simultaneously, so this must stay thread-local,
# never a single module-level connection.
_thread_local = threading.local()


def _get_connection(timeout):
    conn = getattr(_thread_local, "conn", None)
    if conn is None:
        conn = http.client.HTTPSConnection(DATA_API_HOST, timeout=timeout)
        _thread_local.conn = conn
    return conn


def _reset_connection():
    conn = getattr(_thread_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
    _thread_local.conn = None


def _make_request(wallet_address, limit, timeout, start, offset):
    """Makes ONE HTTP request, handling a stale/dead keep-alive connection
    with exactly one reset-and-retry (no backoff delay — a dead connection
    isn't a rate-limit issue). Returns (status, body_bytes) — does NOT
    interpret the HTTP status code itself; that's _fetch_one_page's job,
    since 429/5xx need backoff+retry at a different layer than a broken
    TCP connection does. Unchanged behavior from before this rewrite, just
    extracted into its own function so pagination/backoff can wrap it
    without duplicating this exact retry shape.
    """
    query = {"user": wallet_address, "type": "TRADE", "limit": limit, "offset": offset}
    if start is not None:
        query["start"] = int(start)
    params = urllib.parse.urlencode(query)
    path = f"/activity?{params}"

    for attempt in (1, 2):
        try:
            conn = _get_connection(timeout)
            conn.request("GET", path, headers=REQUEST_HEADERS)
            response = conn.getresponse()
            body = response.read()
            return response.status, body
        except (http.client.HTTPException, OSError) as e:
            _reset_connection()
            if attempt == 2:
                raise RuntimeError(f"fetch failed after reconnect attempt: {e}")


def _fetch_one_page(wallet_address, limit, timeout, start, offset):
    """One logical page fetch, retrying HTTP-level 429/5xx with exponential
    backoff (RETRYABLE_STATUS_CODES, up to RATE_LIMIT_MAX_RETRIES) — a
    DIFFERENT failure class from _make_request's connection-level retry, and
    handled differently on purpose: any OTHER non-200 status (400, 404, etc
    — permanent/logic errors) raises immediately, since retrying an
    inherently-wrong request just wastes time. Polymarket's own docs don't
    document a Retry-After header for this endpoint (see module docstring),
    so this is a fixed doubling schedule, not one driven by a server hint.
    """
    rate_limit_attempt = 0
    while True:
        status, body = _make_request(wallet_address, limit, timeout, start, offset)
        if status == 200:
            return json.loads(body.decode("utf-8"))

        if status in RETRYABLE_STATUS_CODES and rate_limit_attempt < RATE_LIMIT_MAX_RETRIES:
            delay = RATE_LIMIT_BACKOFF_BASE_SECONDS * (2 ** rate_limit_attempt)
            time.sleep(delay)
            rate_limit_attempt += 1
            continue

        raise RuntimeError(f"HTTP {status} after {rate_limit_attempt} backoff retr(y/ies): {body[:200]!r}")


def _activity_trade_id(record):
    """Returns the same stable fill id normalize_activity_record() exposes.

    Kept as one helper because live pagination needs to recognize an
    already-seen raw record before normalization, while downstream callers
    still need the normalized trade_id. The two paths must never drift.
    """
    tx_hash = record.get("transactionHash", "")
    asset = record.get("asset", "")
    side = (record.get("side") or "").upper()
    timestamp = record.get("timestamp")
    return f"{tx_hash}:{asset}:{side}:{timestamp}"


def fetch_wallet_trades(wallet_address, limit=DEFAULT_FETCH_LIMIT,
                        timeout=DEFAULT_TIMEOUT_SECONDS, start=None,
                        known_trade_ids=None):
    """Fetches one wallet's recent TRADE-type activity directly from
    Polymarket's own public Data API, over this thread's persistent
    connection (see module docstring). Returns the raw JSON list
    (Polymarket's own field names, NOT yet normalized — see
    normalize_activity_record), automatically paginated (see module
    docstring's PAGINATION section) so a wallet whose trade count within the
    requested window exceeds one page is never silently truncated.

    `known_trade_ids` is an optional already-processed boundary for a live
    newest-first poll. Once any row on a page is known, that complete page is
    returned and no older offset is requested. Returning the complete boundary
    page is deliberate: downstream dedup removes its known rows, while any
    genuine gap on the same page remains visible instead of being skipped.

    `start` (optional, unix epoch seconds) filters server-side to trades at
    or after that time — confirmed live to work correctly (every returned
    record's timestamp was >= the requested start). Added specifically so
    validate_direct_feed.py can compare against bullpen using the same real
    time window instead of a count-based `limit`, which isn't a fair
    comparison when the two sources' per-call trade volume differs.

    Raises on a genuine failure — callers that fan out across many wallets
    (see fetch_all_wallets_concurrent) must catch per-wallet so one bad
    wallet can't take down the whole batch.
    """
    all_records = []
    offset = 0
    for _ in range(MAX_PAGES_PER_FETCH):
        page = _fetch_one_page(wallet_address, limit, timeout, start, offset)
        all_records.extend(page)
        if known_trade_ids and any(_activity_trade_id(record) in known_trade_ids for record in page):
            break  # reached the live poll's already-processed boundary
        if len(page) < limit:
            break  # short page -> no more data, stop paging
        offset += limit
    return all_records


def normalize_activity_record(record, wallet_address):
    """Converts one Polymarket /activity TRADE record into the exact dict
    shape bot.py's process_trade() already expects from bullpen's tracker
    feed (see bot.py's process_trade() for the authoritative field list) —
    field-mapping confirmed against a real side-by-side bullpen call, not
    guessed:
        user_address  <- proxyWallet
        market_slug    <- slug
        market_title   <- title
        outcome        <- outcome
        side           <- side
        price          <- price
        size_usd       <- usdcSize — the ACTUAL on-chain settled amount,
                           INCLUDING Polymarket's real taker fee (see module
                           docstring) — deliberately not size*price, which
                           is what bullpen itself reports and which this
                           validation found to be the pre-fee nominal value,
                           not what actually moved on-chain.
        timestamp      <- timestamp (unix epoch seconds -> formatted string,
                           same "YYYY-MM-DD HH:MM:SS UTC" shape bullpen uses,
                           so sort-by-timestamp in bot.py's poll loop still
                           orders correctly whichever source produced it)

    trade_id is deliberately NOT Polymarket's transactionHash alone: a single
    settlement transaction can in principle contain multiple distinct fills
    (a market order matching several resting orders at once), so
    transactionHash alone could silently collapse genuinely separate trades
    into one already-seen entry. Composed from transaction_hash + asset +
    side + timestamp instead — all intrinsic, immutable properties of one
    fill, so the same real trade produces the same trade_id on every repeated
    poll (required for bot.py's seen_trade_ids dedup to work correctly).
    """
    tx_hash = record.get("transactionHash", "")
    side = (record.get("side") or "").upper()
    timestamp = record.get("timestamp")
    trade_id = _activity_trade_id(record)

    formatted_timestamp = None
    if timestamp is not None:
        formatted_timestamp = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(timestamp))

    return {
        "trade_id": trade_id,
        "transaction_hash": tx_hash,
        "user_address": record.get("proxyWallet") or wallet_address,
        "market_slug": record.get("slug") or "",
        "market_title": record.get("title") or "",
        "outcome": record.get("outcome") or "",
        "side": side,
        "price": record.get("price"),
        "size_usd": record.get("usdcSize"),
        "timestamp": formatted_timestamp,
    }


def fetch_all_wallets_concurrent(wallet_addresses, limit=DEFAULT_FETCH_LIMIT,
                                  max_workers=DEFAULT_MAX_WORKERS, timeout=DEFAULT_TIMEOUT_SECONDS,
                                  executor=None, start=None, known_trade_ids=None):
    """Fetches every wallet's recent trade activity CONCURRENTLY via a
    thread pool. Pass a long-lived `executor` (created ONCE by the caller,
    reused across every poll cycle) to get the real connection-reuse speedup
    — see module docstring. If `executor` is omitted, one is created and torn
    down for just this call (simpler for one-off use, e.g. validate_direct_
    feed.py, but loses the cross-cycle speedup since a fresh executor means
    fresh worker threads means fresh, cold connections every time).

    A single wallet's fetch failing does not abort the batch — recorded in
    the returned "errors" list, every other wallet's trades are still
    returned normally. Mirrors bot.py's own "one bad market shouldn't stop
    the whole sweep" philosophy (see run_closeout_sweep).

    Returns {"trades": [...normalized dicts, unsorted, possibly containing
    duplicates already in bot.py's seen_trade_ids...], "errors": [...]} —
    same rough shape as bullpen's own feed.get("trades", []) once the caller
    reads .get("trades", []), so this is a drop-in replacement for the
    tracker-feed call site once validated.
    """
    trades = []
    errors = []

    def _fetch_one(wallet_address):
        fetch_kwargs = {"limit": limit, "timeout": timeout, "start": start}
        if known_trade_ids is not None:
            fetch_kwargs["known_trade_ids"] = known_trade_ids
        raw = fetch_wallet_trades(wallet_address, **fetch_kwargs)
        return [normalize_activity_record(r, wallet_address) for r in raw]

    def _run(exec_):
        futures = {exec_.submit(_fetch_one, addr): addr for addr in wallet_addresses}
        for future in as_completed(futures):
            wallet_address = futures[future]
            try:
                trades.extend(future.result())
            except Exception as e:
                errors.append({"wallet_address": wallet_address, "error": str(e)})

    if executor is not None:
        _run(executor)
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as owned_executor:
            _run(owned_executor)

    return {"trades": trades, "errors": errors}


def make_persistent_executor(max_workers=DEFAULT_MAX_WORKERS):
    """Creates the ONE long-lived ThreadPoolExecutor a caller (bot.py's main
    loop) should create ONCE at startup and pass into every
    fetch_all_wallets_concurrent(..., executor=...) call for the rest of the
    process's life — reusing the same worker threads is what keeps each
    thread's persistent HTTPS connection alive across poll cycles instead of
    discarding it every 30s.
    """
    return ThreadPoolExecutor(max_workers=max_workers)
