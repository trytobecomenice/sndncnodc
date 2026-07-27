#!/usr/bin/env python3
"""Unit tests for token_sync_worker.py: extract_token_rows() (pure
parsing/validation logic) and main()'s scheduling-loop resilience
(2026-07-25 — turned into a long-running daemon, same pattern as
wss_listener.py's reconnect loop, verified the same way: a simulated-failure
harness, not a live run).

Run: python3 -m unittest test_token_sync_worker -v
"""

import asyncio
import json
import unittest
from unittest.mock import patch

import token_sync_worker as tw


class TestExtractTokenRows(unittest.TestCase):
    def test_binary_market_produces_two_rows(self):
        market = {
            "slug": "some-market",
            "outcomes": '["Yes", "No"]',
            "clobTokenIds": '["111", "222"]',
        }
        rows = tw.extract_token_rows(market)
        self.assertEqual(rows, [("111", "some-market", "Yes"), ("222", "some-market", "No")])

    def test_token_id_kept_as_string_never_cast_to_int(self):
        market = {"slug": "m", "outcomes": '["Yes"]', "clobTokenIds": '["12345678901234567890"]'}
        rows = tw.extract_token_rows(market)
        self.assertIsInstance(rows[0][0], str)
        self.assertEqual(rows[0][0], "12345678901234567890")

    def test_missing_slug_returns_empty(self):
        market = {"outcomes": '["Yes", "No"]', "clobTokenIds": '["111", "222"]'}
        self.assertEqual(tw.extract_token_rows(market), [])

    def test_missing_clob_token_ids_returns_empty(self):
        market = {"slug": "m", "outcomes": '["Yes", "No"]'}
        self.assertEqual(tw.extract_token_rows(market), [])

    def test_length_mismatch_returns_empty_not_a_wrong_pairing(self):
        market = {"slug": "m", "outcomes": '["Yes", "No"]', "clobTokenIds": '["111"]'}
        self.assertEqual(tw.extract_token_rows(market), [])

    def test_malformed_json_returns_empty_not_an_exception(self):
        market = {"slug": "m", "outcomes": "not json", "clobTokenIds": '["111"]'}
        self.assertEqual(tw.extract_token_rows(market), [])

    def test_already_parsed_list_is_accepted_not_just_a_json_string(self):
        market = {"slug": "m", "outcomes": ["Yes", "No"], "clobTokenIds": ["111", "222"]}
        rows = tw.extract_token_rows(market)
        self.assertEqual(len(rows), 2)


class TestMainSchedulingLoop(unittest.TestCase):
    """Same simulated-failure-harness verification approach as
    wss_listener.py's reconnect loop test."""

    def test_survives_repeated_sync_failures_and_reschedules(self):
        attempts = []

        async def fake_run_one_sync():
            attempts.append(1)
            if len(attempts) >= 3:
                raise KeyboardInterrupt("stop test")
            raise RuntimeError("simulated sync failure")

        async def fake_sleep(_seconds):
            return None

        async def go():
            with patch.object(tw, "run_one_sync", fake_run_one_sync), \
                 patch.object(tw, "SYNC_INTERVAL_SECONDS", 0.01), \
                 patch("asyncio.sleep", fake_sleep):
                try:
                    await tw.main()
                except KeyboardInterrupt:
                    pass

        asyncio.run(go())
        self.assertEqual(len(attempts), 3)


if __name__ == "__main__":
    unittest.main()
