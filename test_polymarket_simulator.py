#!/usr/bin/env python3
"""Unit tests for polymarket_simulator.py — the direct order-book-walking
replacement for `bullpen polymarket preview` in paper-mode simulation.

Run: python3 -m unittest test_polymarket_simulator -v
"""

import json
import time
import unittest
from unittest.mock import MagicMock, patch

import config
import polymarket_simulator
from polymarket_simulator import (
    _thread_local,
    token_id_for_outcome,
    _walk_asks_for_usd,
    _walk_bids_for_shares,
    fetch_market_by_token_id,
    fetch_market_info,
    fetch_order_book,
    fetch_order_book_for_outcome,
    resolve_market_category,
    simulate_fill,
)


def _mock_response(status, body_obj):
    resp = MagicMock()
    resp.status = status
    resp.read.return_value = json.dumps(body_obj).encode("utf-8")
    return resp


def _book_body(bids, asks, last_trade_price=None, age_seconds=0.0):
    """Builds a /book response body with a realistic `timestamp` field (ms
    since epoch, `age_seconds` old — 0 by default, i.e. fresh) so tests
    exercise the same staleness check real responses go through, rather
    than accidentally bypassing it via a field the real API always sends.
    """
    body = {"bids": bids, "asks": asks, "timestamp": str(int((time.time() - age_seconds) * 1000))}
    if last_trade_price is not None:
        body["last_trade_price"] = last_trade_price
    return body


# A real market shape, taken directly from a live curl against
# gamma-api.polymarket.com/markets?slug=... (see module docstring).
REAL_MARKET = {
    "slug": "new-rhianna-album-before-gta-vi-926",
    "outcomes": '["Yes", "No"]',
    "clobTokenIds": '["111", "222"]',
    "feesEnabled": True,
    "feeSchedule": {"exponent": 1, "rate": 0.05, "takerOnly": True, "rebateRate": 0.25},
    "events": [{"slug": "what-will-happen-before-gta-vi"}],
}

# Real event `tags` shapes, taken directly from live curls against
# gamma-api.polymarket.com/events?slug=... (see resolve_market_category's docstring).
NOVELTY_EVENT = {
    "slug": "what-will-happen-before-gta-vi",
    "tags": [{"slug": "pop-culture", "label": "Culture"}, {"slug": "all", "label": "All"},
             {"slug": "politics", "label": "Politics"}, {"slug": "gta-vi", "label": "GTA VI"}],
}
POLITICS_EVENT = {
    "slug": "democratic-presidential-nominee-2028",
    "tags": [{"slug": "united-states", "label": "United States"}, {"slug": "elections", "label": "Elections"},
             {"slug": "politics", "label": "Politics"}],
}


class TestFetchMarketInfo(unittest.TestCase):
    def tearDown(self):
        _thread_local.conns = None

    def test_parses_outcomes_token_ids_and_fee_rate(self):
        mock_conn = MagicMock()
        mock_conn.getresponse.side_effect = [_mock_response(200, [REAL_MARKET])]
        with patch("http.client.HTTPSConnection", return_value=mock_conn):
            info = fetch_market_info("new-rhianna-album-before-gta-vi-926")
        self.assertEqual(info["outcomes"], ["Yes", "No"])
        self.assertEqual(info["clob_token_ids"], ["111", "222"])
        self.assertAlmostEqual(info["fee_rate"], 0.05)
        self.assertEqual(info["event_slug"], "what-will-happen-before-gta-vi")

    def test_missing_events_field_gives_none_event_slug_not_a_crash(self):
        market = {**REAL_MARKET, "events": []}
        mock_conn = MagicMock()
        mock_conn.getresponse.side_effect = [_mock_response(200, [market])]
        with patch("http.client.HTTPSConnection", return_value=mock_conn):
            info = fetch_market_info("some-slug")
        self.assertIsNone(info["event_slug"])

    def test_fees_disabled_market_gets_zero_rate_not_missing_key(self):
        market = {**REAL_MARKET, "feesEnabled": False, "feeSchedule": None}
        mock_conn = MagicMock()
        mock_conn.getresponse.side_effect = [_mock_response(200, [market])]
        with patch("http.client.HTTPSConnection", return_value=mock_conn):
            info = fetch_market_info("some-slug")
        self.assertEqual(info["fee_rate"], 0.0)

    def test_no_market_found_raises_rather_than_silently_returning_nothing(self):
        # Genuinely unknown slug: both the plain lookup AND the closed=true
        # retry come back empty (see fetch_market_info's closed-market
        # retry, 2026-07-26) before this raises.
        mock_conn = MagicMock()
        mock_conn.getresponse.side_effect = [_mock_response(200, []), _mock_response(200, [])]
        with patch("http.client.HTTPSConnection", return_value=mock_conn):
            with self.assertRaises(RuntimeError):
                fetch_market_info("no-such-slug")

    def test_closed_market_found_via_retry(self):
        # First lookup (default, excludes resolved markets) comes back
        # empty; the closed=true retry finds the real, already-resolved
        # market -- must succeed, not raise.
        mock_conn = MagicMock()
        mock_conn.getresponse.side_effect = [_mock_response(200, []), _mock_response(200, [REAL_MARKET])]
        with patch("http.client.HTTPSConnection", return_value=mock_conn):
            info = fetch_market_info("some-resolved-slug")
        self.assertEqual(info["outcomes"], ["Yes", "No"])

    def test_mismatched_outcomes_and_token_ids_length_raises(self):
        market = {**REAL_MARKET, "clobTokenIds": '["111"]'}  # only 1, outcomes has 2
        mock_conn = MagicMock()
        mock_conn.getresponse.side_effect = [_mock_response(200, [market])]
        with patch("http.client.HTTPSConnection", return_value=mock_conn):
            with self.assertRaises(RuntimeError):
                fetch_market_info("some-slug")


