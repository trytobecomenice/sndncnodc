#!/usr/bin/env python3
"""Unit tests for bullpen_client.py's exit-code classification — specifically
that a `bullpen` exit code 2 ("Authentication failure", per `bullpen --help`)
raises the distinct BullpenAuthError rather than a generic RuntimeError. This
is the exact fact bot.py's fetch_feed_with_auth_recovery() halt/recover logic
depends on (2026-07-21 fix for the silent-error-storm-on-expired-session
incident) — a wrong classification here would silently break that fix.

Run: python3 -m unittest test_bullpen_client -v
"""

import subprocess
import unittest
from unittest.mock import patch

from bullpen_client import BullpenAuthError, _run_bullpen_json_once, extract_filled_shares


def _fake_result(returncode, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=["bullpen"], returncode=returncode, stdout=stdout, stderr=stderr)


class TestExitCodeClassification(unittest.TestCase):
    def test_exit_code_2_raises_bullpen_auth_error(self):
        with patch("subprocess.run", return_value=_fake_result(2, stderr="Session expired. Run: bullpen login")):
            with self.assertRaises(BullpenAuthError) as ctx:
                _run_bullpen_json_once(["tracker", "feed"])
        self.assertIn("Session expired", str(ctx.exception))

    def test_exit_code_1_raises_plain_runtime_error_not_auth_error(self):
        with patch("subprocess.run", return_value=_fake_result(1, stderr="some other failure")):
            with self.assertRaises(RuntimeError) as ctx:
                _run_bullpen_json_once(["tracker", "feed"])
        self.assertNotIsInstance(ctx.exception, BullpenAuthError)

    def test_exit_code_0_with_valid_json_returns_data(self):
        with patch("subprocess.run", return_value=_fake_result(0, stdout='{"trades": []}')):
            data = _run_bullpen_json_once(["tracker", "feed"])
        self.assertEqual(data, {"trades": []})

    def test_bullpen_auth_error_is_a_runtime_error_subclass(self):
        # Confirms callers using a bare `except RuntimeError` (e.g. the retry
        # loop in run_bullpen_json) still catch it — only bot.py's explicit
        # `except BullpenAuthError` branch needs to distinguish it.
        self.assertTrue(issubclass(BullpenAuthError, RuntimeError))


class TestExtractFilledShares(unittest.TestCase):
    """2026-07-26, entry-side FAK integration — specifically must
    distinguish a real, present numeric 0 (genuinely filled nothing) from a
    response shape that just doesn't tell us (None), since a caller falling
    back to a full-budget assumption on ambiguous responses must not do so
    when we actually know the true (possibly zero) fill size."""

    def test_reads_first_matching_key(self):
        self.assertEqual(extract_filled_shares({"filled_shares": 12.5}), 12.5)

    def test_falls_through_key_candidates_in_order(self):
        self.assertEqual(extract_filled_shares({"shares": 7.0}), 7.0)

    def test_genuine_zero_fill_is_distinguishable_from_missing(self):
        self.assertEqual(extract_filled_shares({"filled_shares": 0}), 0.0)

    def test_missing_field_returns_none_not_zero(self):
        self.assertIsNone(extract_filled_shares({"status": "MATCHED"}))

    def test_non_numeric_value_ignored(self):
        self.assertIsNone(extract_filled_shares({"filled_shares": "lots"}))


if __name__ == "__main__":
    unittest.main()
