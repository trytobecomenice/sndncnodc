#!/usr/bin/env python3
import unittest

from reconcile_paper_trade_events import allocate_events


class TestEventAllocation(unittest.TestCase):
    def test_unique_interval_allocates_partial_events_to_one_lot(self):
        trades = [{"id": "a", "strategy": "bot_filtered", "wallet_address": "w", "market_slug": "m", "outcome": "Yes",
                   "opened_at": 10, "closed_at": 20, "allocation_end_at": 20}]
        events = [
            {"id": "e1", "strategy": "bot_filtered", "trader_address": "w", "market_slug": "m", "outcome": "Yes",
             "timestamp": 15, "pnl_usd": 1},
            {"id": "e2", "strategy": "bot_filtered", "trader_address": "w", "market_slug": "m", "outcome": "Yes",
             "timestamp": 20, "pnl_usd": 2},
        ]
        allocated, unmatched, ambiguous = allocate_events(trades, events)
        self.assertEqual([row["id"] for row in allocated["a"]], ["e1", "e2"])
        self.assertFalse(unmatched)
        self.assertFalse(ambiguous)

    def test_no_match_and_overlapping_lots_are_never_forced(self):
        trades = [
            {"id": "a", "strategy": "bot_filtered", "wallet_address": "w", "market_slug": "m", "outcome": "Yes",
             "opened_at": 10, "closed_at": 20, "allocation_end_at": 20},
            {"id": "b", "strategy": "bot_filtered", "wallet_address": "w", "market_slug": "m", "outcome": "Yes",
             "opened_at": 15, "closed_at": 25, "allocation_end_at": 25},
        ]
        events = [
            {"id": "amb", "strategy": "bot_filtered", "trader_address": "w", "market_slug": "m", "outcome": "Yes",
             "timestamp": 17, "pnl_usd": 1},
            {"id": "none", "strategy": "bot_filtered", "trader_address": "x", "market_slug": "m", "outcome": "Yes",
             "timestamp": 17, "pnl_usd": 1},
        ]
        allocated, unmatched, ambiguous = allocate_events(trades, events)
        self.assertFalse(allocated)
        self.assertEqual([row["id"] for row in unmatched], ["none"])
        self.assertEqual(ambiguous[0]["candidate_trade_ids"], ["a", "b"])

    def test_partial_event_payload_fields_survive_report_allocation(self):
        trades = [{"id": "a", "strategy": "bot_filtered", "wallet_address": "w", "market_slug": "m", "outcome": "Yes",
                   "opened_at": 10, "closed_at": 20, "allocation_end_at": 20}]
        events = [{"id": "e", "strategy": "bot_filtered", "trader_address": "w", "market_slug": "m", "outcome": "Yes",
                   "timestamp": 15, "event_type": "paper_sell", "pnl_usd": 1.5,
                   "cost_basis_usd": 2.0}]
        allocated, unmatched, ambiguous = allocate_events(trades, events)
        self.assertEqual(allocated["a"][0]["cost_basis_usd"], 2.0)
        self.assertFalse(unmatched)
        self.assertFalse(ambiguous)

    def test_identical_real_and_shadow_lots_are_separated_by_strategy(self):
        base = {"wallet_address": "w", "market_slug": "m", "outcome": "Yes",
                "opened_at": 10, "closed_at": 20, "allocation_end_at": 20}
        trades = [
            {**base, "id": "real", "strategy": "bot_filtered"},
            {**base, "id": "rehab", "strategy": "shadow_rehab"},
        ]
        events = [{"id": "e", "strategy": "shadow_rehab", "trader_address": "w",
                   "market_slug": "m", "outcome": "Yes", "timestamp": 15, "pnl_usd": 1}]
        allocated, unmatched, ambiguous = allocate_events(trades, events)
        self.assertEqual([row["id"] for row in allocated["rehab"]], ["e"])
        self.assertNotIn("real", allocated)
        self.assertFalse(unmatched)
        self.assertFalse(ambiguous)


if __name__ == "__main__":
    unittest.main()
