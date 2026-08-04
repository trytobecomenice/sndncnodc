#!/usr/bin/env python3
"""Unit tests for polymarket_data_api.py — mainly normalize_activity_record(),
the field-mapping adapter process_trade() depends on being exactly right, and
fetch_all_wallets_concurrent()'s per-wallet error isolation.

Run: python3 -m unittest test_polymarket_data_api -v
"""

import json
import unittest
from unittest.mock import MagicMock, patch

import polymarket_data_api
from polymarket_data_api import (
    _get_connection,
    _reset_connection,
    _thread_local,
    fetch_all_wallets_concurrent,
    fetch_wallet_trades,
    make_persistent_executor,
    normalize_activity_record,
)


# A real record shape, taken directly from a live curl against data-api.polymarket.com
# (see polymarket_data_api.py's module docstring) — not fabricated.
REAL_TRADE_RECORD = {
    "proxyWallet": "0xf0318c32136c2db7fec88b84869aee6a1106c80c",
    "timestamp": 1784643205,
    "conditionId": "0xeb9efeb9813767d7c6eb5ae79d18f7cfcd5bd5a408292e03b7e725bc521f5a16",
    "type": "TRADE",
    "size": 475.29,
    "usdcSize": 190.116,
    "transactionHash": "0x4e4362d34d2da9ab6d6f28298db64b63752b9666e43e556cf6b19c97931387f9",
    "price": 0.4,
    "asset": "47268626575751365011669618825003576972992526889448864522694882784694279724041",
    "side": "BUY",
    "outcomeIndex": 1,
    "title": "Will Sabah FK win on 2026-07-21?",
    "slug": "ucl-sf-kps-2026-07-21-sf",
    "eventSlug": "ucl-sf-kps-2026-07-21",
    "outcome": "No",
}


