import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from utils import simpleverify_store as store


class SimpleVerifyStore(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)

    def tearDown(self):
        os.remove(self.db)

    def test_defaults_when_unconfigured(self):
        cfg = store.get(123, db=self.db)
        self.assertEqual(cfg["enabled"], 0)
        self.assertIsNone(cfg["unverified_role_id"])
        self.assertFalse(store.is_ready(cfg))

    def test_not_ready_until_roles_channel_and_enabled(self):
        store.set_roles(1, 10, 20, db=self.db)
        self.assertFalse(store.is_ready(store.get(1, db=self.db)))   # no channel yet
        store.set_channel(1, 30, db=self.db)
        self.assertFalse(store.is_ready(store.get(1, db=self.db)))   # not enabled yet
        store.set_enabled(1, True, db=self.db)
        cfg = store.get(1, db=self.db)
        self.assertTrue(store.is_ready(cfg))
        self.assertEqual((cfg["unverified_role_id"], cfg["verified_role_id"], cfg["channel_id"]),
                         (10, 20, 30))

    def test_disable_keeps_settings(self):
        store.set_roles(2, 10, 20, db=self.db)
        store.set_channel(2, 30, db=self.db)
        store.set_enabled(2, True, db=self.db)
        store.disable(2, db=self.db)
        cfg = store.get(2, db=self.db)
        self.assertEqual(cfg["enabled"], 0)
        self.assertEqual(cfg["unverified_role_id"], 10)   # not wiped
        self.assertFalse(store.is_ready(cfg))

    def test_panel_message_roundtrip_and_clear(self):
        store.set_panel_message(3, 999, db=self.db)
        self.assertEqual(store.get(3, db=self.db)["panel_message_id"], 999)
        store.set_panel_message(3, None, db=self.db)
        self.assertIsNone(store.get(3, db=self.db)["panel_message_id"])

    def test_guilds_are_isolated(self):
        store.set_roles(1, 10, 20, db=self.db)
        store.set_roles(2, 11, 21, db=self.db)
        self.assertEqual(store.get(1, db=self.db)["unverified_role_id"], 10)
        self.assertEqual(store.get(2, db=self.db)["unverified_role_id"], 11)

    def test_unknown_column_rejected(self):
        with self.assertRaises(ValueError):
            store._update(1, db=self.db, bogus=1)


if __name__ == "__main__":
    unittest.main()
