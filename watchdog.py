#!/usr/bin/env python3
"""bot.py process watchdog (2026-07-31) — see docs/copy-trading/SAFETY.md
Sec.56. Meant to run periodically via cron; detects a dead bot.py and
restarts it, alerting via Telegram both when it's found dead and once the
restart is confirmed healthy.

Built in direct response to a real incident the same night: bot.py died
silently (no SIGTERM log line, no traceback — most likely killed by the
severe memory pressure the Docker/Prometheus/Grafana stack put on the EC2
box at the time) and sat dead for ~2.5 hours before anyone noticed, purely
because nothing was watching the process itself.

Reuses dashboard.py's own PID_PATH/bot_pid()/start_bot() (bot.pid at the
repo root) rather than re-implementing that logic — importantly, NOT
`data/bot.pid`, an informal file this project had been hand-maintaining
that dashboard.py's own /api/toggle never actually reads. Using the real
one keeps this watchdog, the dashboard's start/stop button, and any future
tooling agreeing on one single source of truth instead of two
independently-drifting PID files.

Pause mechanism: touch WATCHDOG_PAUSE_PATH (data/watchdog_paused) to make
this a no-op. dashboard.py's stop_bot() does not (yet) touch this file
itself, so deliberately stopping bot.py via the dashboard's stop button or
a manual SIGTERM WILL get "helpfully" revived on this watchdog's very next
run unless the pause file is touched first — a known, documented
limitation, not silently glossed over.
"""

import os
import time

import config
import dashboard
import telegram_alerts

WATCHDOG_PAUSE_PATH = os.path.join(config.BASE_DIR, "data", "watchdog_paused")
WATCHDOG_LOG_PATH = os.path.join(config.BASE_DIR, "watchdog.out.log")

# Seconds to wait after start_bot() before checking whether the restart
# actually took — bot.py's own startup (loading tracked traders, wallet
# scores, etc.) is not instant.
RESTART_CONFIRM_DELAY_SECONDS = 5


def _log(message):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
    print(line)
    with open(WATCHDOG_LOG_PATH, "a") as f:
        f.write(line + "\n")


def main():
    if os.path.exists(WATCHDOG_PAUSE_PATH):
        return  # deliberately paused -- no-op, no log spam, no Telegram noise

    if dashboard.bot_pid() is not None:
        return  # alive, nothing to do

    _log("bot.py not running -- restarting")
    telegram_alerts.send_telegram_alert(
        "⚠️ copybot watchdog: bot.py was found dead, restarting now."
    )
    dashboard.start_bot()

    time.sleep(RESTART_CONFIRM_DELAY_SECONDS)
    new_pid = dashboard.bot_pid()
    if new_pid is not None:
        _log(f"restart succeeded, new pid {new_pid}")
        telegram_alerts.send_telegram_alert(
            f"✅ copybot watchdog: bot.py restarted successfully (pid {new_pid})."
        )
    else:
        _log("restart FAILED -- bot.py did not come back up")
        telegram_alerts.send_telegram_alert(
            "\U0001F6A8 copybot watchdog: restart attempt FAILED -- bot.py still not running. "
            "Needs manual investigation."
        )


if __name__ == "__main__":
    main()
