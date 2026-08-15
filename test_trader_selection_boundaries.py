import re
import unittest
from pathlib import Path


ROOT = Path(__file__).parent


class TraderSelectionBoundaryTest(unittest.TestCase):
    DECISION_TS_FILES = (
        "packages/copy-trading/src/scanLeaderboard.ts",
        "packages/copy-trading/src/scoreWallets.ts",
        "packages/copy-trading/src/scoreWalletCategories.ts",
        "packages/copy-trading/src/discoverCategorySpecialists.ts",
        "packages/copy-trading/src/walletApprovalQueue.ts",
    )

    def test_every_selection_and_approval_module_cannot_import_bullpen(self):
        for relative in self.DECISION_TS_FILES:
            source = (ROOT / relative).read_text()
            # Match the package/module identity, not a callable name, so an
            # aliased import cannot evade the boundary.
            self.assertNotRegex(source, r'(?m)^\s*import\b[^;]*["\']@copybot/bullpen-client["\']', relative)

        refill = (ROOT / "propose_pool_refill.py").read_text()
        self.assertNotRegex(refill, r'(?m)^\s*(?:from|import)\s+bullpen_client\b')
        self.assertIn("from polymarket_data_api import fetch_wallet_trades", refill)

    def test_scorer_update_set_cannot_write_approved_roster_columns(self):
        source = (ROOT / "packages/copy-trading/src/scoreWallets.ts").read_text()
        marker = ".onConflictDoUpdate({"
        blocks = source.split(marker)[1:]
        self.assertGreater(len(blocks), 0)
        for index, tail in enumerate(blocks, 1):
            update_block = tail.split("});", 1)[0]
            self.assertIsNone(re.search(r"\bstatus\s*:", update_block), f"upsert block {index}")
            self.assertNotIn("statusReason:", update_block, f"upsert block {index}")
            self.assertNotIn("statusChangedAt:", update_block, f"upsert block {index}")

    def test_derived_metric_gate_key_has_an_explicit_writer_allowlist(self):
        writers = {}
        src = ROOT / "packages/copy-trading/src"
        for path in src.glob("*.ts"):
            if path.name.endswith(".test.ts"):
                continue
            matches = re.findall(r'derivedMetricsSource\s*:\s*["\']([^"\']+)["\']', path.read_text())
            if matches:
                writers[path.name] = matches
        self.assertEqual(writers, {
            "scoreWalletCategories.ts": ["polymarket_official_raw_category"],
            "scoreWallets.ts": ["legacy_unverified"],
        })

    def test_legacy_bullpen_tracker_feed_code_is_absent_from_bot(self):
        source = (ROOT / "bot.py").read_text()
        self.assertNotIn("def fetch_feed_with_auth_recovery", source)
        self.assertNotRegex(source, r'\[\s*["\']tracker["\']\s*,\s*["\']feed["\']')

    def test_bullpen_canary_has_no_database_or_order_mutation_path(self):
        source = (ROOT / "bullpen_execution_canary.py").read_text()
        self.assertNotIn("import db", source)
        self.assertNotIn('["polymarket",', source)
        self.assertIn('["status"]', source)


if __name__ == "__main__":
    unittest.main()
