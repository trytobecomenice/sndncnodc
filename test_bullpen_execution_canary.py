import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import call, patch

import bullpen_execution_canary as canary


class BullpenExecutionCanaryTest(unittest.TestCase):
    def test_uses_read_only_status_and_portfolio_and_emits_success(self):
        output = io.StringIO()
        responses = [
            {"health": {"logged_in": True}},
            {"balances": []},
        ]
        with patch.object(canary, "run_bullpen_json", side_effect=responses) as run:
            with redirect_stdout(output):
                code = canary.run_canary()
        self.assertEqual(code, 0)
        self.assertEqual(
            run.call_args_list,
            [
                call(
                    ["--read-only", "status"], retries=3, retry_delay=1.0, timeout=20
                ),
                call(
                    ["--read-only", "portfolio"], retries=3, retry_delay=1.0, timeout=20
                ),
            ],
        )
        self.assertTrue(json.loads(output.getvalue())["ok"])

    def test_successful_status_without_login_fails_before_portfolio(self):
        output = io.StringIO()
        with patch.object(
            canary, "run_bullpen_json", return_value={"health": {"logged_in": False}}
        ) as run:
            with redirect_stdout(output):
                code = canary.run_canary()
        self.assertEqual(code, 1)
        self.assertEqual(run.call_count, 1)
        self.assertIn("bullpen login", json.loads(output.getvalue())["error"])

    def test_failure_is_nonzero_and_contains_no_secret_payload(self):
        output = io.StringIO()
        with patch.object(canary, "run_bullpen_json", side_effect=RuntimeError("auth expired")):
            with redirect_stdout(output):
                code = canary.run_canary()
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output.getvalue())["error"], "auth expired")


if __name__ == "__main__":
    unittest.main()
