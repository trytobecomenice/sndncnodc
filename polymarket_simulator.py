#!/usr/bin/env python3
"""Direct Polymarket order-book simulator (2026-07-22) — replaces `bullpen
polymarket preview` for PAPER MODE ORDER SIMULATION ONLY (measure_paper_
shortfall's cost/fee measurement). Actual trade EXECUTION (buy/sell) is
untouched and stays on bullpen's signing path — self-custodying a key to
also remove bullpen from execution would contradict an existing, deliberate
project principle (docs/copy-trading/SAFETY.md §6, "why private keys never
belong in this app"). This module never signs or broadcasts anything; it
only reads two more of Polymarket's own public, no-auth endpoints.

Verified live before writing any of this, not assumed from docs:
- gamma-api.polymarket.com/markets?slug=<slug> is public, no-auth, and
  returns (among many fields) `outcomes` and `clobTokenIds` as same-order,
  same-length JSON-string-encoded arrays (outcomes[i] <-> clobTokenIds[i]),
  plus `feesEnabled` (bool) and `feeSchedule` ({"rate": float, "takerOnly":
  true, ...}) — the EXACT per-market taker fee rate, straight from the
  source. This resolved an open question from the prior design discussion
  (whether a market's fee category would need deriving/guessing): it
  doesn't — the rate is just a field on the market object. Confirmed across
  5 live markets: rate varied (0.04, 0.05) and feesEnabled was False (no
  feeSchedule at all) on one, so both must be handled, not assumed uniform.
- clob.polymarket.com/book?token_id=<id> is public, no-auth, and returns
  {"bids": [...], "asks": [...]}, each {"price": str, "size": str}. Do NOT
  trust the API's own ordering: confirmed live that bids come back sorted
  ASCENDING (best/highest bid last) while asks come back DESCENDING
  (best/lowest ask last) — inconsistent with each other and not documented
  either way, so this module explicitly re-sorts both sides itself
  (bids descending, asks ascending, best price at index 0) rather than
  relying on an unstated, possibly-fragile API convention.
- The taker fee formula (fee = shares * feeRate * price * (1-price)) was
  already verified exactly against 2 independent real trades in a prior
  session (see polymarket_data_api.py's docstring) — reused here verbatim,
  applied per price LEVEL during the book walk (not on the single average
  fill price), since the formula is nonlinear in price and a large copy
  order can span multiple levels.
- A copy-bot's simulated fill is always a TAKER fill (it crosses the spread
  to buy/sell "at market" immediately) — feeSchedule's `takerOnly: true` on
  every market checked confirms Polymarket doesn't charge makers here at
  all, so no maker/taker ambiguity needs resolving for this use case.

WHAT "network_fee" MEANS HERE: always 0.0, and this is NOT a placeholder —
this module never broadcasts a transaction (paper mode only), so there is
no real gas cost to estimate for a simulated fill. bullpen's own preview
number was an estimate of what a REAL execution's gas would cost — a
different quantity that stops applying once no execution is happening.

RATE LIMITS / RETRIES: same worst-case defensive posture as
polymarket_data_api.py (see that module's docstring) — Polymarket's docs
don't publish numbers for these two endpoints either, so 429/5xx get the
same exponential-backoff treatment, other 4xx are never retried. This
module keeps its OWN thread-local-connection + backoff implementation
rather than sharing code with polymarket_data_api.py: that module is the
LIVE, currently-running tracking feed (Rule 14) — refactoring it to share
code here would risk the live feed for a nice-to-have, not something this
task asked for. The pattern is intentionally identical in shape; the code
is a separate, small copy on purpose.

Stdlib only (http.client), matches the project's zero-third-party-
dependency policy.
"""

import http.client
import json
import threading
import time
import urllib.parse

import config

GAMMA_API_HOST = "gamma-api.polymarket.com"
CLOB_API_HOST = "clob.polymarket.com"
DEFAULT_TIMEOUT_SECONDS = 10

RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
RATE_LIMIT_MAX_RETRIES = 4
RATE_LIMIT_BACKOFF_BASE_SECONDS = 1.0  # 1s, 2s, 4s, 8s

# A /book response's own server-side timestamp older than this is refused
# rather than trusted (see fetch_order_book's docstring). Generous versus
# normal network/clock jitter (round trips here are sub-second in practice),
# tight versus a genuinely stale/degraded snapshot — a judgment call, not a
# verified-safe number; 30s (one poll interval) was considered and rejected
# as too loose for a value that feeds TTP exit pricing.
MAX_BOOK_AGE_SECONDS = 15

