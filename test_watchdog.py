#!/usr/bin/env python3
"""Unit tests for watchdog.py (2026-07-31) — bot.py process supervision,
built after a real ~2.5h silent outage this session.

Run: python3 -m unittest test_watchdog -v
"""

import os
import tempfile
import unittest
from unittest.mock import patch

import config
import watchdog


class TestWatchdog(unittest.TestCase):
    def setUp(self):
        fd, self._log_path = tempfile.mkstemp(suffix=".log")
        os.close(fd)
        self._log_patcher = patch.object(watchdog, "WATCHDOG_LOG_PATH", self._log_path)
        self._log_patcher.start()
        self._pause_patcher = patch.object(
            watchdog, "WATCHDOG_PAUSE_PATH", os.path.join(tempfile.gettempdir(), "nonexistent-pause-sentinel")
        )
        self._pause_patcher.start()

    def tearDown(self):
        self._log_patcher.stop()
        self._pause_patcher.stop()
        os.remove(self._log_path)

    def test_noop_when_paused(self):
        with tempfile.NamedTemporaryFile() as pause_file:
            with patch.object(watchdog, "WATCHDOG_PAUSE_PATH", pause_file.name), \
                 patch("watchdog.dashboard.bot_pid") as mock_bot_pid, \
                 patch("watchdog.telegram_alerts.send_telegram_alert") as mock_alert:
                watchdog.main()
        mock_bot_pid.assert_not_called()
        mock_alert.assert_not_called()

    def test_noop_when_bot_is_already_alive(self):
        with patch("watchdog.dashboard.bot_pid", return_value=12345), \
             patch("watchdog.dashboard.start_bot") as mock_start, \
             patch("watchdog.telegram_alerts.send_telegram_alert") as mock_alert:
            watchdog.main()
        mock_start.assert_not_called()
        mock_alert.assert_not_called()

    def test_restarts_and_alerts_twice_when_dead_and_restart_succeeds(self):
        with patch("watchdog.dashboard.bot_pid", side_effect=[None, 99999]), \
             patch("watchdog.dashboard.start_bot") as mock_start, \
             patch("watchdog.telegram_alerts.send_telegram_alert") as mock_alert, \
             patch("watchdog.time.sleep"):
            watchdog.main()
        mock_start.assert_called_once()
        self.assertEqual(mock_alert.call_count, 2)
        self.assertIn("found dead", mock_alert.call_args_list[0].args[0])
        self.assertIn("restarted successfully", mock_alert.call_args_list[1].args[0])
        self.assertIn("99999", mock_alert.call_args_list[1].args[0])

    def test_alerts_failure_when_restart_does_not_come_back_up(self):
        with patch("watchdog.dashboard.bot_pid", side_effect=[None, None]), \
             patch("watchdog.dashboard.start_bot") as mock_start, \
             patch("watchdog.telegram_alerts.send_telegram_alert") as mock_alert, \
             patch("watchdog.time.sleep"):
            watchdog.main()
        mock_start.assert_called_once()
        self.assertEqual(mock_alert.call_count, 2)
        self.assertIn("found dead", mock_alert.call_args_list[0].args[0])
        self.assertIn("FAILED", mock_alert.call_args_list[1].args[0])

    def test_pause_file_absent_by_default_does_not_prevent_a_real_restart_check(self):
        """Sanity check on the setUp fixture itself: with the pause path
        pointed at something that doesn't exist, the paused branch must
        not fire."""
        with patch("watchdog.dashboard.bot_pid", return_value=1) as mock_bot_pid:
            watchdog.main()
        mock_bot_pid.assert_called_once()


if __name__ == "__main__":
    unittest.main()
