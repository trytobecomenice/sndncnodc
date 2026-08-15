import json
import unittest
from pathlib import Path

from bullpen_client import (
    extract_fill_price,
    extract_filled_shares,
    extract_order_id,
    extract_order_status,
    require_filled,
)


class BullpenContractVectorTest(unittest.TestCase):
    def test_shared_vectors(self):
        vectors = json.loads((Path(__file__).parent / "testdata/bullpen_execution_contract_vectors.json").read_text())
        for vector in vectors:
            with self.subTest(vector["name"]):
                response = vector["response"]
                try:
                    require_filled(response, vector["name"])
                    filled = True
                except RuntimeError:
                    filled = False
                self.assertEqual(filled, vector["filled"])
                self.assertEqual(extract_fill_price(response), vector["fill_price"])
                self.assertEqual(extract_filled_shares(response), vector["filled_shares"])
                self.assertEqual(extract_order_id(response), vector["order_id"])
                self.assertEqual(extract_order_status(response), vector["order_status"])


if __name__ == "__main__":
    unittest.main()