REQUEST_HEADERS = {
    "User-Agent": "polymarket-copybot/1.0 (+personal research bot)",
    "Accept": "application/json",
}

# One HTTPSConnection per (thread, host), reused across calls this thread
# makes to that host. Keyed by host since this module alone talks to two
# hosts (gamma-api for market/fee metadata, clob for order books).
_thread_local = threading.local()


def _get_connection(host, timeout):
    conns = getattr(_thread_local, "conns", None)
    if conns is None:
        conns = {}
        _thread_local.conns = conns
    conn = conns.get(host)
    if conn is None:
        conn = http.client.HTTPSConnection(host, timeout=timeout)
        conns[host] = conn
    return conn


def _reset_connection(host):
    conns = getattr(_thread_local, "conns", None)
    if not conns:
        return
    conn = conns.get(host)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
        conns[host] = None


def _make_request(host, path, timeout):
    """One HTTP GET, handling a stale/dead keep-alive connection with
    exactly one reset-and-retry (no backoff delay — a dead connection isn't
    a rate-limit issue). Returns (status, body_bytes); does not interpret
    the status code itself."""
    for attempt in (1, 2):
        try:
            conn = _get_connection(host, timeout)
            conn.request("GET", path, headers=REQUEST_HEADERS)
            response = conn.getresponse()
            body = response.read()
            return response.status, body
        except (http.client.HTTPException, OSError) as e:
            _reset_connection(host)
            if attempt == 2:
                raise RuntimeError(f"fetch failed after reconnect attempt: {e}")


def _fetch_json(host, path, timeout):
    """One logical GET, retrying HTTP-level 429/5xx with exponential
    backoff (same policy as polymarket_data_api.py); any other non-200
    status raises immediately."""
    rate_limit_attempt = 0
    while True:
        status, body = _make_request(host, path, timeout)
        if status == 200:
            return json.loads(body.decode("utf-8"))

        if status in RETRYABLE_STATUS_CODES and rate_limit_attempt < RATE_LIMIT_MAX_RETRIES:
            delay = RATE_LIMIT_BACKOFF_BASE_SECONDS * (2 ** rate_limit_attempt)
            time.sleep(delay)
            rate_limit_attempt += 1
            continue

        raise RuntimeError(f"HTTP {status} after {rate_limit_attempt} backoff retr(y/ies): {body[:200]!r}")


def fetch_market_info(market_slug, timeout=DEFAULT_TIMEOUT_SECONDS):
    """Fetches a market's outcome->token_id mapping and real per-market
    taker fee rate from Gamma. Raises if the slug doesn't resolve to
    exactly one market — a copy trade must always be against a real,
    currently-known market, so a miss here is a genuine error, not
    something to paper over.
    """
    path = f"/markets?slug={urllib.parse.quote(market_slug)}"
    data = _fetch_json(GAMMA_API_HOST, path, timeout)
    if not data:
        # Gamma's default /markets listing excludes already-resolved markets
        # (confirmed live, 2026-07-26: the same slug 404s here but returns
        # data with &closed=true). A paper shortfall measurement can easily
        # run after its source market has already closed -- that's a real,
        # known market, not an unknown slug, so retry explicitly before
        # concluding it's genuinely missing.
        closed_path = f"/markets?slug={urllib.parse.quote(market_slug)}&closed=true"
        data = _fetch_json(GAMMA_API_HOST, closed_path, timeout)
    if not data:
        raise RuntimeError(f"no market found for slug {market_slug!r}")
    market = data[0]

    outcomes = json.loads(market.get("outcomes") or "[]")
    clob_token_ids = json.loads(market.get("clobTokenIds") or "[]")
    if len(outcomes) != len(clob_token_ids):
        raise RuntimeError(
            f"outcomes/clobTokenIds length mismatch for {market_slug!r}: "
            f"{outcomes!r} vs {clob_token_ids!r}"
        )

    fees_enabled = bool(market.get("feesEnabled"))
    fee_schedule = market.get("feeSchedule") or {}
    fee_rate = float(fee_schedule.get("rate", 0.0)) if fees_enabled else 0.0

    # event_slug (added for category resolution, 2026-07-22): the SAME
    # /markets?slug= response already carries an `events` field — extracting
    # it here avoids a second Gamma call in resolve_market_category(). None
    # if a market genuinely has no parent event (not observed live, but not
    # assumed impossible either).
    events = market.get("events") or []
    event_slug = events[0].get("slug") if events else None

    return {
        "outcomes": outcomes, "clob_token_ids": clob_token_ids, "fee_rate": fee_rate,
        "event_slug": event_slug,
    }


