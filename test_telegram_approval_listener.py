#!/usr/bin/env python3
"""Unit tests for telegram_approval_listener.py (2026-08-01, Telegram
wallet-approval workflow — the "receive" half).

Run: python3 -m unittest test_telegram_approval_listener -v
"""

import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import telegram_approval_listener as listener


class TestParseCallbackData(unittest.TestCase):
    def test_parses_approve(self):
        self.assertEqual(listener.parse_callback_data("wa:req-1:approve"), ("req-1", "approve"))

    def test_parses_reject(self):
        self.assertEqual(listener.parse_callback_data("wa:req-1:reject"), ("req-1", "reject"))

    def test_request_id_can_contain_hyphens_and_uuids(self):
        self.assertEqual(
            listener.parse_callback_data("wa:8f14e45f-ceea-467e-9f0e-6e3a5f6b7c8d:approve"),
            ("8f14e45f-ceea-467e-9f0e-6e3a5f6b7c8d", "approve"),
        )

    def test_returns_none_for_wrong_action(self):
        self.assertIsNone(listener.parse_callback_data("wa:req-1:maybe"))

    def test_returns_none_for_unrelated_callback_data(self):
        self.assertIsNone(listener.parse_callback_data("some_other_feature:xyz"))

    def test_returns_none_for_empty_or_missing_data(self):
        self.assertIsNone(listener.parse_callback_data(""))
        self.assertIsNone(listener.parse_callback_data(None))

    def test_returns_none_for_extra_colon_segments(self):
        self.assertIsNone(listener.parse_callback_data("wa:req:1:approve"))


class TestIsAuthorizedChat(unittest.TestCase):
    def setUp(self):
        self._saved_chat_id = listener.telegram_alerts.TELEGRAM_CHAT_ID
        listener.telegram_alerts.TELEGRAM_CHAT_ID = "12345"

    def tearDown(self):
        listener.telegram_alerts.TELEGRAM_CHAT_ID = self._saved_chat_id

    def test_matching_chat_id_authorized(self):
        self.assertTrue(listener.is_authorized_chat("12345"))

    def test_matching_int_chat_id_authorized(self):
        # Telegram delivers chat.id as a JSON integer; TELEGRAM_CHAT_ID is a string.
        self.assertTrue(listener.is_authorized_chat(12345))

    def test_mismatched_chat_id_not_authorized(self):
        self.assertFalse(listener.is_authorized_chat("99999"))

    def test_unconfigured_chat_id_never_authorizes(self):
        listener.telegram_alerts.TELEGRAM_CHAT_ID = None
        self.assertFalse(listener.is_authorized_chat("12345"))


class TestFormatResolutionText(unittest.TestCase):
    def test_approve_with_tier(self):
        text = listener.format_resolution_text("original", "approve", "track")
        self.assertIn("Approved", text)
        self.assertIn("track", text)
        self.assertIn("original", text)

    def test_approve_without_tier(self):
        text = listener.format_resolution_text("original", "approve", None)
        self.assertIn("Approved", text)

    def test_reject(self):
        text = listener.format_resolution_text("original", "reject", "track")
        self.assertIn("Rejected", text)
        self.assertNotIn("Approved", text)


class TestOffsetPersistence(unittest.TestCase):
    def setUp(self):
        fd, self.tmp_path = tempfile.mkstemp()
        os.close(fd)
        os.remove(self.tmp_path)  # start from "file doesn't exist yet"
        self._patcher = patch.object(listener, "OFFSET_FILE", self.tmp_path)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        if os.path.exists(self.tmp_path):
            os.remove(self.tmp_path)

    def test_load_offset_defaults_to_zero_when_file_missing(self):
        self.assertEqual(listener.load_offset(), 0)

    def test_save_then_load_round_trips(self):
        listener.save_offset(42)
        self.assertEqual(listener.load_offset(), 42)

    def test_load_offset_defaults_to_zero_on_corrupt_file(self):
        with open(self.tmp_path, "w") as f:
            f.write("not a number")
        self.assertEqual(listener.load_offset(), 0)


class TestGetUpdates(unittest.TestCase):
    def setUp(self):
        self._saved_token = listener.telegram_alerts.TELEGRAM_BOT_TOKEN
        listener.telegram_alerts.TELEGRAM_BOT_TOKEN = "test-token"

    def tearDown(self):
        listener.telegram_alerts.TELEGRAM_BOT_TOKEN = self._saved_token

    def test_returns_result_list_on_success(self):
        mock_response = MagicMock(status=200)
        mock_response.read.return_value = json.dumps({"ok": True, "result": [{"update_id": 1}]}).encode()
        mock_conn = MagicMock()
        mock_conn.getresponse.return_value = mock_response
        with patch("telegram_approval_listener.http.client.HTTPSConnection", return_value=mock_conn):
            result = listener.get_updates(0, 30)
        self.assertEqual(result, [{"update_id": 1}])

    def test_returns_empty_list_when_result_is_empty(self):
        mock_response = MagicMock(status=200)
        mock_response.read.return_value = json.dumps({"ok": True, "result": []}).encode()
        mock_conn = MagicMock()
        mock_conn.getresponse.return_value = mock_response
        with patch("telegram_approval_listener.http.client.HTTPSConnection", return_value=mock_conn):
            result = listener.get_updates(0, 30)
        self.assertEqual(result, [])

    def test_raises_telegram_api_error_on_non_2xx(self):
        mock_response = MagicMock(status=401)
        mock_response.read.return_value = b'{"ok": false}'
        mock_conn = MagicMock()
        mock_conn.getresponse.return_value = mock_response
        with patch("telegram_approval_listener.http.client.HTTPSConnection", return_value=mock_conn):
            with self.assertRaises(listener.TelegramApiError):
                listener.get_updates(0, 30)

    def test_raises_telegram_api_error_on_network_failure(self):
        with patch("telegram_approval_listener.http.client.HTTPSConnection", side_effect=OSError("no route")):
            with self.assertRaises(listener.TelegramApiError):
                listener.get_updates(0, 30)


