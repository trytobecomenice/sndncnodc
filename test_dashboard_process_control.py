#!/usr/bin/env python3
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import dashboard


class TestDashboardProcessControl(unittest.TestCase):
    def test_process_validation_rejects_non_bot_command(self):
        result = Mock(stdout="S /usr/bin/python3 unrelated.py")
        with patch("dashboard.os.kill"), patch("dashboard.subprocess.run", return_value=result):
            self.assertFalse(dashboard._is_live_bot_process(123))

    def test_bot_pid_repairs_missing_pid_file_from_discovery(self):
        with tempfile.TemporaryDirectory() as directory:
            pid_path = str(Path(directory) / "bot.pid")
            with patch.object(dashboard, "PID_PATH", pid_path), \
                 patch("dashboard.bot_pids", return_value=[123]):
                self.assertEqual(dashboard.bot_pid(), 123)
                self.assertEqual(Path(pid_path).read_text(), "123")

    def test_start_bot_rechecks_under_lock_and_does_not_duplicate(self):
        with tempfile.TemporaryDirectory() as directory:
            lock_path = str(Path(directory) / "start.lock")
            with patch.object(dashboard, "START_LOCK_PATH", lock_path), \
                 patch("dashboard.bot_pid", return_value=123), \
                 patch("dashboard.subprocess.Popen") as popen:
                dashboard.start_bot()
            popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
