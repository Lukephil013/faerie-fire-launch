"""Rotating memory.db backups (livingpc/backup.py)."""
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from livingpc.backup import backup_memory, default_backup_dir, _snapshots
from livingpc.memory import MemoryStore


class TestBackup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "memory.db")
        self.backups = os.path.join(self.tmp.name, "backups")
        mem = MemoryStore(self.db)
        mem.add("projects", "current", "faerie fire")
        mem.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_snapshot_is_a_valid_readable_copy(self):
        result = backup_memory(self.db, self.backups, keep=5)
        self.assertTrue(os.path.exists(result["path"]))
        conn = sqlite3.connect(result["path"])
        try:
            n = conn.execute("SELECT COUNT(*) FROM memory").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(n, 1)

    def test_backup_works_while_db_is_open(self):
        mem = MemoryStore(self.db)   # daemon holds the DB open
        try:
            result = backup_memory(self.db, self.backups, keep=5)
        finally:
            mem.close()
        self.assertTrue(os.path.exists(result["path"]))

    def test_rotation_prunes_oldest_beyond_keep(self):
        for i in range(5):
            backup_memory(self.db, self.backups, keep=3,
                          now=datetime(2026, 7, 1, 12, 0, i))
        names = _snapshots(self.backups)
        self.assertEqual(len(names), 3)
        # oldest two (seconds 0 and 1) were pruned; newest three remain
        self.assertEqual(names, ["memory-20260701-120002.db",
                                 "memory-20260701-120003.db",
                                 "memory-20260701-120004.db"])

    def test_salt_copied_alongside_once(self):
        salt = os.path.join(self.tmp.name, "secret.salt")
        with open(salt, "wb") as f:
            f.write(b"\x01\x02")
        first = backup_memory(self.db, self.backups, keep=5,
                              now=datetime(2026, 7, 1, 12, 0, 0))
        second = backup_memory(self.db, self.backups, keep=5,
                               now=datetime(2026, 7, 1, 12, 0, 1))
        self.assertTrue(first["salt_copied"])
        self.assertFalse(second["salt_copied"])
        self.assertTrue(os.path.exists(os.path.join(self.backups, "secret.salt")))

    def test_missing_db_raises(self):
        with self.assertRaises(FileNotFoundError):
            backup_memory(os.path.join(self.tmp.name, "nope.db"), self.backups)

    def test_default_dir_sits_next_to_db(self):
        self.assertEqual(default_backup_dir(self.db),
                         os.path.join(self.tmp.name, "backups"))


if __name__ == "__main__":
    unittest.main()
