#!/usr/bin/env python3
"""
Local dashboard for the Polymarket copytrading bot.

Serves a single-page dashboard at http://localhost:8787 showing PnL, win
rate, trade counts, and the full trade log, with a button to start/stop
bot.py. Pure standard library — no extra installs required.

Reads from the shared SQLite DB (data/app.db, see db.py) rather than
state.json/trades_log.json directly — bot_event_log rows store the exact
same event dicts bot.py always built (each row's payload_json is literally
json.dumps(event)), so build_status()'s stats computation below is otherwise
unchanged from the JSON-file version.
"""

import json
import fcntl
import logging
import os
import signal
import sqlite3
import subprocess
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler

import config
from db import get_closed_trade_stats_since, get_tracked_traders, realized_pnl_total

PORT = 8787
PID_PATH = os.path.join(config.BASE_DIR, "bot.pid")
START_LOCK_PATH = os.path.join(config.BASE_DIR, "data", "bot-start.lock")
BOT_LOG_PATH = config.BOT_LOG_PATH

# Own rotating log (2026-07-22, disk-exhaustion hardening) — same reasoning
# as bot.py's logger (see its module-level comment): a plain text file for
# this process's own 2 status prints, capped instead of growing forever.
logger = logging.getLogger("dashboard")
logger.setLevel(logging.INFO)
_file_handler = RotatingFileHandler(
    config.DASHBOARD_LOG_PATH, maxBytes=config.LOG_MAX_BYTES, backupCount=config.LOG_BACKUP_COUNT,
)
_file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(_file_handler)
logger.addHandler(logging.StreamHandler())
INDEX_PATH = os.path.join(config.BASE_DIR, "static", "index.html")


def _is_live_bot_process(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False

    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "stat=", "-o", "args="],
            capture_output=True, text=True, timeout=5,
        )
        output = result.stdout.strip()
        if not output or output.startswith("Z") or "bot.py" not in output:
            return False
    except Exception:
        return False

    proc_dir = f"/proc/{pid}"
    if os.path.isdir(proc_dir):
        try:
            cwd = os.path.realpath(os.readlink(os.path.join(proc_dir, "cwd")))
            cmdline = open(os.path.join(proc_dir, "cmdline"), "rb").read().split(b"\0")
            if cwd != os.path.realpath(config.BASE_DIR):
                return False
            if not any(os.path.basename(part.decode(errors="ignore")) == "bot.py"
                       for part in cmdline if part):
                return False
        except OSError:
            return False
    return True


def bot_pids():
    """Return every live bot.py PID for this repository, not just bot.pid."""
    candidates = set()
    try:
        with open(PID_PATH) as f:
            candidates.add(int(f.read().strip()))
    except (ValueError, OSError):
        pass

    proc_root = "/proc"
    if os.path.isdir(proc_root):
        for name in os.listdir(proc_root):
            if name.isdigit():
                candidates.add(int(name))
    return sorted(pid for pid in candidates if _is_live_bot_process(pid))


def _write_pid(pid):
    temporary = f"{PID_PATH}.{os.getpid()}.tmp"
    with open(temporary, "w") as f:
        f.write(str(pid))
        f.flush()
        os.fsync(f.fileno())
    os.replace(temporary, PID_PATH)


def bot_pid():
    pids = bot_pids()
    if not pids:
        try:
            os.remove(PID_PATH)
        except FileNotFoundError:
            pass
        return None

    # Repair a missing/stale PID file. Returning any live PID prevents an
    # unsafe second start; watchdog separately alerts if len(bot_pids()) > 1.
    current = None
    try:
        with open(PID_PATH) as f:
            current = int(f.read().strip())
    except (ValueError, OSError):
        pass
    if current != pids[0]:
        _write_pid(pids[0])
    return pids[0]


