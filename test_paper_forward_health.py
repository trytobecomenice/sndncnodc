import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import check_paper_forward_health as health


class PaperForwardHealthTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "app.db"
        self.phase0_path = Path(self.temp.name) / "phase0.jsonl"
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            "CREATE TABLE wallet_profile(status TEXT,circuit_breaker_muted INTEGER);"
            "CREATE TABLE bot_event_log(timestamp INTEGER,event_type TEXT);"
            "CREATE TABLE decision_journal(created_at INTEGER,score_breakdown_json TEXT);"
            "CREATE TABLE bot_risk_state(key TEXT,value_json TEXT);"
        )
        conn.executemany(
            "INSERT INTO wallet_profile VALUES ('track',0)", [(), (), ()]
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        self.temp.cleanup()

    def _write_phase0(self, events, mtime=1000):
        self.phase0_path.write_text(
            "".join(json.dumps({"event_type": event}) + "\n" for event in events)
        )
        os.utime(self.phase0_path, (mtime, mtime))

    @mock.patch.object(health, "_matching_pids", return_value=[123])
    def test_healthy_paper_observation(self, _pids):
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO bot_event_log VALUES (950,'paper_buy')")
        conn.execute(
            "INSERT INTO decision_journal VALUES (?,?)",
            (950, json.dumps({"sizing_tier": "unprovenanced", "trade_size_usd": 3.0})),
        )
        conn.commit()
        conn.close()
        self._write_phase0(["poll_cycle", "wallet_signal"])

        result = health.collect_health(self.db_path, self.phase0_path, 900, now=1000)

        self.assertTrue(result["ok"])
        self.assertEqual(result["sizing_tiers_since_start"], {"unprovenanced": 1})

    @mock.patch.object(health, "_matching_pids", return_value=[123])
    def test_wrong_size_live_event_and_disconnect_only_fail(self, _pids):
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO bot_event_log VALUES (950,'live_buy')")
        conn.execute(
            "INSERT INTO decision_journal VALUES (?,?)",
            (950, json.dumps({"sizing_tier": "unprovenanced", "trade_size_usd": 5.0})),
        )
        conn.commit()
        conn.close()
        self._write_phase0(["ws_disconnected"] * 500)

        result = health.collect_health(self.db_path, self.phase0_path, 900, now=1000)

        self.assertFalse(result["ok"])
        self.assertEqual(result["wrong_unprovenanced_size_count"], 1)
        self.assertEqual(result["live_event_count"], 1)
        self.assertTrue(any("all ws_disconnected" in item for item in result["failures"]))

    @mock.patch.object(health, "_matching_pids", return_value=[123])
    def test_persisted_risk_latches_fail_the_gate(self, _pids):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO bot_risk_state VALUES ('kill_switch',?)",
            (json.dumps({"reasons": ["bad equity"]}),),
        )
        conn.execute(
            "INSERT INTO bot_risk_state VALUES ('entry_interlock',?)",
            (json.dumps({"active": True, "reasons": ["ledger_integrity"]}),),
        )
        conn.commit()
        conn.close()
        self._write_phase0(["poll_cycle"])

        result = health.collect_health(self.db_path, self.phase0_path, 900, now=1000)

        self.assertFalse(result["ok"])
        self.assertTrue(any("kill_switch" in item for item in result["failures"]))
        self.assertTrue(any("entry_interlock" in item for item in result["failures"]))


if __name__ == "__main__":
    unittest.main()
