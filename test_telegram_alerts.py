#!/usr/bin/env python3
"""Unit tests for telegram_alerts.py (2026-07-31, Phase 1 observability).

Run: python3 -m unittest test_telegram_alerts -v
"""

import unittest
from unittest.mock import MagicMock, patch

import config
import telegram_alerts


class TestSendTelegramAlert(unittest.TestCase):
    def setUp(self):
        self._saved_enabled = config.ENABLE_TELEGRAM_ALERTS
        self._saved_token = telegram_alerts.TELEGRAM_BOT_TOKEN
        self._saved_chat_id = telegram_alerts.TELEGRAM_CHAT_ID
        config.ENABLE_TELEGRAM_ALERTS = True
        telegram_alerts.TELEGRAM_BOT_TOKEN = "test-token"
        telegram_alerts.TELEGRAM_CHAT_ID = "12345"

    def tearDown(self):
        config.ENABLE_TELEGRAM_ALERTS = self._saved_enabled
        telegram_alerts.TELEGRAM_BOT_TOKEN = self._saved_token
        telegram_alerts.TELEGRAM_CHAT_ID = self._saved_chat_id

    def test_noop_when_alerts_disabled(self):
        config.ENABLE_TELEGRAM_ALERTS = False
        with patch("telegram_alerts.http.client.HTTPSConnection") as mock_conn:
            result = telegram_alerts.send_telegram_alert("hi")
        self.assertFalse(result)
        mock_conn.assert_not_called()

    def test_noop_when_token_missing(self):
        telegram_alerts.TELEGRAM_BOT_TOKEN = None
        with patch("telegram_alerts.http.client.HTTPSConnection") as mock_conn:
            result = telegram_alerts.send_telegram_alert("hi")
        self.assertFalse(result)
        mock_conn.assert_not_called()

    def test_noop_when_chat_id_missing(self):
        telegram_alerts.TELEGRAM_CHAT_ID = None
        with patch("telegram_alerts.http.client.HTTPSConnection") as mock_conn:
            result = telegram_alerts.send_telegram_alert("hi")
        self.assertFalse(result)
        mock_conn.assert_not_called()

    def test_returns_true_on_2xx_response(self):
        mock_response = MagicMock(status=200)
        mock_conn = MagicMock()
        mock_conn.getresponse.return_value = mock_response
        with patch("telegram_alerts.http.client.HTTPSConnection", return_value=mock_conn):
            result = telegram_alerts.send_telegram_alert("hi")
        self.assertTrue(result)
        mock_conn.close.assert_called_once()

    def test_returns_false_on_non_2xx_response(self):
        mock_response = MagicMock(status=401)
        mock_conn = MagicMock()
        mock_conn.getresponse.return_value = mock_response
        with patch("telegram_alerts.http.client.HTTPSConnection", return_value=mock_conn):
            result = telegram_alerts.send_telegram_alert("hi")
        self.assertFalse(result)

    def test_network_failure_is_caught_not_raised(self):
        with patch("telegram_alerts.http.client.HTTPSConnection", side_effect=OSError("no route")):
            result = telegram_alerts.send_telegram_alert("hi")  # must not raise
        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
