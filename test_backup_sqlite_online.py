#!/usr/bin/env python3
from collections import namedtuple
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from backup_sqlite_online import create_backup, validate_capacity


DiskUsage = namedtuple("usage", "total used free")


class TestOnlineSqliteBackup(unittest.TestCase):
    def test_capacity_rejects_projected_85_percent_breach(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "app.db"
            db_path.write_bytes(b"x" * 100)
            output = Path(tmp) / "backup.db"
            with patch("shutil.disk_usage", return_value=DiskUsage(1000, 800, 200)):
                with self.assertRaises(RuntimeError):
                    validate_capacity(db_path, output)

    def test_backup_api_produces_integrity_checked_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "app.db"
            output = Path(tmp) / "backup.db"
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE evidence(id INTEGER PRIMARY KEY,value TEXT)")
            conn.execute("INSERT INTO evidence(value) VALUES('truth')")
            conn.commit()
            conn.close()
            result = create_backup(db_path, output)
            self.assertEqual(result["integrity_check"], "ok")
            check = sqlite3.connect(output)
            self.assertEqual(check.execute("SELECT value FROM evidence").fetchone()[0], "truth")
            check.close()


if __name__ == "__main__":
    unittest.main()
