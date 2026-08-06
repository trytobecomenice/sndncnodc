#!/usr/bin/env python3

import json
from pathlib import Path
import tempfile
import unittest

from phase0_autopsy import normalize_journals
from research.autopsy_features import (
    annotate_open_lot_topology_risk,
    cluster_signals,
    cohort_summary,
    cutoff_mtm_capability,
    open_lots_by_wallet_tier,
    read_normalized_rows,
    session_label,
)
from tools.autopsy_stress_tester import run_stress_test
from tools.build_event_graph import (
    build_event_graph, compare_snapshots, serialize_graph,
)
from tools.mock_data_generator import write_hostile_journal
from research.virtual_matching_engine import (
    simulate_cash_buy, simulate_journals, simulate_share_sell,
)
from research.validate_matching_engine import validate_cases
from research.generate_24h_postmortem_report import generate_report


class TestHostileAutopsy(unittest.TestCase):
    def test_full_stress_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run_stress_test(tmp)
        self.assertEqual(result["status"], "PASS")
        self.assertTrue(all(result["checks"].values()))

    def test_cluster_is_one_sample_but_wallet_signals_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "hostile.jsonl"
            normalized = Path(tmp) / "normalized.csv"
            write_hostile_journal(journal)
            normalize_journals([journal], normalized)
            rows = list(read_normalized_rows(normalized))
        assignments, clusters = cluster_signals(rows, window_ms=2_000)
        cluster = next(item for item in clusters if item["market_slug"] == "cluster-market")
        self.assertEqual(cluster["signal_count"], 5)
        self.assertEqual(cluster["wallet_count"], 5)
        self.assertEqual(len({assignments[f"stress-cluster-{i}"] for i in range(5)}), 1)
        cohort = cohort_summary(rows, assignments)
        cluster_wallet_rows = [item for item in cohort if item["wallet"].startswith("0xcluster")]
        self.assertEqual(sum(item["accepted_signal_count"] for item in cluster_wallet_rows), 5)

    def test_cluster_maximum_diameter_breaks_transitive_chain(self):
        rows = [
            {
                "signal_event_id": f"signal-{index}", "wallet": f"0x{index}",
                "market_slug": "market", "side": "BUY",
                "first_local_seen_timestamp_ms": timestamp,
            }
            for index, timestamp in enumerate((0, 2_900, 5_800, 8_700))
        ]
        _assignments, clusters = cluster_signals(
            rows, window_ms=3_000, max_diameter_ms=3_000
        )
        self.assertEqual([item["signal_count"] for item in clusters], [2, 2])
        self.assertTrue(all(item["actual_diameter_ms"] <= 3_000 for item in clusters))

    def test_open_lots_report_cost_and_age_without_fake_mtm(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "hostile.jsonl"
            write_hostile_journal(journal, base_timestamp_ms=1_000_000)
            summaries, details = open_lots_by_wallet_tier(
                [journal], cutoff_timestamp_ms=1_000_000 + 4 * 3_600_000
            )
        self.assertEqual(summaries[("0xcluster0", 3)]["known_cost_basis_micros"], 3_000_000)
        self.assertGreaterEqual(summaries[("0xcluster0", 3)]["oldest_age_hours"], 1)
        self.assertEqual(
            next(item for item in details if item["wallet"] == "0xcluster0")["valuation_status"],
            "COST_AND_AGE_ONLY_NO_MTM",
        )

    def test_session_stratum_is_configurable(self):
        # 2027-01-15 15:00:00 UTC = 10:00 New York.
        timestamp_ms = 1_800_000_000_000
        self.assertEqual(session_label(timestamp_ms, "UTC", "00:00", "23:59"), "US_HOURS")
        self.assertEqual(session_label(timestamp_ms, "UTC", "00:00", "00:01"), "NON_US_HOURS")

    def test_cutoff_mtm_is_not_fabricated(self):
        self.assertEqual(
            cutoff_mtm_capability()["status"], "UNAVAILABLE_FROM_CURRENT_PHASE0_SCHEMA"
        )

    def test_topology_risk_annotation_does_not_invent_haircut(self):
        before = {
            "topology": {
                "graph": {"last_synced_epoch_ms": 100},
                "nodes": [
                    {"id": "event:e", "node_type": "event",
                     "market_membership_hash": "old", "observed_market_count": 1},
                    {"id": "market:a", "node_type": "market", "slug": "market-a"},
                ],
                "edges": [{"source": "event:e", "target": "market:a",
                           "relation": "contains_market"}],
            },
        }
        after = {
            "topology": {
                "graph": {"last_synced_epoch_ms": 200},
                "nodes": [
                    {"id": "event:e", "node_type": "event",
                     "market_membership_hash": "new", "observed_market_count": 2,
                     "completeness_status": "OPEN_MUTABLE"},
                    {"id": "market:a", "node_type": "market", "slug": "market-a"},
                    {"id": "market:b", "node_type": "market", "slug": "market-b"},
                ],
                "edges": [{"source": "event:e", "target": "market:a",
                           "relation": "contains_market"}],
            },
        }
        annotated = annotate_open_lot_topology_risk(
            [{"market_slug": "market-a", "opened_timestamp_ms": 50}], after, before
        )
        self.assertEqual(annotated[0]["topology_risk_status"], "TOPOLOGY_RISK")
        self.assertIsNone(annotated[0]["uncertainty_haircut_fraction"])


class TestEventGraph(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import networkx  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("offline graph dependency is not installed")

    def test_standard_event_does_not_invent_cross_market_exclusivity(self):
        events = [{
            "id": "event", "slug": "event", "negRisk": False,
            "markets": [
                {"id": "a", "outcomes": '["Yes","No"]'},
                {"id": "b", "outcomes": '["Yes","No"]'},
            ],
        }]
        graph, constraints, audit = build_event_graph(events)
        self.assertEqual(audit["standard_binary_market_count"], 2)
        self.assertFalse(any(
            item["constraint_type"] == "neg_risk_exactly_one_market_yes"
            for item in constraints
        ))
        self.assertEqual(len(graph.nodes), 7)

    def test_explicit_negative_risk_adds_one_guarded_constraint(self):
        events = [{
            "id": "event", "negRisk": True, "negRiskMarketID": "group",
            "markets": [
                {"id": "a", "outcomes": ["Yes", "No"]},
                {"id": "b", "outcomes": ["Yes", "No"], "negRiskOther": True},
            ],
        }]
        graph, constraints, audit = build_event_graph(events)
        cross = [
            item for item in constraints
            if item["constraint_type"] == "neg_risk_exactly_one_market_yes"
        ]
        self.assertEqual(len(cross), 1)
        self.assertTrue(cross[0]["augmented"])
        payload = serialize_graph(graph, constraints, audit)
        self.assertEqual(payload["audit"]["negative_risk_event_count"], 1)

    def test_mutable_snapshot_detects_added_market(self):
        before_events = [{
            "id": "event", "active": True, "closed": False, "negRisk": True,
            "markets": [{"id": "a", "outcomes": ["Yes", "No"]}],
        }]
        after_events = [{
            "id": "event", "active": True, "closed": False, "negRisk": True,
            "markets": [
                {"id": "a", "outcomes": ["Yes", "No"]},
                {"id": "b", "outcomes": ["Yes", "No"]},
            ],
        }]
        before = serialize_graph(*build_event_graph(before_events, synced_at_epoch_ms=1))
        after = serialize_graph(*build_event_graph(after_events, synced_at_epoch_ms=2))
        change = compare_snapshots(before, after)
        self.assertTrue(change["topology_changed"])
        self.assertEqual(len(change["events_with_changed_market_membership"]), 1)
        event = next(
            node for node in after["topology"]["nodes"] if node["id"] == "event:event"
        )
        self.assertEqual(event["completeness_status"], "OPEN_MUTABLE")

    def test_open_lot_crossing_membership_change_is_flagged_without_fake_haircut(self):
        before_events = [{
            "id": "event", "active": True, "closed": False,
            "markets": [{"id": "a", "slug": "market-a", "outcomes": ["Yes", "No"]}],
        }]
        after_events = [{
            "id": "event", "active": True, "closed": False,
            "markets": [
                {"id": "a", "slug": "market-a", "outcomes": ["Yes", "No"]},
                {"id": "b", "slug": "market-b", "outcomes": ["Yes", "No"]},
            ],
        }]
        before = serialize_graph(*build_event_graph(before_events, synced_at_epoch_ms=100))
        after = serialize_graph(*build_event_graph(after_events, synced_at_epoch_ms=200))
        lots = [{"market_slug": "market-a", "opened_timestamp_ms": 50}]
        annotated = annotate_open_lot_topology_risk(lots, after, before)
        self.assertEqual(annotated[0]["topology_risk_status"], "TOPOLOGY_RISK")
        self.assertIsNone(annotated[0]["uncertainty_haircut_fraction"])
        self.assertIn("BLOCKED", annotated[0]["uncertainty_haircut_status"])


class TestVirtualMatchingEngine(unittest.TestCase):
    def setUp(self):
        self.book = {
            "bids": [[0.49, 4], [0.40, 10]],
            "asks": [[0.50, 4], [0.60, 10]],
        }

    def test_fak_walks_levels_and_returns_partial_fill(self):
        result = simulate_cash_buy(self.book, 10, "FAK")
        self.assertEqual(result["status"], "PARTIAL_FILL")
        self.assertEqual(result["levels_consumed"], 2)
        self.assertEqual(result["filled_cash_micros"], 8_000_000)
        self.assertEqual(result["unfilled_cash_micros"], 2_000_000)
        self.assertGreater(result["vwap_price_micros"], result["best_price_micros"])

    def test_fok_rejects_entire_order_when_displayed_depth_is_short(self):
        result = simulate_cash_buy(self.book, 10, "FOK")
        self.assertEqual(result["status"], "REJECTED_FOK_INSUFFICIENT_DEPTH")
        self.assertEqual(result["filled_cash_micros"], 0)
        self.assertEqual(result["fill_ratio_ppm"], 0)
        self.assertEqual(result["preview_fill_before_fok_kill"]["filled_cash_micros"], 8_000_000)

    def test_sell_walk_has_side_adjusted_slippage(self):
        result = simulate_share_sell(self.book, 6, "FAK")
        self.assertEqual(result["status"], "FULL_FILL")
        self.assertEqual(result["levels_consumed"], 2)
        self.assertGreater(result["side_adjusted_slippage_micros"], 0)

    def test_empty_book_is_explicit_no_fill(self):
        result = simulate_cash_buy({"asks": []}, 3, "FAK")
        self.assertEqual(result["status"], "NO_FILL")
        self.assertIsNone(result["vwap_price_micros"])

    def test_nonlinear_fee_is_calculated_per_level(self):
        book = {"asks": [[0.50, 10], [0.90, 10]]}
        result = simulate_cash_buy(book, 14, "FAK", fee_rate="0.05")
        # 10 * .5 * .5 * .05 = .125; 10 * .9 * .1 * .05 = .045.
        self.assertEqual(result["taker_fee_cash_micros"], 170_000)
        self.assertEqual(result["total_cash_debit_micros"], 14_170_000)
        self.assertEqual(result["cash_budget_mode"], "ORDER_NOTIONAL")

    def test_all_in_budget_embeds_marginal_fee_and_never_overspends(self):
        result = simulate_cash_buy(
            {"asks": [[0.50, 100]]}, 10, "FAK", fee_rate="0.05",
            cash_budget_mode="ALL_IN_CAPITAL",
        )
        self.assertLessEqual(result["total_cash_debit_micros"], 10_000_000)
        self.assertGreater(result["taker_fee_cash_micros"], 0)
        self.assertLess(result["filled_cash_micros"], 10_000_000)

    def test_fee_dust_is_explicitly_truncated_to_documented_precision(self):
        result = simulate_share_sell(
            {"bids": [[0.50, "0.0001"]]}, "0.0001", "FAK", fee_rate="0.05"
        )
        self.assertEqual(result["taker_fee_cash_micros"], 0)
        self.assertEqual(result["fee_precision_micros"], 10)
        self.assertIn("floor_fee", result["rounding_policy"])

    def test_journal_runner_censors_unknown_delayed_book(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "hostile.jsonl"
            write_hostile_journal(journal)
            rows = list(simulate_journals([journal], tiers_usd=(3,), order_type="FAK"))
        ghost = next(
            row for row in rows
            if row["signal_event_id"] == "stress-ghost"
            and row["observation_delay_ms"] == 100
        )
        self.assertFalse(ghost["causal_valid"])
        self.assertEqual(ghost["simulation_status"], "CAUSAL_BOOK_UNAVAILABLE")
        self.assertIsNone(ghost["match_result"])


class TestMatchingValidation(unittest.TestCase):
    def test_trade_tape_cannot_authorize_engine(self):
        report = validate_cases([{
            "case_id": "tape", "evidence_type": "PUBLIC_TRADE_TAPE_1S",
        }])
        self.assertEqual(report["status"], "FAIL_CLOSED")
        self.assertEqual(report["eligible_case_count"], 0)

    def test_attributable_golden_vector_can_pass(self):
        report = validate_cases([{
            "case_id": "golden", "evidence_type": "VENUE_GOLDEN_VECTOR",
            "book": {"asks": [[0.5, 6]]},
            "request": {
                "side": "BUY", "cash_usd": 3, "order_type": "FOK",
                "fee_rate": "0.05",
            },
            "observed": {
                "filled_cash_micros": 3_000_000,
                "filled_shares_micros": 6_000_000,
                "vwap_price_micros": 500_000,
                "taker_fee_cash_micros": 75_000,
            },
        }])
        self.assertEqual(report["status"], "PASS")


class TestPostmortemReport(unittest.TestCase):
    def test_mock_report_is_explicit_about_missing_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "hostile.jsonl"
            write_hostile_journal(journal)
            report = generate_report(
                [journal], Path(tmp) / "report", required_hours=24,
                allow_incomplete=True, write_plot=False,
            )
        self.assertEqual(report["status"], "PRELIMINARY_INCOMPLETE_WINDOW")
        self.assertEqual(
            report["capabilities"]["brier_and_log_loss"]["status"], "UNAVAILABLE"
        )
        utility = report["capabilities"]["economic_utility_t_minus_1s_to_t_plus_5m"]
        self.assertEqual(utility["status"], "UNAVAILABLE")
        self.assertEqual(utility["missing_required_checkpoints_ms"], [-1000, 300000])
        self.assertEqual(
            {item["tier_usd"] for item in report["s3_s5_displayed_fill_survival_and_markout"]},
            {3, 5},
        )
        self.assertEqual(
            {item["side"] for item in report["s3_s5_displayed_fill_survival_and_markout"]},
            {"BUY", "SELL"},
        )

    def test_incomplete_window_fails_closed_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "hostile.jsonl"
            write_hostile_journal(journal)
            report = generate_report(
                [journal], Path(tmp) / "report", required_hours=24,
                write_plot=False,
            )
        self.assertEqual(report["status"], "FAIL_CLOSED_INCOMPLETE_WINDOW")

    def test_mock_24h_window_becomes_ready_without_inventing_metrics(self):
        base_ms = 1_800_000_000_000
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "hostile.jsonl"
            write_hostile_journal(journal, base_timestamp_ms=base_ms)
            with journal.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({
                    "event_type": "poll_cycle",
                    "timestamp_ms": base_ms + 24 * 3_600_000 + 1,
                }) + "\n")
            report = generate_report(
                [journal], Path(tmp) / "report", required_hours=24,
                write_plot=False,
            )
        self.assertEqual(report["status"], "READY_FOR_REVIEW")
        self.assertTrue(report["window_audit"]["window_complete"])
        self.assertEqual(report["capabilities"]["mtbt"]["status"], "UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