def start_bot():
    os.makedirs(os.path.dirname(START_LOCK_PATH), exist_ok=True)
    with open(START_LOCK_PATH, "a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        if bot_pid():
            return
        # 2026-07-22: no longer redirects the child's stdout to BOT_LOG_PATH via
        # a file handle opened HERE. bot.py now owns that file itself through
        # its own RotatingFileHandler (see bot.py's module-level logger setup).
        proc = subprocess.Popen(
            [sys.executable, "-u", "bot.py"],
            cwd=config.BASE_DIR,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _write_pid(proc.pid)


def stop_bot():
    pid = bot_pid()
    if not pid:
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass
    # Deliberately do NOT delete the pid file here. bot.py finishes its
    # in-flight work before exiting (see its SIGTERM handler), so the
    # process is genuinely still running for a few seconds — deleting the
    # pid file immediately (a) lied about that in the UI, and (b) orphaned
    # the pid from tracking, so bot_pid() never got the chance to reap the
    # exited child and it stayed a zombie for the dashboard's lifetime.
    # bot_pid() now owns the pid file's cleanup: it removes it on the first
    # status poll after the process actually dies.


def _connect():
    conn = sqlite3.connect(config.SQLITE_PATH)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


def _load_recent_events(conn, limit=200):
    """Returns the same list-of-dicts shape trades_log.json used to be —
    each bot_event_log row's payload_json IS that original event dict
    (append_log in db.py stores json.dumps(event) verbatim), newest first.
    """
    cur = conn.execute(
        "SELECT payload_json FROM bot_event_log ORDER BY timestamp DESC LIMIT ?", (limit,)
    )
    return [json.loads(row["payload_json"]) for row in cur.fetchall()]


def _open_position_count(conn):
    cur = conn.execute(
        "SELECT COUNT(*) c FROM paper_trade "
        "WHERE status = 'open' AND strategy = 'bot_filtered' AND is_demo_data = 0"
    )
    return cur.fetchone()["c"]


def build_status():
    conn = _connect()
    try:
        recent_events = _load_recent_events(conn, limit=200)
        open_positions = _open_position_count(conn)

        # trades_executed/wins/losses/realized_pnl need the FULL event
        # history for correct totals, not just the 200-row display window —
        # query bot_event_log directly for those, same event_type buckets
        # build_status always used.
        BUY_EVENTS = {"paper_buy", "live_buy"}
        CLOSE_EVENTS = {"paper_sell", "live_sell",
                        "paper_sell_trailing_tp", "live_sell_trailing_tp",
                        "position_resolved"}

        trades_executed = 0
        wins = 0
        losses = 0
        realized_pnl = 0.0
        unresolved_count = 0
        error_count = 0
        skip_count = 0
        unknown_fill_count = 0

        cur = conn.execute("SELECT event_type, payload_json FROM bot_event_log")
        for row in cur.fetchall():
            et = row["event_type"] or ""
            if et in BUY_EVENTS or et in CLOSE_EVENTS:
                trades_executed += 1
            if et in CLOSE_EVENTS:
                payload = json.loads(row["payload_json"])
                pnl = payload.get("pnl_usd", 0.0) or 0.0
                realized_pnl += pnl
                if pnl > 0:
                    wins += 1
                else:
                    losses += 1
            elif et == "unresolved_trade":
                unresolved_count += 1
            elif et == "error":
                error_count += 1
            elif et.startswith("skip_"):
                skip_count += 1
            elif et == "unknown_fill_state":
                unknown_fill_count += 1

        # P0 ledger integrity (2026-08-07): event history remains useful for
        # operational counts, but its raw close PnL includes confirmed
        # phantom rows. Economic totals and win rate come from db.py's
        # classification-aware readers instead.
        clean_stats = get_closed_trade_stats_since(0)
        closed = clean_stats["closed_count"]
        wins = clean_stats["wins"]
        losses = clean_stats["losses"]
        win_rate = clean_stats["win_rate"] * 100 if clean_stats["win_rate"] is not None else None
        realized_pnl = realized_pnl_total()

        # Reflects whichever source config.TRACKED_TRADERS_SOURCE is
        # currently set to (static config.py dict or wallet_profile), same
        # as bot.py itself uses — see db.get_tracked_traders. Falls back to
        # the static dict on error (e.g. MIN_TRACKED_TRADERS underrun in
        # "db" mode) rather than 500ing this read-only status endpoint;
        # bot.py's own startup check is the actual enforcement point.
        try:
            tracked_traders = get_tracked_traders()
        except Exception:
            tracked_traders = config.TRACKED_TRADERS

        return {
            "bot_running": bot_pid() is not None,
            "mode": "live" if config.LIVE_MODE else "paper",
            "tracked_traders": tracked_traders,
            "trade_size_usd": {
                "min": config.MIN_TRADE_USD, "max": config.MAX_TRADE_USD, "base_if_unscored": config.BASE_TRADE_USD,
            },
            "stats": {
                "realized_pnl_usd": round(realized_pnl, 4),
                "win_rate": round(win_rate, 1) if win_rate is not None else None,
                "trades_executed": trades_executed,
                "wins": wins,
                "losses": losses,
                "open_positions": open_positions,
                "unresolved_count": unresolved_count,
                "error_count": error_count,
                "skip_count": skip_count,
                "unknown_fill_count": unknown_fill_count,
            },
            "trades": recent_events,
        }
    finally:
        conn.close()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send_json(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            with open(INDEX_PATH, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/status":
            try:
                self._send_json(build_status())
            except Exception as e:
                self._send_json({"error": str(e)}, status=500)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/api/toggle":
            try:
                if bot_pid():
                    stop_bot()
                else:
                    start_bot()
                self._send_json(build_status())
            except Exception as e:
                self._send_json({"error": str(e)}, status=500)
        else:
            self.send_response(404)
            self.end_headers()


def main():
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}"
    logger.info(f"Copybot dashboard running at {url}")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Dashboard stopped.")


if __name__ == "__main__":
    main()