class TestFetchMarketByTokenId(unittest.TestCase):
    """The 'unknown token' on-demand fallback (Rule 30 addendum) — real
    /markets/keyset response shape, verified live before this function was
    written (see its own docstring)."""

    def tearDown(self):
        _thread_local.conns = None

    def _keyset_response(self, markets):
        return {"$schema": "...", "markets": markets}

    def test_resolves_the_matching_outcome_not_just_the_first_one(self):
        market = {**REAL_MARKET}  # outcomes=["Yes","No"], clobTokenIds=["111","222"]
        mock_conn = MagicMock()
        mock_conn.getresponse.side_effect = [_mock_response(200, self._keyset_response([market]))]
        with patch("http.client.HTTPSConnection", return_value=mock_conn):
            result = fetch_market_by_token_id("222")
        self.assertEqual(result, ("new-rhianna-album-before-gta-vi-926", "No"))

    def test_integer_token_id_matches_string_clob_token_id(self):
        market = {**REAL_MARKET}
        mock_conn = MagicMock()
        mock_conn.getresponse.side_effect = [_mock_response(200, self._keyset_response([market]))]
        with patch("http.client.HTTPSConnection", return_value=mock_conn):
            result = fetch_market_by_token_id(111)
        self.assertEqual(result, ("new-rhianna-album-before-gta-vi-926", "Yes"))

    def test_unknown_token_id_returns_none_not_an_exception(self):
        mock_conn = MagicMock()
        mock_conn.getresponse.side_effect = [_mock_response(200, self._keyset_response([]))]
        with patch("http.client.HTTPSConnection", return_value=mock_conn):
            result = fetch_market_by_token_id("99999")
        self.assertIsNone(result)

    def test_mismatched_outcomes_and_token_ids_length_returns_none(self):
        market = {**REAL_MARKET, "clobTokenIds": '["111"]'}  # only 1, outcomes has 2
        mock_conn = MagicMock()
        mock_conn.getresponse.side_effect = [_mock_response(200, self._keyset_response([market]))]
        with patch("http.client.HTTPSConnection", return_value=mock_conn):
            result = fetch_market_by_token_id("111")
        self.assertIsNone(result)

    def test_matched_market_but_token_id_not_among_its_own_outcomes_returns_none(self):
        # Defensive case: the query matched a market, but (e.g. a Gamma data
        # quirk) none of ITS OWN clobTokenIds string-match ours exactly.
        market = {**REAL_MARKET}
        mock_conn = MagicMock()
        mock_conn.getresponse.side_effect = [_mock_response(200, self._keyset_response([market]))]
        with patch("http.client.HTTPSConnection", return_value=mock_conn):
            result = fetch_market_by_token_id("999999")
        self.assertIsNone(result)


