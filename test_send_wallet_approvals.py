#!/usr/bin/env python3
"""Unit tests for send_wallet_approvals.py (2026-08-01, Telegram
wallet-approval workflow).

Run: python3 -m unittest test_send_wallet_approvals -v
"""

import json
import unittest
from unittest.mock import patch

import send_wallet_approvals as swa


def _request(**overrides):
    base = {
        "id": "req-1",
        "wallet_address": "0xabc",
        "nickname": None,
        "requested_tier": "track",
        "source": "category_quota",
        "category": "crypto",
        "score_snapshot_json": json.dumps({
            "compositeScore": 0.712,
            "pnlTStat": 2.5,
            "winRate": 0.65,
            "tradeCount": 40,
            "roi": 0.12,
            "washTradingSuspect": False,
        }),
        "reason": "category quota (crypto): t_stat=2.50, roi=12.0%, 40 trades, win_rate=65.0%",
        "status": "pending",
        "telegram_message_id": None,
        "telegram_chat_id": None,
    }
    base.update(overrides)
    return base


class TestFormatCandidateMessage(unittest.TestCase):
    def test_includes_key_fields(self):
        message = swa.format_candidate_message(_request())
        self.assertIn("0xabc", message)
        self.assertIn("track", message)
        self.assertIn("crypto", message)
        self.assertIn("0.712", message)
        self.assertIn("65.0%", message)
        self.assertIn("40", message)

    def test_uses_nickname_over_address_when_present(self):
        message = swa.format_candidate_message(_request(nickname="Big Whale"))
        self.assertIn("Big Whale", message)

    def test_flags_wash_trading_suspect(self):
        snapshot = json.loads(_request()["score_snapshot_json"])
        snapshot["washTradingSuspect"] = True
        message = swa.format_candidate_message(_request(score_snapshot_json=json.dumps(snapshot)))
        self.assertIn("WASH-TRADING SUSPECT", message)

    def test_handles_missing_optional_fields_without_crashing(self):
        # global_pool candidates don't carry pnlTStat/roi/washTradingSuspect
        # at all (see walletApprovalQueue.ts's ScoreSnapshot type).
        sparse = _request(
            source="global_pool", category=None,
            score_snapshot_json=json.dumps({"compositeScore": 0.5, "winRate": 0.6, "tradeCount": 30}),
            reason="composite score 0.500 >= track threshold 0.4",
        )
        message = swa.format_candidate_message(sparse)
        self.assertIn("0.500", message)
        self.assertNotIn("t_stat", message)

    def test_handles_empty_score_snapshot(self):
        message = swa.format_candidate_message(_request(score_snapshot_json="{}"))
        self.assertIn("0xabc", message)  # doesn't crash, just omits score lines


class TestBuildButtons(unittest.TestCase):
    def test_builds_approve_reject_row_with_request_id_in_callback_data(self):
        buttons = swa.build_buttons("req-42")
        self.assertEqual(len(buttons), 1)
        self.assertEqual(len(buttons[0]), 2)
        self.assertEqual(buttons[0][0]["callback_data"], "wa:req-42:approve")
        self.assertEqual(buttons[0][1]["callback_data"], "wa:req-42:reject")


class TestMain(unittest.TestCase):
    def test_no_pending_requests_sends_nothing(self):
        with patch("send_wallet_approvals.db.get_pending_wallet_approval_requests", return_value=[]), \
             patch("send_wallet_approvals.send_telegram_message_with_buttons") as mock_send:
            swa.main()
        mock_send.assert_not_called()

    def test_sends_each_pending_request_and_marks_it_sent(self):
        requests = [_request(id="req-1", wallet_address="0xa"), _request(id="req-2", wallet_address="0xb")]
        with patch("send_wallet_approvals.db.get_pending_wallet_approval_requests", return_value=requests), \
             patch("send_wallet_approvals.send_telegram_message_with_buttons", side_effect=[111, 222]), \
             patch("send_wallet_approvals.telegram_alerts.TELEGRAM_CHAT_ID", "chat-xyz"), \
             patch("send_wallet_approvals.db.mark_wallet_approval_request_sent") as mock_mark:
            swa.main()
        self.assertEqual(mock_mark.call_count, 2)
        mock_mark.assert_any_call("req-1", 111, "chat-xyz")
        mock_mark.assert_any_call("req-2", 222, "chat-xyz")

    def test_a_failed_send_is_not_marked_sent_and_does_not_stop_the_batch(self):
        requests = [_request(id="req-1", wallet_address="0xa"), _request(id="req-2", wallet_address="0xb")]
        with patch("send_wallet_approvals.db.get_pending_wallet_approval_requests", return_value=requests), \
             patch("send_wallet_approvals.send_telegram_message_with_buttons", side_effect=[None, 222]), \
             patch("send_wallet_approvals.telegram_alerts.TELEGRAM_CHAT_ID", "chat-xyz"), \
             patch("send_wallet_approvals.db.mark_wallet_approval_request_sent") as mock_mark:
            swa.main()
        mock_mark.assert_called_once_with("req-2", 222, "chat-xyz")


if __name__ == "__main__":
    unittest.main()
