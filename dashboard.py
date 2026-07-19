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
import os
import signal
import sqlite3
import subprocess
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import config
from db import get_tracked_traders

PORT = 8787
PID_PATH = os.path.join(config.BASE_DIR, "bot.pid")
BOT_LOG_PATH = os.path.join(config.BASE_DIR, "bot.out.log")
INDEX_PATH = os.path.join(config.BASE_DIR, "static", "index.html")


def bot_pid():
    if not os.path.exists(PID_PATH):
        return None
    try:
        with open(PID_PATH) as f:
            pid = int(f.read().strip())
    except (ValueError, OSError):
        return None

    # Reap the bot if it's an exited child of THIS process. start_bot()
    # discards its Popen object, so nothing ever wait()s on the child —
    # when bot.py exits, it lingers as a zombie, and zombies pass the
    # os.kill(pid, 0) existence check below, making a dead bot report as
    # "running" forever (observed 2026-07-18: a cleanly-SIGTERM'd bot.py
    # showed ZN/<defunct> in ps while this function kept returning its pid).
    try:
        os.waitpid(pid, os.WNOHANG)
    except OSError:
        pass  # not our child (bot started manually or by a prior dashboard run)

    try:
        os.kill(pid, 0)
    except OSError:
        os.remove(PID_PATH)
        return None

    # os.kill(pid, 0) also succeeds on zombies parented to OTHER processes
    # (which we cannot reap) — check the real process state and treat
    # Z/defunct as dead. `ps -o stat=` is Darwin/Linux-portable.
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "stat="],
            capture_output=True, text=True, timeout=5,
        )
        if result.stdout.strip().startswith("Z"):
            os.remove(PID_PATH)
            return None
    except Exception:
        pass  # ps unavailable/slow — fall back on the existence check above

    return pid


def start_bot():
    if bot_pid():
        return
    log_f = open(BOT_LOG_PATH, "a")
    # -u matches how the bot is started manually: unbuffered stdout, so
    # bot.out.log updates line-by-line instead of in block-buffered bursts
    # (buffered output caused a false "bot is silent/frozen" reading on
    # 2026-07-18 — keep these two launch paths identical).
    proc = subprocess.Popen(
        [sys.executable, "-u", "bot.py"],
        cwd=config.BASE_DIR,
        stdout=log_f,
        stderr=subprocess.STDOUT,
    )
    with open(PID_PATH, "w") as f:
        f.write(str(proc.pid))


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

        closed = wins + losses
        win_rate = (wins / closed * 100) if closed else None

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
            "fixed_trade_usd": config.FIXED_TRADE_USD,
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
    print(f"Copybot dashboard running at {url}")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")


if __name__ == "__main__":
    main()