class TestTokenIdForOutcome(unittest.TestCase):
    def test_matches_case_insensitively(self):
        info = {"outcomes": ["Yes", "No"], "clob_token_ids": ["111", "222"]}
        self.assertEqual(token_id_for_outcome(info, "yes"), "111")
        self.assertEqual(token_id_for_outcome(info, "NO"), "222")

    def test_unmatched_outcome_raises(self):
        info = {"outcomes": ["Yes", "No"], "clob_token_ids": ["111", "222"]}
        with self.assertRaises(RuntimeError):
            token_id_for_outcome(info, "Maybe")


class TestResolveMarketCategory(unittest.TestCase):
    """resolve_market_category() — added for category-specific wallet
    scoring. Real tag data taken from live curls (see module docstring)."""

    def tearDown(self):
        _thread_local.conns = None

    def test_matches_a_configured_category(self):
        mock_conn = MagicMock()
        mock_conn.getresponse.side_effect = [_mock_response(200, [POLITICS_EVENT])]
        with patch("http.client.HTTPSConnection", return_value=mock_conn):
            category = resolve_market_category("democratic-presidential-nominee-2028")
        self.assertEqual(category, "politics")

    def test_multi_tag_event_matches_first_configured_slug_in_list_order(self):
        # NOVELTY_EVENT carries both "pop-culture" and "politics" tags —
        # config.CATEGORY_TAG_SLUGS lists "politics" before "pop-culture",
        # so politics must win regardless of the tags array's own order
        # (pop-culture appears FIRST in the raw tags list).
        self.assertLess(
            config.CATEGORY_TAG_SLUGS.index("politics"),
            config.CATEGORY_TAG_SLUGS.index("pop-culture"),
        )
        mock_conn = MagicMock()
        mock_conn.getresponse.side_effect = [_mock_response(200, [NOVELTY_EVENT])]
        with patch("http.client.HTTPSConnection", return_value=mock_conn):
            category = resolve_market_category("what-will-happen-before-gta-vi")
        self.assertEqual(category, "politics")

    def test_no_matching_tag_returns_other(self):
        event = {"slug": "some-event", "tags": [{"slug": "caitlin-clark", "label": "Caitlin Clark"}]}
        mock_conn = MagicMock()
        mock_conn.getresponse.side_effect = [_mock_response(200, [event])]
        with patch("http.client.HTTPSConnection", return_value=mock_conn):
            category = resolve_market_category("some-event")
        self.assertEqual(category, "other")

    def test_falsy_event_slug_returns_none_without_a_network_call(self):
        self.assertIsNone(resolve_market_category(None))
        self.assertIsNone(resolve_market_category(""))

    def test_event_not_found_returns_none_not_a_crash(self):
        mock_conn = MagicMock()
        mock_conn.getresponse.side_effect = [_mock_response(200, [])]
        with patch("http.client.HTTPSConnection", return_value=mock_conn):
            category = resolve_market_category("no-such-event")
        self.assertIsNone(category)

    def test_fetch_failure_returns_none_not_a_raise(self):
        # Category is a scoring refinement, not a trade-blocking check — a
        # failed lookup must degrade gracefully, unlike fetch_market_info's
        # raise-on-failure contract.
        mock_conn = MagicMock()
        mock_conn.getresponse.side_effect = [_mock_response(500, {"error": "server error"})]
        with patch("http.client.HTTPSConnection", return_value=mock_conn), \
             patch("polymarket_simulator.RATE_LIMIT_MAX_RETRIES", 0):
            category = resolve_market_category("some-event")
        self.assertIsNone(category)