class TestNormalizeActivityRecord(unittest.TestCase):
    def test_maps_every_field_process_trade_needs(self):
        result = normalize_activity_record(REAL_TRADE_RECORD, "0xf0318c32136c2db7fec88b84869aee6a1106c80c")
        self.assertEqual(result["user_address"], "0xf0318c32136c2db7fec88b84869aee6a1106c80c")
        self.assertEqual(result["market_slug"], "ucl-sf-kps-2026-07-21-sf")
        self.assertEqual(result["market_title"], "Will Sabah FK win on 2026-07-21?")
        self.assertEqual(result["outcome"], "No")
        self.assertEqual(result["side"], "BUY")
        self.assertEqual(result["price"], 0.4)
        self.assertEqual(result["size_usd"], 190.116)
        self.assertEqual(result["transaction_hash"], REAL_TRADE_RECORD["transactionHash"])

    def test_size_usd_matches_size_times_price_sanity_check(self):
        # Confirms usdcSize really is the USD notional (size * price), the
        # same quantity bot.py's source_size_usd/price = shares model needs —
        # not some other unrelated figure that happens to also be a number.
        record = REAL_TRADE_RECORD
        self.assertAlmostEqual(record["usdcSize"], record["size"] * record["price"], places=3)

    def test_trade_id_is_stable_across_repeated_calls(self):
        # Same input -> same trade_id every time, required for
        # bot.py's seen_trade_ids dedup to actually work across polls.
        first = normalize_activity_record(REAL_TRADE_RECORD, "0xabc")
        second = normalize_activity_record(REAL_TRADE_RECORD, "0xabc")
        self.assertEqual(first["trade_id"], second["trade_id"])

    def test_trade_id_differs_for_different_fills_sharing_one_transaction_hash(self):
        # The exact multi-fill-per-tx risk this design avoids: two distinct
        # fills (different asset/side) inside the SAME settlement tx must
        # not collapse into one trade_id.
        fill_a = {**REAL_TRADE_RECORD, "asset": "111", "side": "BUY"}
        fill_b = {**REAL_TRADE_RECORD, "asset": "222", "side": "SELL"}
        result_a = normalize_activity_record(fill_a, "0xabc")
        result_b = normalize_activity_record(fill_b, "0xabc")
        self.assertNotEqual(result_a["trade_id"], result_b["trade_id"])

    def test_formatted_timestamp_matches_bullpen_string_shape(self):
        result = normalize_activity_record(REAL_TRADE_RECORD, "0xabc")
        # "YYYY-MM-DD HH:MM:SS UTC" — same shape as bullpen's own timestamp
        # string, confirmed via a live side-by-side bullpen tracker feed call.
        self.assertRegex(result["timestamp"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} UTC$")

    def test_missing_optional_fields_default_to_empty_string_not_none(self):
        sparse = {"transactionHash": "0xabc", "asset": "1", "side": "BUY", "timestamp": 1700000000}
        result = normalize_activity_record(sparse, "0xabc")
        self.assertEqual(result["market_slug"], "")
        self.assertEqual(result["market_title"], "")
        self.assertEqual(result["outcome"], "")


class TestFetchAllWalletsConcurrent(unittest.TestCase):
    def test_one_wallet_failing_does_not_abort_the_batch(self):
        def fake_fetch(wallet_address, limit=100, timeout=10, start=None):
            if wallet_address == "0xBAD":
                raise TimeoutError("simulated network failure")
            return [REAL_TRADE_RECORD]

        with patch("polymarket_data_api.fetch_wallet_trades", side_effect=fake_fetch):
            result = fetch_all_wallets_concurrent(["0xGOOD1", "0xBAD", "0xGOOD2"])

        self.assertEqual(len(result["trades"]), 2)  # the two good wallets' trades
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(result["errors"][0]["wallet_address"], "0xBAD")

    def test_all_wallets_succeeding_returns_no_errors(self):
        with patch("polymarket_data_api.fetch_wallet_trades", return_value=[REAL_TRADE_RECORD]):
            result = fetch_all_wallets_concurrent(["0xA", "0xB"])
        self.assertEqual(len(result["trades"]), 2)
        self.assertEqual(result["errors"], [])

    def test_accepts_an_externally_provided_executor(self):
        # The whole point of make_persistent_executor(): bot.py must be able
        # to pass in one long-lived executor across every poll cycle rather
        # than a fresh one being created (and its connections discarded)
        # every call.
        executor = make_persistent_executor(max_workers=2)
        try:
            with patch("polymarket_data_api.fetch_wallet_trades", return_value=[REAL_TRADE_RECORD]):
                result = fetch_all_wallets_concurrent(["0xA"], executor=executor)
            self.assertEqual(len(result["trades"]), 1)
        finally:
            executor.shutdown()

    def test_forwards_the_live_known_trade_boundary_to_each_wallet_fetch(self):
        known = {"known-trade-id"}
        with patch("polymarket_data_api.fetch_wallet_trades", return_value=[]) as mock_fetch:
            fetch_all_wallets_concurrent(["0xA"], known_trade_ids=known)
        self.assertIs(mock_fetch.call_args.kwargs["known_trade_ids"], known)


class TestStartParam(unittest.TestCase):
    def tearDown(self):
        _thread_local.conn = None

    def test_start_param_included_in_request_path_when_given(self):
        _thread_local.conn = None
        mock_conn = MagicMock()
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.read.return_value = b"[]"
        mock_conn.getresponse.return_value = mock_response
        with patch("http.client.HTTPSConnection", return_value=mock_conn):
            fetch_wallet_trades("0xABC", start=1700000000)
        path_used = mock_conn.request.call_args[0][1]
        self.assertIn("start=1700000000", path_used)

    def test_start_param_omitted_when_not_given(self):
        _thread_local.conn = None
        mock_conn = MagicMock()
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.read.return_value = b"[]"
        mock_conn.getresponse.return_value = mock_response
        with patch("http.client.HTTPSConnection", return_value=mock_conn):
            fetch_wallet_trades("0xABC")
        path_used = mock_conn.request.call_args[0][1]
        self.assertNotIn("start=", path_used)


class TestConnectionReuse(unittest.TestCase):
    def tearDown(self):
        # Don't leak a mock connection into other tests running on this
        # same thread.
        _thread_local.conn = None

    def test_get_connection_returns_the_same_object_on_repeated_calls(self):
        _thread_local.conn = None
        with patch("http.client.HTTPSConnection") as mock_cls:
            mock_cls.return_value = MagicMock()
            first = _get_connection(timeout=10)
            second = _get_connection(timeout=10)
        self.assertIs(first, second)
        mock_cls.assert_called_once()  # only ONE connection actually constructed

    def test_reset_connection_forces_a_fresh_one_on_next_call(self):
        _thread_local.conn = None
        with patch("http.client.HTTPSConnection") as mock_cls:
            mock_cls.side_effect = [MagicMock(), MagicMock()]
            first = _get_connection(timeout=10)
            _reset_connection()
            second = _get_connection(timeout=10)
        self.assertIsNot(first, second)
        self.assertEqual(mock_cls.call_count, 2)


def _mock_response(status, body_obj):
    resp = MagicMock()
    resp.status = status
    resp.read.return_value = json.dumps(body_obj).encode("utf-8")
    return resp


class TestPagination(unittest.TestCase):
    """fetch_wallet_trades() automatic offset-based pagination, added
    2026-07-22 in response to: "if a wallet makes 100 trades in a split
    second, does our fetcher handle pagination correctly to ensure zero
    missed trades?" """

    def tearDown(self):
        _thread_local.conn = None

    def test_stops_after_a_short_page_single_page_case(self):
        _thread_local.conn = None
        mock_conn = MagicMock()
        # Fewer records than `limit` -> exactly one page, no second request.
        mock_conn.getresponse.side_effect = [_mock_response(200, [{"id": 1}, {"id": 2}])]
        with patch("http.client.HTTPSConnection", return_value=mock_conn):
            result = fetch_wallet_trades("0xABC", limit=100)
        self.assertEqual(result, [{"id": 1}, {"id": 2}])
        self.assertEqual(mock_conn.request.call_count, 1)

    def test_follows_a_full_page_with_a_second_request_at_the_next_offset(self):
        _thread_local.conn = None
        mock_conn = MagicMock()
        page1 = [{"id": i} for i in range(3)]   # FULL page (== limit) -> expect a 2nd request
        page2 = [{"id": 99}]                    # short page -> stop here
        mock_conn.getresponse.side_effect = [_mock_response(200, page1), _mock_response(200, page2)]
        with patch("http.client.HTTPSConnection", return_value=mock_conn):
            result = fetch_wallet_trades("0xABC", limit=3)
        self.assertEqual(result, page1 + page2)
        self.assertEqual(mock_conn.request.call_count, 2)
        # Second request's path must ask for the NEXT offset (3), not repeat offset 0.
        second_call_path = mock_conn.request.call_args_list[1][0][1]
        self.assertIn("offset=3", second_call_path)
        first_call_path = mock_conn.request.call_args_list[0][0][1]
        self.assertIn("offset=0", first_call_path)

    def test_stops_at_max_pages_even_if_every_page_is_full(self):
        _thread_local.conn = None
        mock_conn = MagicMock()
        full_page = [{"id": i} for i in range(2)]
        # Always return a FULL page — without a cap this would loop forever.
        mock_conn.getresponse.side_effect = [_mock_response(200, full_page) for _ in range(50)]
        with patch("http.client.HTTPSConnection", return_value=mock_conn), \
             patch("polymarket_data_api.MAX_PAGES_PER_FETCH", 4):
            result = fetch_wallet_trades("0xABC", limit=2)
        self.assertEqual(mock_conn.request.call_count, 4)
        self.assertEqual(len(result), 8)  # 4 pages * 2 records

    def test_known_trade_on_a_full_page_stops_before_older_offsets(self):
        _thread_local.conn = None
        mock_conn = MagicMock()
        page1 = [
            {"transactionHash": "0xNEW", "asset": "1", "side": "BUY", "timestamp": 20},
            {"transactionHash": "0xKNOWN", "asset": "2", "side": "BUY", "timestamp": 10},
        ]
        page2 = [
            {"transactionHash": "0xOLD", "asset": "3", "side": "BUY", "timestamp": 1},
        ]
        mock_conn.getresponse.side_effect = [_mock_response(200, page1), _mock_response(200, page2)]
        known_id = normalize_activity_record(page1[1], "0xABC")["trade_id"]
        with patch("http.client.HTTPSConnection", return_value=mock_conn):
            result = fetch_wallet_trades("0xABC", limit=2, known_trade_ids={known_id})
        self.assertEqual(result, page1)
        self.assertEqual(mock_conn.request.call_count, 1)

    def test_new_trade_burst_keeps_paging_until_the_known_boundary(self):
        _thread_local.conn = None
        mock_conn = MagicMock()
        page1 = [
            {"transactionHash": "0xNEW1", "asset": "1", "side": "BUY", "timestamp": 30},
            {"transactionHash": "0xNEW2", "asset": "2", "side": "BUY", "timestamp": 20},
        ]
        page2 = [
            {"transactionHash": "0xNEW3", "asset": "3", "side": "BUY", "timestamp": 15},
            {"transactionHash": "0xKNOWN", "asset": "4", "side": "BUY", "timestamp": 10},
        ]
        page3 = [
            {"transactionHash": "0xOLD", "asset": "5", "side": "BUY", "timestamp": 1},
        ]
        mock_conn.getresponse.side_effect = [
            _mock_response(200, page1), _mock_response(200, page2), _mock_response(200, page3)
        ]
        known_id = normalize_activity_record(page2[1], "0xABC")["trade_id"]
        with patch("http.client.HTTPSConnection", return_value=mock_conn):
            result = fetch_wallet_trades("0xABC", limit=2, known_trade_ids={known_id})
        self.assertEqual(result, page1 + page2)
        self.assertEqual(mock_conn.request.call_count, 2)


class TestRateLimitBackoff(unittest.TestCase):
    """fetch_wallet_trades()'s HTTP 429/5xx retry-with-backoff, added
    2026-07-22 in response to: "are we fully protected with exponential
    backoff or retry logic if Polymarket's server throttles our requests?" """

    def tearDown(self):
        _thread_local.conn = None

    def test_429_is_retried_and_eventually_succeeds(self):
        _thread_local.conn = None
        mock_conn = MagicMock()
        mock_conn.getresponse.side_effect = [
            _mock_response(429, {"error": "rate limited"}),
            _mock_response(429, {"error": "rate limited"}),
            _mock_response(200, [{"id": 1}]),
        ]
        with patch("http.client.HTTPSConnection", return_value=mock_conn), \
             patch("polymarket_data_api.time.sleep") as mock_sleep:
            result = fetch_wallet_trades("0xABC", limit=100)
        self.assertEqual(result, [{"id": 1}])
        self.assertEqual(mock_conn.request.call_count, 3)
        # Exponential: 1s then 2s (RATE_LIMIT_BACKOFF_BASE_SECONDS=1, doubling).
        mock_sleep.assert_any_call(1.0)
        mock_sleep.assert_any_call(2.0)

    def test_5xx_is_retried_the_same_way_as_429(self):
        _thread_local.conn = None
        mock_conn = MagicMock()
        mock_conn.getresponse.side_effect = [
            _mock_response(503, {"error": "service unavailable"}),
            _mock_response(200, [{"id": 1}]),
        ]
        with patch("http.client.HTTPSConnection", return_value=mock_conn), \
             patch("polymarket_data_api.time.sleep"):
            result = fetch_wallet_trades("0xABC", limit=100)
        self.assertEqual(result, [{"id": 1}])

    def test_gives_up_after_max_retries_and_raises(self):
        _thread_local.conn = None
        mock_conn = MagicMock()
        mock_conn.getresponse.side_effect = [_mock_response(429, {"error": "rate limited"})] * 10
        with patch("http.client.HTTPSConnection", return_value=mock_conn), \
             patch("polymarket_data_api.time.sleep"), \
             patch("polymarket_data_api.RATE_LIMIT_MAX_RETRIES", 2):
            with self.assertRaises(RuntimeError) as ctx:
                fetch_wallet_trades("0xABC", limit=100)
        self.assertIn("429", str(ctx.exception))

    def test_permanent_4xx_is_not_retried_at_all(self):
        _thread_local.conn = None
        mock_conn = MagicMock()
        mock_conn.getresponse.side_effect = [_mock_response(400, {"error": "bad request"})]
        with patch("http.client.HTTPSConnection", return_value=mock_conn), \
             patch("polymarket_data_api.time.sleep") as mock_sleep:
            with self.assertRaises(RuntimeError) as ctx:
                fetch_wallet_trades("0xABC", limit=100)
        self.assertIn("400", str(ctx.exception))
        mock_sleep.assert_not_called()
        mock_conn.request.assert_called_once()  # no retry attempted at all


if __name__ == "__main__":
    unittest.main()
