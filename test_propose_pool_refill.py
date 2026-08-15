import unittest
from unittest.mock import patch

import propose_pool_refill as refill


class ProposePoolRefillOfficialDataTest(unittest.TestCase):
    def test_recent_trade_evidence_uses_one_bounded_official_page(self):
        rows = [{"price": 0.04, "side": "SELL", "slug": "m"}]
        with patch.object(refill, "fetch_wallet_trades", return_value=rows) as fetch:
            self.assertEqual(refill.fetch_recent_trades("0xabc", limit=17), rows)
        fetch.assert_called_once_with("0xabc", limit=17, max_pages=1)

    def test_fetch_failure_is_unknown_not_false_evidence(self):
        with patch.object(refill, "fetch_wallet_trades", side_effect=RuntimeError("offline")):
            self.assertIsNone(refill.fetch_recent_trades("0xabc", limit=17))


if __name__ == "__main__":
    unittest.main()