class TestFetchOrderBook(unittest.TestCase):
    def tearDown(self):
        _thread_local.conns = None

    def test_reorders_bids_descending_and_asks_ascending_regardless_of_api_order(self):
        # Real API behavior confirmed live: bids come back ascending, asks
        # come back descending — this module must not trust that ordering.
        raw = _book_body(
            bids=[{"price": "0.10", "size": "5"}, {"price": "0.50", "size": "3"}],
            asks=[{"price": "0.90", "size": "2"}, {"price": "0.55", "size": "4"}],
        )
        mock_conn = MagicMock()
        mock_conn.getresponse.side_effect = [_mock_response(200, raw)]
        with patch("http.client.HTTPSConnection", return_value=mock_conn):
            book = fetch_order_book("some-token")
        self.assertEqual(book["bids"], [(0.50, 3.0), (0.10, 5.0)])  # best (highest) bid first
        self.assertEqual(book["asks"], [(0.55, 4.0), (0.90, 2.0)])  # best (lowest) ask first

    def test_surfaces_last_trade_price_from_the_same_response(self):
        # Confirmed live: /book's own response includes last_trade_price
        # directly — no second call needed for bot.py's TTP price check.
        # Passed as a STRING here (2026-07-29 fix) -- the real API's actual
        # wire format, confirmed live: "0.999", not 0.999.
        raw = _book_body(
            bids=[{"price": "0.10", "size": "5"}],
            asks=[{"price": "0.90", "size": "2"}],
            last_trade_price="0.51",
        )
        mock_conn = MagicMock()
        mock_conn.getresponse.side_effect = [_mock_response(200, raw)]
        with patch("http.client.HTTPSConnection", return_value=mock_conn):
            book = fetch_order_book("some-token")
        self.assertEqual(book["last_trade_price"], 0.51)

    def test_last_trade_price_string_from_real_api_is_coerced_to_float(self):
        # 2026-07-29: found live -- the real API always returns this field
        # as a JSON string ("0.999"), never already numeric, despite this
        # function's own docstring claiming "float or None". Uncoerced,
        # this crashed bot.py's get_market_prices() ('<=' not supported
        # between instances of 'str' and 'int') every time a thin/empty
        # book fell back to it -- 200 real occurrences over 3+ days before
        # being caught, each one silently aborting that TTP sweep AND that
        # cycle's kill-switch evaluation. This pins the fix down as a type,
        # not just a value.
        raw = _book_body(bids=[], asks=[], last_trade_price="0.13")
        mock_conn = MagicMock()
        mock_conn.getresponse.side_effect = [_mock_response(200, raw)]
        with patch("http.client.HTTPSConnection", return_value=mock_conn):
            book = fetch_order_book("some-token")
        self.assertEqual(book["last_trade_price"], 0.13)
        self.assertIsInstance(book["last_trade_price"], float)

    def test_missing_last_trade_price_is_none_not_a_crash(self):
        raw = _book_body(bids=[], asks=[])
        mock_conn = MagicMock()
        mock_conn.getresponse.side_effect = [_mock_response(200, raw)]
        with patch("http.client.HTTPSConnection", return_value=mock_conn):
            book = fetch_order_book("some-token")
        self.assertIsNone(book["last_trade_price"])


class TestOrderBookStaleness(unittest.TestCase):
    """fetch_order_book()'s staleness check, added 2026-07-22 after finding
    the /book response's own timestamp field was fetched and silently
    discarded — never checked against wall-clock time."""

    def tearDown(self):
        _thread_local.conns = None

    def test_fresh_book_is_accepted(self):
        raw = _book_body(bids=[{"price": "0.10", "size": "5"}], asks=[{"price": "0.90", "size": "2"}], age_seconds=1.0)
        mock_conn = MagicMock()
        mock_conn.getresponse.side_effect = [_mock_response(200, raw)]
        with patch("http.client.HTTPSConnection", return_value=mock_conn):
            book = fetch_order_book("some-token")  # must not raise
        self.assertEqual(len(book["bids"]), 1)

    def test_book_older_than_max_age_raises(self):
        raw = _book_body(
            bids=[{"price": "0.10", "size": "5"}], asks=[{"price": "0.90", "size": "2"}],
            age_seconds=polymarket_simulator.MAX_BOOK_AGE_SECONDS + 5,
        )
        mock_conn = MagicMock()
        mock_conn.getresponse.side_effect = [_mock_response(200, raw)]
        with patch("http.client.HTTPSConnection", return_value=mock_conn):
            with self.assertRaises(RuntimeError) as ctx:
                fetch_order_book("some-token")
        self.assertIn("stale", str(ctx.exception))

    def test_book_exactly_at_the_boundary_is_accepted(self):
        raw = _book_body(
            bids=[{"price": "0.10", "size": "5"}], asks=[{"price": "0.90", "size": "2"}],
            age_seconds=polymarket_simulator.MAX_BOOK_AGE_SECONDS - 0.5,
        )
        mock_conn = MagicMock()
        mock_conn.getresponse.side_effect = [_mock_response(200, raw)]
        with patch("http.client.HTTPSConnection", return_value=mock_conn):
            fetch_order_book("some-token")  # must not raise

    def test_missing_timestamp_is_lenient_not_an_error(self):
        raw = {"bids": [{"price": "0.10", "size": "5"}], "asks": [{"price": "0.90", "size": "2"}]}
        mock_conn = MagicMock()
        mock_conn.getresponse.side_effect = [_mock_response(200, raw)]
        with patch("http.client.HTTPSConnection", return_value=mock_conn):
            book = fetch_order_book("some-token")  # must not raise
        self.assertEqual(len(book["bids"]), 1)

    def test_ignore_staleness_bypasses_the_raise(self):
        # The zombie-position dump exit's whole reason to exist (2026-07-27,
        # see bot.close_position_zombie_dump) — a book this old must still
        # be usable when explicitly opted into via ignore_staleness=True.
        raw = _book_body(
            bids=[{"price": "0.10", "size": "5"}], asks=[{"price": "0.90", "size": "2"}],
            age_seconds=polymarket_simulator.MAX_BOOK_AGE_SECONDS + 300,
        )
        mock_conn = MagicMock()
        mock_conn.getresponse.side_effect = [_mock_response(200, raw)]
        with patch("http.client.HTTPSConnection", return_value=mock_conn):
            book = fetch_order_book("some-token", ignore_staleness=True)  # must not raise
        self.assertEqual(len(book["bids"]), 1)

    def test_ignore_staleness_default_is_false_normal_callers_unaffected(self):
        raw = _book_body(
            bids=[{"price": "0.10", "size": "5"}], asks=[{"price": "0.90", "size": "2"}],
            age_seconds=polymarket_simulator.MAX_BOOK_AGE_SECONDS + 5,
        )
        mock_conn = MagicMock()
        mock_conn.getresponse.side_effect = [_mock_response(200, raw)]
        with patch("http.client.HTTPSConnection", return_value=mock_conn):
            with self.assertRaises(RuntimeError):
                fetch_order_book("some-token")  # no ignore_staleness passed -> still raises


