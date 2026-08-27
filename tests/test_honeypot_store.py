import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from utils import honeypot_store as store


class HoneypotStore(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)

    def tearDown(self):
        os.remove(self.db)

    def test_defaults(self):
        cfg = store.get(1, db=self.db)
        self.assertEqual(cfg["enabled"], 0)
        self.assertEqual(cfg["action"], "timeout")
        self.assertEqual(cfg["timeout_minutes"], store.DEFAULT_TIMEOUT_MIN)
        self.assertFalse(store.is_armed(cfg))

    def test_arming_needs_channel_and_enabled(self):
        store.set_channel(1, 500, db=self.db)
        self.assertFalse(store.is_armed(store.get(1, db=self.db)))   # channel set, not enabled
        store.set_enabled(1, True, db=self.db)
        self.assertTrue(store.is_armed(store.get(1, db=self.db)))

    def test_set_channel_does_not_auto_enable(self):
        # Re-pointing a trap must not silently arm it — arming is a separate step.
        store.set_channel(2, 600, db=self.db)
        self.assertEqual(store.get(2, db=self.db)["enabled"], 0)

    def test_action_validation(self):
        for a in ("timeout", "kick", "ban"):
            store.set_action(3, a, db=self.db)
            self.assertEqual(store.get(3, db=self.db)["action"], a)
        with self.assertRaises(ValueError):
            store.set_action(3, "explode", db=self.db)

    def test_timeout_minutes_clamped(self):
        store.set_action(4, "timeout", timeout_minutes=99999999, db=self.db)
        self.assertEqual(store.get(4, db=self.db)["timeout_minutes"], store.MAX_TIMEOUT_MIN)
        store.set_action(4, "timeout", timeout_minutes=0, db=self.db)
        self.assertEqual(store.get(4, db=self.db)["timeout_minutes"], 1)

    def test_kick_ban_ignore_timeout_minutes(self):
        store.set_action(5, "timeout", timeout_minutes=30, db=self.db)
        store.set_action(5, "ban", db=self.db)
        self.assertEqual(store.get(5, db=self.db)["timeout_minutes"], 30)   # preserved, unused

    def test_log_channel_roundtrip_and_clear(self):
        store.set_log_channel(6, 777, db=self.db)
        self.assertEqual(store.get(6, db=self.db)["log_channel_id"], 777)
        store.set_log_channel(6, None, db=self.db)
        self.assertIsNone(store.get(6, db=self.db)["log_channel_id"])

    def test_disable_keeps_channel(self):
        store.set_channel(7, 800, db=self.db)
        store.set_enabled(7, True, db=self.db)
        store.disable(7, db=self.db)
        cfg = store.get(7, db=self.db)
        self.assertEqual(cfg["enabled"], 0)
        self.assertEqual(cfg["channel_id"], 800)
        self.assertFalse(store.is_armed(cfg))

    def test_delete_messages_toggle(self):
        self.assertEqual(store.get(7, db=self.db)["delete_messages"], 0)
        store.set_delete_messages(7, True, db=self.db)
        self.assertEqual(store.get(7, db=self.db)["delete_messages"], 1)
        store.set_delete_messages(7, False, db=self.db)
        self.assertEqual(store.get(7, db=self.db)["delete_messages"], 0)

    def test_migrates_pre_delete_messages_schema(self):
        # A db created before the column existed must gain it on first open
        # without losing the row.
        import sqlite3
        c = sqlite3.connect(self.db)
        c.execute("""CREATE TABLE honeypot_config (
            guild_id INTEGER PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0,
            channel_id INTEGER, action TEXT NOT NULL DEFAULT 'timeout',
            timeout_minutes INTEGER NOT NULL DEFAULT 60, log_channel_id INTEGER,
            updated_ts INTEGER)""")
        c.execute("INSERT INTO honeypot_config (guild_id, enabled, channel_id) VALUES (8, 1, 900)")
        c.commit(); c.close()
        cfg = store.get(8, db=self.db)
        self.assertEqual(cfg["delete_messages"], 0)
        self.assertTrue(store.is_armed(cfg))
        store.set_delete_messages(8, True, db=self.db)
        self.assertEqual(store.get(8, db=self.db)["delete_messages"], 1)


if __name__ == "__main__":
    unittest.main()