def token_id_for_outcome(market_info, outcome):
    outcome_lower = outcome.lower()
    for name, token_id in zip(market_info["outcomes"], market_info["clob_token_ids"]):
        if name.lower() == outcome_lower:
            return token_id
    raise RuntimeError(f"outcome {outcome!r} not found among market outcomes {market_info['outcomes']!r}")


def fetch_market_by_token_id(token_id, timeout=DEFAULT_TIMEOUT_SECONDS):
    """The 'unknown token' on-demand fallback (2026-07-25, Rule 30 addendum):
    resolves a raw CTF token_id straight to (market_slug, outcome) for a
    live_whale_event row that arrived before token_sync_worker.py's periodic
    sync ever saw this market.

    Uses `/markets/keyset?clob_token_ids=<id>` — NOT `/tokens/{token_id}`,
    which does not exist (confirmed live: a direct curl against it 404s).
    `/markets/keyset` is Gamma's own current, non-deprecated endpoint (see
    token_sync_worker.py's module docstring for why `/markets` itself is
    avoided) and, verified live, correctly resolves a real token_id to its
    market; an unknown/not-yet-indexed token_id returns 200 with an empty
    `markets` array, not an error — this function treats that the same way
    (returns None), not as an exception.

    Returns (market_slug, outcome) or None if the token_id has no match yet
    (which can legitimately mean "check again shortly", not "this token_id
    is invalid" — see bot.sweep_live_whale_events()'s TTL-bounded retry).
    Raises on an actual HTTP/network failure (distinct from "not found") so
    callers can tell the two apart.
    """
    path = f"/markets/keyset?clob_token_ids={urllib.parse.quote(str(token_id))}"
    data = _fetch_json(GAMMA_API_HOST, path, timeout)
    markets = data.get("markets") or []
    if not markets:
        return None

    market = markets[0]
    slug = market.get("slug")
    try:
        outcomes = json.loads(market.get("outcomes") or "[]")
        clob_token_ids = json.loads(market.get("clobTokenIds") or "[]")
    except (json.JSONDecodeError, TypeError):
        return None
    if len(outcomes) != len(clob_token_ids) or not slug:
        return None

    token_id_str = str(token_id)
    for outcome_name, candidate_token_id in zip(outcomes, clob_token_ids):
        if str(candidate_token_id) == token_id_str:
            return slug, outcome_name
    # The market matched the query but none of its own outcomes' token_ids
    # string-match ours exactly -- treat as "not resolvable" rather than
    # guessing at the wrong outcome.
    return None


def resolve_market_category(event_slug, timeout=DEFAULT_TIMEOUT_SECONDS):
    """Buckets an event's tags against config.CATEGORY_TAG_SLUGS (first match
    wins, in list order). Returns one of:
      - a matched category string (e.g. "politics")
      - "other" — the lookup succeeded but no configured tag matched
      - None — event_slug was falsy, or the lookup itself failed

    Deliberately NEVER RAISES, unlike fetch_market_info()/fetch_order_book()
    — category is a scoring/sizing refinement, not something that should
    block or delay an actual trade. A failed lookup degrades to None, which
    callers treat as "no category signal," falling back to the wallet's
    global composite_score (see bot.compute_trade_size_usd()).

    MULTI-TAG AMBIGUITY, stated plainly, not hidden: one event can carry
    several tags spanning both broad topics and narrow one-off labels —
    confirmed live: the novelty event "what-will-happen-before-gta-vi"
    carried BOTH "politics" and "pop-culture" tags at once, with individual
    sub-markets covering wildly different subjects (a presidential
    scenario, a Bitcoin price target). This function does not disambiguate
    PER-MARKET within a shared event — it buckets by the FIRST
    config.CATEGORY_TAG_SLUGS entry present anywhere in the event's tags,
    in list order. A market whose true topic doesn't match its event's
    bucketed category is a known, accepted imprecision — reordering
    CATEGORY_TAG_SLUGS changes which tag wins ties, it can't eliminate the
    ambiguity itself.
    """
    if not event_slug:
        return None
    try:
        path = f"/events?slug={urllib.parse.quote(event_slug)}"
        data = _fetch_json(GAMMA_API_HOST, path, timeout)
    except Exception:
        return None
    if not data:
        return None

    tags = data[0].get("tags") or []
    tag_slugs = {t.get("slug") for t in tags if t.get("slug")}
    for category in config.CATEGORY_TAG_SLUGS:
        if category in tag_slugs:
            return category
    return "other"