class TestFetchOrderBookForOutcome(unittest.TestCase):
    """The shared market_slug+outcome -> book lookup used by both
    simulate_fill() and bot.py's get_market_prices()."""

    def tearDown(self):
        _thread_local.conns = None

    def test_resolves_outcome_to_token_id_then_fetches_that_books_book(self):
        book_raw = _book_body(
            bids=[{"price": "0.48", "size": "10"}],
            asks=[{"price": "0.50", "size": "10"}],
            last_trade_price=0.49,
        )
        mock_conn = MagicMock()
        mock_conn.getresponse.side_effect = [
            _mock_response(200, [REAL_MARKET]),  # market info (Yes -> "111", No -> "222")
            _mock_response(200, book_raw),
        ]
        with patch("http.client.HTTPSConnection", return_value=mock_conn):
            market_info, book = fetch_order_book_for_outcome("new-rhianna-album-before-gta-vi-926", "No")
        self.assertEqual(market_info["outcomes"], ["Yes", "No"])
        self.assertEqual(book["bids"], [(0.48, 10.0)])
        self.assertEqual(book["last_trade_price"], 0.49)
        # Confirms it actually requested the "No" token specifically, not just the first one.
        second_call_path = mock_conn.request.call_args_list[1][0][1]
        self.assertIn("222", second_call_path)


class TestWalkAsksForUsd(unittest.TestCase):
    def test_fills_entirely_within_the_first_level(self):
        asks = [(0.50, 100.0), (0.60, 100.0)]
        avg_price, shares, fee, exhausted = _walk_asks_for_usd(asks, 25.0, fee_rate=0.05)
        self.assertAlmostEqual(avg_price, 0.50)
        self.assertAlmostEqual(shares, 50.0)  # $25 / 0.50
        self.assertFalse(exhausted)
        # fee = shares * feeRate * price * (1-price) = 50 * 0.05 * 0.50 * 0.50
        self.assertAlmostEqual(fee, 50.0 * 0.05 * 0.50 * 0.50)

    def test_walks_across_multiple_levels_and_fee_is_summed_per_level(self):
        asks = [(0.40, 10.0), (0.50, 100.0)]  # level 1 only holds $4 worth
        avg_price, shares, fee, exhausted = _walk_asks_for_usd(asks, 20.0, fee_rate=0.05)
        # level 1: 10 shares @ 0.40 = $4; remaining $16 at level 2: 32 shares @ 0.50
        self.assertAlmostEqual(shares, 10.0 + 32.0)
        self.assertAlmostEqual(avg_price, 20.0 / 42.0)
        expected_fee = (10.0 * 0.05 * 0.40 * 0.60) + (32.0 * 0.05 * 0.50 * 0.50)
        self.assertAlmostEqual(fee, expected_fee)
        self.assertFalse(exhausted)

    def test_insufficient_depth_flags_exhausted_with_partial_fill(self):
        asks = [(0.50, 10.0)]  # only $5 worth of depth
        avg_price, shares, fee, exhausted = _walk_asks_for_usd(asks, 100.0, fee_rate=0.05)
        self.assertAlmostEqual(shares, 10.0)
        self.assertTrue(exhausted)

    def test_empty_book_returns_none_price(self):
        avg_price, shares, fee, exhausted = _walk_asks_for_usd([], 100.0, fee_rate=0.05)
        self.assertIsNone(avg_price)
        self.assertEqual(shares, 0.0)


