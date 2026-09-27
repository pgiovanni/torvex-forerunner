import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from utils import guild_blocklist as store


class GuildBlocklistStore(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)

    def tearDown(self):
        os.remove(self.db)

    def test_empty(self):
        self.assertFalse(store.is_blocked(1, db=self.db))
        self.assertIsNone(store.get(1, db=self.db))
        self.assertEqual(store.all_blocked(db=self.db), [])

    def test_block_and_unblock(self):
        e = store.block(1394876181739995226, "account + cheat shop", added_by=42,
                        guild_name="BlackNova", db=self.db, now=100.0)
        self.assertEqual(e["guild_id"], 1394876181739995226)
        self.assertEqual(e["reason"], "account + cheat shop")
        self.assertEqual(e["added_by"], "42")
        self.assertEqual(e["guild_name"], "BlackNova")
        self.assertEqual(e["added_ts"], 100.0)
        self.assertTrue(store.is_blocked("1394876181739995226", db=self.db))   # str id works too
        self.assertTrue(store.unblock(1394876181739995226, db=self.db))
        self.assertFalse(store.unblock(1394876181739995226, db=self.db))      # second time: nothing
        self.assertFalse(store.is_blocked(1394876181739995226, db=self.db))

    def test_reblock_updates_reason_keeps_name(self):
        store.block(5, "first", guild_name="Named", db=self.db, now=1.0)
        e = store.block(5, "  second   reason  ", db=self.db, now=2.0)
        self.assertEqual(e["reason"], "second reason")     # whitespace collapsed
        self.assertEqual(e["guild_name"], "Named")         # not wiped by a None
        self.assertEqual(len(store.all_blocked(db=self.db)), 1)

    def test_reason_required_and_capped(self):
        with self.assertRaises(ValueError):
            store.block(5, "   ", db=self.db)
        e = store.block(5, "x" * 1000, db=self.db)
        self.assertEqual(len(e["reason"]), store.MAX_REASON)

    def test_all_blocked_newest_first(self):
        store.block(1, "a", db=self.db, now=10.0)
        store.block(2, "b", db=self.db, now=30.0)
        store.block(3, "c", db=self.db, now=20.0)
        self.assertEqual([r["guild_id"] for r in store.all_blocked(db=self.db)], [2, 3, 1])

    def test_parse_guild_id(self):
        self.assertEqual(store.parse_guild_id("1394876181739995226"), 1394876181739995226)
        self.assertEqual(store.parse_guild_id(" `1394876181739995226` "), 1394876181739995226)
        self.assertIsNone(store.parse_guild_id("BlackNova"))
        self.assertIsNone(store.parse_guild_id("123"))          # too short to be a snowflake
        self.assertIsNone(store.parse_guild_id(None))
        self.assertIsNone(store.parse_guild_id("<@1394876181739995226>"))  # a user mention, not an id



class ReaddPolicy(unittest.TestCase):
    def test_notice_is_one_line_with_the_policy_link(self):
        self.assertNotIn("\n", store.NOTICE_TEXT)
        self.assertIn("torvex.app/TrustSafety", store.NOTICE_TEXT)
        self.assertLess(len(store.NOTICE_TEXT), 200)


if __name__ == "__main__":
    unittest.main()