def fetch_order_book(token_id, timeout=DEFAULT_TIMEOUT_SECONDS, ignore_staleness=False):
    """Fetches one token's live order book. Returns {"bids": [(price,
    size), ...], "asks": [(price, size), ...], "last_trade_price": float
    or None}, EXPLICITLY sorted here (bids descending, asks ascending,
    best price always at index 0) — see module docstring on why the API's
    own ordering isn't trusted. last_trade_price comes from the same
    /book response (confirmed live: it's a real top-level field on this
    endpoint, per-token — not a separate call) — used by bot.py's TTP
    price check as its last-resort fallback, not by simulate_fill.

    STALENESS CHECK (added 2026-07-22, found during a resilience review):
    the /book response carries its own server-side `timestamp` (ms since
    epoch) — this used to be fetched and silently discarded. Now compared
    against wall-clock time; a book older than MAX_BOOK_AGE_SECONDS raises
    rather than being trusted. This catches a genuinely stale snapshot from
    a degraded Polymarket backend (the HTTP call still succeeds inside
    `timeout` — a slow/stuck matching engine can still serve an old cached
    book quickly) — a different failure mode from a slow/dead connection,
    which the timeout and connection-retry logic already handle. Lenient
    if `timestamp` is absent (not raised as an error) — every response
    checked live during development included it, so this isn't expected in
    practice; not inventing a new failure mode for a field that shouldn't
    be missing rather than papering over one that's actually missing.

    ignore_staleness (2026-07-27, "zombie position" dump exit — see
    RISK_MANAGEMENT.md's zombie-position rule) skips the raise above
    entirely, still returning whatever book IS there. Default False for
    every existing caller (normal fills/TTP checks must never trade on
    data this check would otherwise reject) — the ONLY caller that should
    ever pass True is the zombie-dump exit path, for a position that has
    already failed this exact check for >= config.
    ZOMBIE_POSITION_THRESHOLD_SECONDS: at that point, continuing to refuse
    stale data just keeps capital trapped, which is worse than a
    best-effort exit at a still-real (if not maximally fresh) price.
    """
    path = f"/book?token_id={token_id}"
    data = _fetch_json(CLOB_API_HOST, path, timeout)

    book_timestamp_ms = data.get("timestamp")
    if book_timestamp_ms is not None and not ignore_staleness:
        age_seconds = time.time() - (float(book_timestamp_ms) / 1000.0)
        if age_seconds > MAX_BOOK_AGE_SECONDS:
            raise RuntimeError(
                f"order book for token {token_id} is stale: {age_seconds:.1f}s old "
                f"(server timestamp {book_timestamp_ms}), exceeds "
                f"MAX_BOOK_AGE_SECONDS={MAX_BOOK_AGE_SECONDS} — refusing to price "
                f"a fill or TTP check off it rather than trust a degraded snapshot"
            )

    bids = sorted(
        ((float(level["price"]), float(level["size"])) for level in data.get("bids", [])),
        key=lambda level: level[0], reverse=True,
    )
    asks = sorted(
        ((float(level["price"]), float(level["size"])) for level in data.get("asks", [])),
        key=lambda level: level[0],
    )
    # 2026-07-29: confirmed live against the real API — this field comes
    # back as a JSON STRING (e.g. "0.999"), same as every bid/ask price
    # above, NOT already a float despite this function's own docstring
    # claiming "float or None". Uncoerced, this silently produced a str
    # that crashed bot.py's get_market_prices() the moment a thin/empty
    # book fell back to it (`indicative <= 0` -> "'<=' not supported
    # between instances of 'str' and 'int'") — found live: 200 occurrences
    # over 3+ days, each one aborting that entire TTP sweep cycle AND
    # skipping that cycle's kill-switch evaluation (see bot.py's main loop,
    # which explicitly skips the equity check when the sweep itself failed).
    last_trade_price_raw = data.get("last_trade_price")
    last_trade_price = float(last_trade_price_raw) if last_trade_price_raw is not None else None
    return {"bids": bids, "asks": asks, "last_trade_price": last_trade_price}