class TestWalkBidsForShares(unittest.TestCase):
    def test_fills_entirely_within_the_first_level(self):
        bids = [(0.50, 100.0), (0.40, 100.0)]
        avg_price, shares, fee, exhausted = _walk_bids_for_shares(bids, 30.0, fee_rate=0.05)
        self.assertAlmostEqual(avg_price, 0.50)
        self.assertAlmostEqual(shares, 30.0)
        self.assertFalse(exhausted)

    def test_insufficient_depth_flags_exhausted(self):
        bids = [(0.50, 10.0)]
        avg_price, shares, fee, exhausted = _walk_bids_for_shares(bids, 100.0, fee_rate=0.05)
        self.assertAlmostEqual(shares, 10.0)
        self.assertTrue(exhausted)


class TestSimulateFill(unittest.TestCase):
    """End-to-end: fetch_market_info + fetch_order_book + walk, mocked at
    the HTTP layer so this exercises the real glue between them."""

    def tearDown(self):
        _thread_local.conns = None

    def test_buy_returns_price_spread_fee_and_zero_network_fee(self):
        book = _book_body(bids=[{"price": "0.48", "size": "1000"}], asks=[{"price": "0.50", "size": "1000"}])
        mock_conn = MagicMock()
        mock_conn.getresponse.side_effect = [
            _mock_response(200, [REAL_MARKET]),  # market info
            _mock_response(200, book),           # order book
        ]
        with patch("http.client.HTTPSConnection", return_value=mock_conn):
            result = simulate_fill("new-rhianna-album-before-gta-vi-926", "Yes", "BUY", 100.0)
        self.assertAlmostEqual(result["price"], 0.50)
        self.assertAlmostEqual(result["spread"], 0.02)
        self.assertEqual(result["network_fee"], 0.0)
        self.assertGreater(result["trading_fee"], 0.0)
        self.assertNotIn("insufficient_liquidity", result)

    def test_empty_ask_side_returns_empty_dict_not_a_crash(self):
        book = _book_body(bids=[{"price": "0.48", "size": "1000"}], asks=[])
        mock_conn = MagicMock()
        mock_conn.getresponse.side_effect = [
            _mock_response(200, [REAL_MARKET]),
            _mock_response(200, book),
        ]
        with patch("http.client.HTTPSConnection", return_value=mock_conn):
            result = simulate_fill("new-rhianna-album-before-gta-vi-926", "Yes", "BUY", 100.0)
        self.assertEqual(result, {})

    def test_insufficient_liquidity_is_flagged(self):
        book = _book_body(
            bids=[{"price": "0.48", "size": "1000"}],
            asks=[{"price": "0.50", "size": "10"}],  # only $5 worth
        )
        mock_conn = MagicMock()
        mock_conn.getresponse.side_effect = [
            _mock_response(200, [REAL_MARKET]),
            _mock_response(200, book),
        ]
        with patch("http.client.HTTPSConnection", return_value=mock_conn):
            result = simulate_fill("new-rhianna-album-before-gta-vi-926", "Yes", "BUY", 100.0)
        self.assertTrue(result["insufficient_liquidity"])
        self.assertAlmostEqual(result["shares_filled"], 10.0)

    def test_sell_walks_bid_side(self):
        book = _book_body(bids=[{"price": "0.48", "size": "1000"}], asks=[{"price": "0.50", "size": "1000"}])
        mock_conn = MagicMock()
        mock_conn.getresponse.side_effect = [
            _mock_response(200, [REAL_MARKET]),
            _mock_response(200, book),
        ]
        with patch("http.client.HTTPSConnection", return_value=mock_conn):
            result = simulate_fill("new-rhianna-album-before-gta-vi-926", "Yes", "SELL", 50.0)
        self.assertAlmostEqual(result["price"], 0.48)


if __name__ == "__main__":
    unittest.main()
