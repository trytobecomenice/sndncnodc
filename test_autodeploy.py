#!/usr/bin/env python3
"""Unit tests for autodeploy.py (2026-07-31) — git-pull-test-restart
pipeline for bot.py, gated on the full test suite passing.

Run: python3 -m unittest test_autodeploy -v
"""

import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import autodeploy


def _result(returncode=0, stdout="", stderr=""):
    return MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)


class TestAutodeploy(unittest.TestCase):
    def setUp(self):
        fd, self._log_path = tempfile.mkstemp(suffix=".log")
        os.close(fd)
        fd, self._lock_path = tempfile.mkstemp(suffix=".lock")
        os.close(fd)
        os.remove(self._lock_path)  # start with the lock NOT held
        fd, self._pause_path = tempfile.mkstemp(suffix=".pause")
        os.close(fd)
        os.remove(self._pause_path)  # start with the watchdog not paused

        self._patchers = [
            patch.object(autodeploy, "AUTODEPLOY_LOG_PATH", self._log_path),
            patch.object(autodeploy, "AUTODEPLOY_LOCK_PATH", self._lock_path),
            patch.object(autodeploy, "WATCHDOG_PAUSE_PATH", self._pause_path),
        ]
        for p in self._patchers:
            p.start()

    def tearDown(self):
        for p in self._patchers:
            p.stop()
        for path in (self._log_path, self._lock_path, self._pause_path):
            if os.path.exists(path):
                os.remove(path)

    def test_noop_when_lock_file_present(self):
        open(self._lock_path, "w").close()
        with patch("autodeploy._run") as mock_run, \
             patch("autodeploy.telegram_alerts.send_telegram_alert") as mock_alert:
            autodeploy.main()
        mock_run.assert_not_called()
        mock_alert.assert_not_called()

    def test_noop_when_already_up_to_date(self):
        with patch("autodeploy._run", side_effect=[
                _result(),  # fetch
                _result(stdout="abc1234\n"),  # rev-parse HEAD
                _result(stdout="abc1234\n"),  # rev-parse origin/main -- same
             ]) as mock_run, \
             patch("autodeploy.telegram_alerts.send_telegram_alert") as mock_alert:
            autodeploy.main()
        self.assertEqual(mock_run.call_count, 3)
        mock_alert.assert_not_called()
        self.assertFalse(os.path.exists(self._lock_path))  # lock released

    def test_happy_path_deploys_and_restarts(self):
        with patch("autodeploy._run", side_effect=[
                _result(),  # fetch
                _result(stdout="old1111\n"),  # rev-parse HEAD
                _result(stdout="new2222\n"),  # rev-parse origin/main
                _result(),  # git pull
                _result(),  # unittest discover -- passes
             ]), \
             patch("autodeploy.dashboard.stop_bot") as mock_stop, \
             patch("autodeploy.dashboard.start_bot") as mock_start, \
             patch("autodeploy.dashboard.bot_pid", side_effect=[None, 55555]), \
             patch("autodeploy.telegram_alerts.send_telegram_alert") as mock_alert, \
             patch("autodeploy.time.sleep"):
            autodeploy.main()
        mock_stop.assert_called_once()
        mock_start.assert_called_once()
        self.assertEqual(mock_alert.call_count, 2)
        self.assertIn("new2222", mock_alert.call_args_list[0].args[0])
        self.assertIn("live", mock_alert.call_args_list[1].args[0])
        self.assertIn("55555", mock_alert.call_args_list[1].args[0])
        self.assertFalse(os.path.exists(self._lock_path))
        self.assertFalse(os.path.exists(self._pause_path))  # pause released after

    def test_pull_failure_alerts_and_stops_short(self):
        with patch("autodeploy._run", side_effect=[
                _result(),
                _result(stdout="old1111\n"),
                _result(stdout="new2222\n"),
                _result(returncode=1, stderr="conflict"),  # git pull fails
             ]), \
             patch("autodeploy.dashboard.stop_bot") as mock_stop, \
             patch("autodeploy.telegram_alerts.send_telegram_alert") as mock_alert:
            autodeploy.main()
        mock_stop.assert_not_called()
        self.assertEqual(mock_alert.call_count, 2)  # "detected" + "pull failed"
        self.assertIn("pull failed", mock_alert.call_args_list[1].args[0])
        self.assertFalse(os.path.exists(self._lock_path))

    def test_failing_tests_roll_back_and_never_touch_bot_py(self):
        with patch("autodeploy._run", side_effect=[
                _result(),
                _result(stdout="old1111\n"),
                _result(stdout="new2222\n"),
                _result(),  # pull ok
                _result(returncode=1),  # tests fail
                _result(),  # git reset --hard
             ]) as mock_run, \
             patch("autodeploy.dashboard.stop_bot") as mock_stop, \
             patch("autodeploy.dashboard.start_bot") as mock_start, \
             patch("autodeploy.telegram_alerts.send_telegram_alert") as mock_alert:
            autodeploy.main()
        mock_stop.assert_not_called()
        mock_start.assert_not_called()
        reset_call = mock_run.call_args_list[-1]
        self.assertEqual(reset_call.args[0], ["git", "reset", "--hard", "old1111"])
        self.assertEqual(mock_alert.call_count, 2)  # "detected" + "rolled back"
        self.assertIn("rolled back", mock_alert.call_args_list[1].args[0])
        self.assertFalse(os.path.exists(self._lock_path))

    def test_restart_failure_after_successful_deploy_alerts_urgently(self):
        with patch("autodeploy._run", side_effect=[
                _result(),
                _result(stdout="old1111\n"),
                _result(stdout="new2222\n"),
                _result(),
                _result(),
             ]), \
             patch("autodeploy.dashboard.stop_bot"), \
             patch("autodeploy.dashboard.start_bot"), \
             patch("autodeploy.dashboard.bot_pid", side_effect=[None, None]), \
             patch("autodeploy.telegram_alerts.send_telegram_alert") as mock_alert, \
             patch("autodeploy.time.sleep"):
            autodeploy.main()
        self.assertEqual(mock_alert.call_count, 2)
        self.assertIn("failed to come back up", mock_alert.call_args_list[1].args[0])
        self.assertFalse(os.path.exists(self._pause_path))


if __name__ == "__main__":
    unittest.main()