class TestHandleCallbackQuery(unittest.TestCase):
    def setUp(self):
        self._saved_chat_id = listener.telegram_alerts.TELEGRAM_CHAT_ID
        listener.telegram_alerts.TELEGRAM_CHAT_ID = "12345"

    def tearDown(self):
        listener.telegram_alerts.TELEGRAM_CHAT_ID = self._saved_chat_id

    def _callback_query(self, chat_id=12345, data="wa:req-1:approve", text="candidate info"):
        return {
            "id": "cbq-1",
            "data": data,
            "message": {"message_id": 555, "chat": {"id": chat_id}, "text": text},
        }

    def test_unauthorized_chat_never_touches_db(self):
        with patch("telegram_approval_listener.db.get_wallet_approval_request") as mock_get, \
             patch("telegram_approval_listener.db.resolve_wallet_approval_request") as mock_resolve, \
             patch("telegram_approval_listener.answer_callback_query") as mock_answer:
            listener.handle_callback_query(self._callback_query(chat_id=99999))
        mock_get.assert_not_called()
        mock_resolve.assert_not_called()
        mock_answer.assert_called_once_with("cbq-1", "Not authorized.")

    def test_unrecognized_callback_data_never_touches_db(self):
        with patch("telegram_approval_listener.db.get_wallet_approval_request") as mock_get, \
             patch("telegram_approval_listener.answer_callback_query") as mock_answer:
            listener.handle_callback_query(self._callback_query(data="garbage"))
        mock_get.assert_not_called()
        mock_answer.assert_called_once_with("cbq-1", "Unrecognized action.")

    def test_unknown_request_id_acks_without_resolving(self):
        with patch("telegram_approval_listener.db.get_wallet_approval_request", return_value=None), \
             patch("telegram_approval_listener.db.resolve_wallet_approval_request") as mock_resolve, \
             patch("telegram_approval_listener.answer_callback_query") as mock_answer:
            listener.handle_callback_query(self._callback_query())
        mock_resolve.assert_not_called()
        mock_answer.assert_called_once_with("cbq-1", "This request no longer exists.")

    def test_already_resolved_request_acks_as_already_handled_and_does_not_edit_message(self):
        request = {"id": "req-1", "wallet_address": "0xabc", "requested_tier": "track"}
        with patch("telegram_approval_listener.db.get_wallet_approval_request", return_value=request), \
             patch("telegram_approval_listener.db.resolve_wallet_approval_request", return_value=False), \
             patch("telegram_approval_listener.answer_callback_query") as mock_answer, \
             patch("telegram_approval_listener.edit_message_text") as mock_edit:
            listener.handle_callback_query(self._callback_query())
        mock_answer.assert_called_once_with("cbq-1", "Already handled.")
        mock_edit.assert_not_called()

    def test_successful_approve_resolves_acks_and_edits_message(self):
        request = {"id": "req-1", "wallet_address": "0xabc", "requested_tier": "track"}
        with patch("telegram_approval_listener.db.get_wallet_approval_request", return_value=request), \
             patch("telegram_approval_listener.db.resolve_wallet_approval_request", return_value=True) as mock_resolve, \
             patch("telegram_approval_listener.answer_callback_query") as mock_answer, \
             patch("telegram_approval_listener.edit_message_text") as mock_edit:
            listener.handle_callback_query(self._callback_query(data="wa:req-1:approve"))
        mock_resolve.assert_called_once_with("req-1", "approved")
        mock_answer.assert_called_once_with("cbq-1", "Approved.")
        mock_edit.assert_called_once()
        args, _ = mock_edit.call_args
        self.assertEqual(args[0], 12345)  # chat_id
        self.assertEqual(args[1], 555)  # message_id
        self.assertIn("Approved", args[2])

    def test_successful_reject_resolves_with_rejected_status(self):
        request = {"id": "req-1", "wallet_address": "0xabc", "requested_tier": "track"}
        with patch("telegram_approval_listener.db.get_wallet_approval_request", return_value=request), \
             patch("telegram_approval_listener.db.resolve_wallet_approval_request", return_value=True) as mock_resolve, \
             patch("telegram_approval_listener.answer_callback_query"), \
             patch("telegram_approval_listener.edit_message_text") as mock_edit:
            listener.handle_callback_query(self._callback_query(data="wa:req-1:reject"))
        mock_resolve.assert_called_once_with("req-1", "rejected")
        args, _ = mock_edit.call_args
        self.assertIn("Rejected", args[2])


if __name__ == "__main__":
    unittest.main()
