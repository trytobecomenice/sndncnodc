#!/usr/bin/env python3
"""Auto-deploy for bot.py (2026-07-31) — see docs/copy-trading/SAFETY.md
Sec.57. Meant to run periodically via cron: detects a new commit on
origin/main, pulls it, runs the full test suite as a gate, and only then
restarts bot.py — rolling back to the previous commit if tests fail,
rather than leaving broken code on disk for the next restart (manual or
watchdog.py's) to pick up.

Before this, every deploy this session was manual: git pull, kill the old
process, start the new one, by hand, every single time. This automates
that exact sequence with a safety gate the manual process never
enforced as a hard block (tests were always run, but a human decided
whether a failure should stop the deploy).

Coordinates with watchdog.py via the SAME pause sentinel
(data/watchdog_paused) — without this, stopping bot.py here to restart it
with new code could race the watchdog's own every-2-minute check, which
would otherwise "helpfully" restart the OLD code before this script gets
a chance to start the new one.

Every stage is Telegram-alerted: this is deploying to whatever is running
this bot (still PAPER_MODE only as of this writing) without a human
reviewing each individual push before it goes live within one cron tick
(default every 5 min, see the crontab entry in SAFETY.md Sec.57) — a
real, deliberate increase in autonomy Joey explicitly asked for, not
something to leave silent.
"""

import os
import subprocess
import time

import config
import dashboard
import telegram_alerts

AUTODEPLOY_LOG_PATH = os.path.join(config.BASE_DIR, "autodeploy.out.log")
AUTODEPLOY_LOCK_PATH = os.path.join(config.BASE_DIR, "data", "autodeploy.lock")
WATCHDOG_PAUSE_PATH = os.path.join(config.BASE_DIR, "data", "watchdog_paused")

RESTART_CONFIRM_DELAY_SECONDS = 5
STOP_CONFIRM_TIMEOUT_SECONDS = 20


def _log(message):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
    print(line)
    with open(AUTODEPLOY_LOG_PATH, "a") as f:
        f.write(line + "\n")


def _run(args):
    return subprocess.run(args, cwd=config.BASE_DIR, capture_output=True, text=True)


def main():
    if os.path.exists(AUTODEPLOY_LOCK_PATH):
        return  # a previous run is still in flight (or crashed mid-deploy) -- don't overlap

    _run(["git", "fetch", "origin", "main"])
    local_commit = _run(["git", "rev-parse", "HEAD"]).stdout.strip()
    remote_commit = _run(["git", "rev-parse", "origin/main"]).stdout.strip()
    if local_commit == remote_commit:
        return  # already up to date, nothing to do

    open(AUTODEPLOY_LOCK_PATH, "w").close()
    try:
        _deploy(local_commit, remote_commit)
    finally:
        os.remove(AUTODEPLOY_LOCK_PATH)


def _deploy(local_commit, remote_commit):
    short_local, short_remote = local_commit[:7], remote_commit[:7]
    _log(f"new commit detected: {short_local} -> {short_remote}")
    telegram_alerts.send_telegram_alert(
        f"\U0001F504 Auto-deploy: new commit {short_remote} detected, deploying..."
    )

    pull = _run(["git", "pull", "origin", "main"])
    if pull.returncode != 0:
        _log(f"git pull FAILED: {pull.stderr}")
        telegram_alerts.send_telegram_alert(
            f"\U0001F6A8 Auto-deploy: git pull failed for {short_remote}: {pull.stderr[:300]}"
        )
        return

    tests = _run(["python3", "-m", "unittest", "discover", "-p", "test_*.py"])
    if tests.returncode != 0:
        _log(f"tests FAILED on {short_remote} -- rolling back to {short_local}")
        _run(["git", "reset", "--hard", local_commit])
        telegram_alerts.send_telegram_alert(
            f"\U0001F6A8 Auto-deploy: tests FAILED on {short_remote}, rolled back to {short_local}. "
            f"bot.py was NOT touched, still running the old code. Needs manual review."
        )
        return

    _log(f"tests passed on {short_remote} -- restarting bot.py")
    # Pause the watchdog for the duration of this restart so it can't race
    # start_bot() below and revive the OLD process first.
    open(WATCHDOG_PAUSE_PATH, "w").close()
    try:
        dashboard.stop_bot()
        for _ in range(STOP_CONFIRM_TIMEOUT_SECONDS):
            if dashboard.bot_pid() is None:
                break
            time.sleep(1)

        dashboard.start_bot()
        time.sleep(RESTART_CONFIRM_DELAY_SECONDS)
        new_pid = dashboard.bot_pid()
    finally:
        os.remove(WATCHDOG_PAUSE_PATH)

    if new_pid is not None:
        _log(f"deployed {short_remote}, bot.py restarted (pid {new_pid})")
        telegram_alerts.send_telegram_alert(
            f"✅ Auto-deploy: {short_remote} is live, bot.py restarted (pid {new_pid})."
        )
    else:
        _log(f"deployed {short_remote} but bot.py failed to restart")
        telegram_alerts.send_telegram_alert(
            f"\U0001F6A8 Auto-deploy: {short_remote} was deployed but bot.py failed to come back "
            f"up! Needs immediate manual attention — the bot is currently DOWN."
        )


if __name__ == "__main__":
    main()
