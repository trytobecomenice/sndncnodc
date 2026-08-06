#!/usr/bin/env python3
import unittest

from reconcile_paper_trade_events import allocate_events


class TestEventAllocation(unittest.TestCase):
    def test_unique_interval_allocates_partial_events_to_one_lot(self):
        trades = [{"id": "a", "wallet_address": "w", "market_slug": "m", "outcome": "Yes",
                   "opened_at": 10, "closed_at": 20, "allocation_end_at": 20}]
        events = [
            {"id": "e1", "trader_address": "w", "market_slug": "m", "outcome": "Yes",
             "timestamp": 15, "pnl_usd": 1},
            {"id": "e2", "trader_address": "w", "market_slug": "m", "outcome": "Yes",
             "timestamp": 20, "pnl_usd": 2},
        ]
        allocated, unmatched, ambiguous = allocate_events(trades, events)
        self.assertEqual([row["id"] for row in allocated["a"]], ["e1", "e2"])
        self.assertFalse(unmatched)
        self.assertFalse(ambiguous)

    def test_no_match_and_overlapping_lots_are_never_forced(self):
        trades = [
            {"id": "a", "wallet_address": "w", "market_slug": "m", "outcome": "Yes",
             "opened_at": 10, "closed_at": 20, "allocation_end_at": 20},
            {"id": "b", "wallet_address": "w", "market_slug": "m", "outcome": "Yes",
             "opened_at": 15, "closed_at": 25, "allocation_end_at": 25},
        ]
        events = [
            {"id": "amb", "trader_address": "w", "market_slug": "m", "outcome": "Yes",
             "timestamp": 17, "pnl_usd": 1},
            {"id": "none", "trader_address": "x", "market_slug": "m", "outcome": "Yes",
             "timestamp": 17, "pnl_usd": 1},
        ]
        allocated, unmatched, ambiguous = allocate_events(trades, events)
        self.assertFalse(allocated)
        self.assertEqual([row["id"] for row in unmatched], ["none"])
        self.assertEqual(ambiguous[0]["candidate_trade_ids"], ["a", "b"])


if __name__ == "__main__":
    unittest.main()
