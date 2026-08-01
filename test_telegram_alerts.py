#!/usr/bin/env python3
"""Unit tests for telegram_alerts.py (2026-07-31, Phase 1 observability).

Run: python3 -m unittest test_telegram_alerts -v
"""

import json
import socket
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


class TestForceIpv4Dns(unittest.TestCase):
    """2026-08-01 fix — every alert from bot.py's own long-running process
    was silently failing on IPv6 (this EC2 box has no real IPv6 route),
    while a fresh standalone process succeeded. _force_ipv4_dns() scopes a
    socket.getaddrinfo monkeypatch to exactly one connection attempt."""

    def setUp(self):
        # Only needed by test_send_telegram_alert_uses_the_ipv4_forcing_scope
        # below -- without a configured token/chat_id, send_telegram_alert()
        # returns False before ever reaching the connection code.
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

    def test_patches_getaddrinfo_to_request_af_inet_only(self):
        seen_families = []
        original = socket.getaddrinfo

        def fake_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
            seen_families.append(family)
            return []

        with patch("socket.getaddrinfo", side_effect=fake_getaddrinfo):
            with telegram_alerts._force_ipv4_dns():
                socket.getaddrinfo("api.telegram.org", 443)
        self.assertEqual(seen_families, [socket.AF_INET])
        self.assertIs(socket.getaddrinfo, original)  # restored, not leaked globally

    def test_restores_original_getaddrinfo_even_on_exception(self):
        original = socket.getaddrinfo
        with self.assertRaises(RuntimeError):
            with telegram_alerts._force_ipv4_dns():
                raise RuntimeError("boom")
        self.assertIs(socket.getaddrinfo, original)

    def test_send_telegram_alert_uses_the_ipv4_forcing_scope(self):
        # Confirms send_telegram_alert() actually wraps its connect/request
        # step in the patch, not just the object construction (a bug this
        # fix's own first draft had -- HTTPSConnection's constructor
        # doesn't resolve DNS at all, connect() does, lazily, inside
        # request()).
        mock_response = MagicMock(status=200)
        mock_conn = MagicMock()
        mock_conn.getresponse.return_value = mock_response
        calls = []

        class _RecordingContext:
            def __enter__(self):
                calls.append("enter")

            def __exit__(self, *a):
                calls.append("exit")

        with patch("telegram_alerts.http.client.HTTPSConnection", return_value=mock_conn), \
             patch("telegram_alerts._force_ipv4_dns", return_value=_RecordingContext()):
            telegram_alerts.send_telegram_alert("hi")
        self.assertEqual(calls, ["enter", "exit"])
        mock_conn.request.assert_called_once()


class TestSendTelegramMessageWithButtons(unittest.TestCase):
    """2026-08-01, Telegram wallet-approval workflow."""

    def setUp(self):
        self._saved_enabled = config.ENABLE_TELEGRAM_ALERTS
        self._saved_token = telegram_alerts.TELEGRAM_BOT_TOKEN
        self._saved_chat_id = telegram_alerts.TELEGRAM_CHAT_ID
        config.ENABLE_TELEGRAM_ALERTS = True
        telegram_alerts.TELEGRAM_BOT_TOKEN = "test-token"
        telegram_alerts.TELEGRAM_CHAT_ID = "12345"
        self.buttons = [[{"text": "✅ Approve", "callback_data": "wa:req-1:approve"},
                          {"text": "❌ Reject", "callback_data": "wa:req-1:reject"}]]

    def tearDown(self):
        config.ENABLE_TELEGRAM_ALERTS = self._saved_enabled
        telegram_alerts.TELEGRAM_BOT_TOKEN = self._saved_token
        telegram_alerts.TELEGRAM_CHAT_ID = self._saved_chat_id

    def test_noop_when_alerts_disabled(self):
        config.ENABLE_TELEGRAM_ALERTS = False
        with patch("telegram_alerts.http.client.HTTPSConnection") as mock_conn:
            result = telegram_alerts.send_telegram_message_with_buttons("hi", self.buttons)
        self.assertIsNone(result)
        mock_conn.assert_not_called()

    def test_noop_when_token_missing(self):
        telegram_alerts.TELEGRAM_BOT_TOKEN = None
        with patch("telegram_alerts.http.client.HTTPSConnection") as mock_conn:
            result = telegram_alerts.send_telegram_message_with_buttons("hi", self.buttons)
        self.assertIsNone(result)
        mock_conn.assert_not_called()

    def test_returns_message_id_on_2xx_response(self):
        mock_response = MagicMock(status=200)
        mock_response.read.return_value = json.dumps({"ok": True, "result": {"message_id": 999}}).encode()
        mock_conn = MagicMock()
        mock_conn.getresponse.return_value = mock_response
        with patch("telegram_alerts.http.client.HTTPSConnection", return_value=mock_conn):
            result = telegram_alerts.send_telegram_message_with_buttons("hi", self.buttons)
        self.assertEqual(result, 999)
        mock_conn.close.assert_called_once()

    def test_request_body_includes_inline_keyboard(self):
        mock_response = MagicMock(status=200)
        mock_response.read.return_value = json.dumps({"result": {"message_id": 1}}).encode()
        mock_conn = MagicMock()
        mock_conn.getresponse.return_value = mock_response
        with patch("telegram_alerts.http.client.HTTPSConnection", return_value=mock_conn):
            telegram_alerts.send_telegram_message_with_buttons("hi", self.buttons)
        _, kwargs = mock_conn.request.call_args
        sent_body = json.loads(kwargs["body"])
        self.assertEqual(sent_body["reply_markup"], {"inline_keyboard": self.buttons})
        self.assertEqual(sent_body["text"], "hi")

    def test_returns_none_on_non_2xx_response(self):
        mock_response = MagicMock(status=401)
        mock_response.read.return_value = b'{"ok": false}'
        mock_conn = MagicMock()
        mock_conn.getresponse.return_value = mock_response
        with patch("telegram_alerts.http.client.HTTPSConnection", return_value=mock_conn):
            result = telegram_alerts.send_telegram_message_with_buttons("hi", self.buttons)
        self.assertIsNone(result)

    def test_returns_none_on_unparseable_response_body(self):
        mock_response = MagicMock(status=200)
        mock_response.read.return_value = b"not json"
        mock_conn = MagicMock()
        mock_conn.getresponse.return_value = mock_response
        with patch("telegram_alerts.http.client.HTTPSConnection", return_value=mock_conn):
            result = telegram_alerts.send_telegram_message_with_buttons("hi", self.buttons)
        self.assertIsNone(result)

    def test_network_failure_is_caught_not_raised(self):
        with patch("telegram_alerts.http.client.HTTPSConnection", side_effect=OSError("no route")):
            result = telegram_alerts.send_telegram_message_with_buttons("hi", self.buttons)  # must not raise
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
