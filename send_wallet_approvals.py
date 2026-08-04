#!/usr/bin/env python3
"""Sends any un-sent pending wallet_approval_request rows to Joey via
Telegram, one message per candidate, each with Approve/Reject inline
buttons (2026-08-01, Telegram wallet-approval workflow).

This is the "send" half — see telegram_approval_listener.py for the
long-running "receive" half that watches for the button tap and actually
flips wallet_profile.status. Nothing here writes to wallet_profile; this
script only ever reads wallet_approval_request and calls Telegram's
sendMessage.

Meant to run as the last step of the daily scan cron chain, right after
`pnpm scan:leaderboard && pnpm scan:wallets && pnpm discover:category-
specialists -- --queue-approvals` — see docs/copy-trading/SAFETY.md's
Telegram wallet-approval section for the full pipeline and crontab line.
Safe to re-run any time: a request that's already been sent
(telegram_message_id IS NOT NULL) is never re-sent (see
db.get_pending_wallet_approval_requests(unsent_only=True)).

Usage:
    python3 send_wallet_approvals.py
"""

import json

import db
import telegram_alerts
from telegram_alerts import send_telegram_message_with_buttons


def format_candidate_message(request):
    """Human-readable Telegram message body for one pending request. Pulls
    whatever fields are present in scoreSnapshotJson (shape varies by
    source — 'global_pool' candidates carry compositeScore/winRate/
    tradeCount, 'category_quota' candidates additionally carry pnlTStat/
    roi/washTradingSuspect — see walletApprovalQueue.ts's ScoreSnapshot
    type) rather than assuming every field is always there.
    """
    snapshot = json.loads(request["score_snapshot_json"] or "{}")
    label = request["nickname"] or request["wallet_address"]
    lines = [
        f"🆕 New {request['requested_tier']} candidate ({request['source'].replace('_', ' ')})",
        f"{label}",
        f"{request['wallet_address']}",
    ]
    if request.get("category"):
        lines.append(f"category: {request['category']}")
    if snapshot.get("compositeScore") is not None:
        lines.append(f"composite_score: {snapshot['compositeScore']:.3f}")
    if snapshot.get("pnlTStat") is not None:
        lines.append(f"t_stat: {snapshot['pnlTStat']:.2f}")
    if snapshot.get("winRate") is not None:
        lines.append(f"win_rate: {snapshot['winRate']*100:.1f}%")
    if snapshot.get("tradeCount") is not None:
        lines.append(f"trades: {snapshot['tradeCount']}")
    if snapshot.get("roi") is not None:
        lines.append(f"roi: {snapshot['roi']*100:.1f}%")
    if snapshot.get("washTradingSuspect"):
        lines.append("⚠ WASH-TRADING SUSPECT — review before approving")
    if request.get("source") == "challenger_shadow":
        lines.append("\n🧪 CLEAN SHADOW EVIDENCE")
        lines.append(f"shadow closes: {snapshot.get('tradeCount', 0)}")
        if snapshot.get("shadowAgeDays") is not None:
            lines.append(f"shadow age: {snapshot['shadowAgeDays']:.1f} days")
        if snapshot.get("meanReturn") is not None:
            lines.append(f"mean return/trade: {snapshot['meanReturn']*100:.2f}%")
        if snapshot.get("lowerConfidenceBound") is not None:
            lines.append(f"one-sided LCB: {snapshot['lowerConfidenceBound']*100:.2f}%")
        replacement = snapshot.get("replacementNickname") or snapshot.get("replacementWalletAddress")
        if replacement:
            lines.append(f"one-in-one-out: retires {replacement}")
    lines.append(f"\n{request['reason']}")
    return "\n".join(lines)


def build_buttons(request_id):
    return [[
        {"text": "✅ Approve", "callback_data": f"wa:{request_id}:approve"},
        {"text": "❌ Reject", "callback_data": f"wa:{request_id}:reject"},
    ]]


def main():
    requests = db.get_pending_wallet_approval_requests(unsent_only=True)
    if not requests:
        print("No un-sent pending wallet_approval_request rows — nothing to send.")
        return

    print(f"Sending {len(requests)} pending candidate(s) to Telegram...")
    sent = 0
    failed = 0
    for request in requests:
        message = format_candidate_message(request)
        buttons = build_buttons(request["id"])
        message_id = send_telegram_message_with_buttons(message, buttons)
        if message_id is None:
            failed += 1
            print(f"  FAILED to send {request['wallet_address']} ({request['requested_tier']}) — "
                  f"will retry on next run")
            continue
        db.mark_wallet_approval_request_sent(request["id"], message_id, telegram_alerts.TELEGRAM_CHAT_ID)
        sent += 1
        print(f"  sent {request['wallet_address']} ({request['requested_tier']}, {request['source']})")

    print(f"Done. {sent} sent, {failed} failed (failed ones stay un-sent and will retry next run).")


if __name__ == "__main__":
    main()