def fetch_order_book_for_outcome(market_slug, outcome, timeout=DEFAULT_TIMEOUT_SECONDS, ignore_staleness=False):
    """Resolves market_slug+outcome -> token_id (via fetch_market_info) then
    fetches that token's order book — the composed lookup both
    simulate_fill() and bot.py's get_market_prices() (TTP monitoring) need,
    just for different purposes (fill simulation vs. a live price read).
    Returns (market_info, book); callers that don't need market_info's
    fee_rate (i.e. get_market_prices) just ignore it.

    ignore_staleness just threads through to fetch_order_book — see that
    function's docstring; default False, only the zombie-dump exit path
    should ever pass True.
    """
    market_info = fetch_market_info(market_slug, timeout=timeout)
    token_id = token_id_for_outcome(market_info, outcome)
    book = fetch_order_book(token_id, timeout=timeout, ignore_staleness=ignore_staleness)
    return market_info, book


def _walk_asks_for_usd(asks, usd_budget, fee_rate):
    """Walks the ask side to fill a USD-denominated BUY, level by level,
    applying the fee formula PER LEVEL (nonlinear in price, so summing
    per-level fees is more accurate than applying it once to the average
    fill price). Returns (avg_price, shares_filled, total_fee, exhausted)
    — exhausted=True means the visible book didn't have enough depth to
    fill the whole budget.
    """
    remaining_usd = usd_budget
    shares_filled = 0.0
    total_fee = 0.0
    for price, size in asks:
        if remaining_usd <= 0:
            break
        level_cost = price * size
        if level_cost <= remaining_usd:
            level_shares, level_cost_taken = size, level_cost
        else:
            level_shares, level_cost_taken = remaining_usd / price, remaining_usd
        shares_filled += level_shares
        remaining_usd -= level_cost_taken
        total_fee += level_shares * fee_rate * price * (1 - price)

    exhausted = remaining_usd > 1e-9
    filled_usd = usd_budget - remaining_usd
    avg_price = (filled_usd / shares_filled) if shares_filled > 0 else None
    return avg_price, shares_filled, total_fee, exhausted


def _walk_bids_for_shares(bids, shares_to_sell, fee_rate):
    """Same idea as _walk_asks_for_usd, mirrored for a share-denominated
    SELL walking the bid side."""
    remaining_shares = shares_to_sell
    proceeds = 0.0
    total_fee = 0.0
    for price, size in bids:
        if remaining_shares <= 0:
            break
        level_shares = min(size, remaining_shares)
        proceeds += level_shares * price
        total_fee += level_shares * fee_rate * price * (1 - price)
        remaining_shares -= level_shares

    exhausted = remaining_shares > 1e-9
    shares_filled = shares_to_sell - remaining_shares
    avg_price = (proceeds / shares_filled) if shares_filled > 0 else None
    return avg_price, shares_filled, total_fee, exhausted


def simulate_fill(market_slug, outcome, side, amount, timeout=DEFAULT_TIMEOUT_SECONDS):
    """Drop-in replacement for `bullpen polymarket preview`'s role inside
    measure_paper_shortfall(): same input convention (amount = USD for
    BUY, shares for SELL) and same output keys the caller already reads
    (price, spread, trading_fee, network_fee) — so the call site is a
    one-line swap. Sourced entirely from Polymarket's own public APIs, no
    bullpen involved.

    Returns a dict with no "price" key (so the caller's existing
    `not preview.get("price")` check naturally treats it as
    no-executable-price) when the book is completely empty on the
    relevant side, or when the outcome name doesn't match the outcome
    Polymarket's own market data has (raises instead in that second case
    — see below). Raises on a genuine fetch/lookup failure (bad slug,
    network error) so callers that already wrap this in try/except (i.e.
    measure_paper_shortfall) get the same fail-soft behavior they had with
    bullpen.
    """
    market_info, book = fetch_order_book_for_outcome(market_slug, outcome, timeout=timeout)

    if not book["bids"] or not book["asks"]:
        return {}
    spread = book["asks"][0][0] - book["bids"][0][0]

    if side == "BUY":
        avg_price, shares_filled, fee, exhausted = _walk_asks_for_usd(book["asks"], amount, market_info["fee_rate"])
    else:
        avg_price, shares_filled, fee, exhausted = _walk_bids_for_shares(book["bids"], amount, market_info["fee_rate"])

    if avg_price is None:
        return {}

    result = {"price": avg_price, "spread": spread, "trading_fee": fee, "network_fee": 0.0}
    if exhausted:
        # The visible book couldn't fill the whole requested amount — a real
        # copy attempt of this size would likely have hit the same wall.
        # Surfaced as an extra field (measure_paper_shortfall opts in to
        # reading it), not a failure: the partial-fill price/fee are still
        # real, useful information.
        result["insufficient_liquidity"] = True
        result["shares_filled"] = shares_filled
    return result
