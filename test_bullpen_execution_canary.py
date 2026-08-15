import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import bullpen_execution_canary as canary


class BullpenExecutionCanaryTest(unittest.TestCase):
    def test_uses_status_only_and_emits_machine_readable_success(self):
        output = io.StringIO()
        with patch.object(canary, "run_bullpen_json", return_value={"authenticated": True}) as run:
            with redirect_stdout(output):
                code = canary.run_canary()
        self.assertEqual(code, 0)
        run.assert_called_once_with(["status"], retries=3, retry_delay=1.0, timeout=20)
        self.assertTrue(json.loads(output.getvalue())["ok"])

    def test_failure_is_nonzero_and_contains_no_secret_payload(self):
        output = io.StringIO()
        with patch.object(canary, "run_bullpen_json", side_effect=RuntimeError("auth expired")):
            with redirect_stdout(output):
                code = canary.run_canary()
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output.getvalue())["error"], "auth expired")


if __name__ == "__main__":
    unittest.main()
