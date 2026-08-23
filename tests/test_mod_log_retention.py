"""Retention sweep + per-guild media layout (2026-08-23, the Quark-shaped
tiers). Runs the cog's blocking sweep helpers against a throwaway messages.db
and media_cache, no live Discord.

    PYTHONIOENCODING=utf-8 python tests/test_mod_log_retention.py
"""
import os
import sys
import time
import shutil
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

import cogs.mod_log as ml  # noqa: E402

NOW = time.time()
H = 3600
D = 86400
OPERATOR, PRO, FREE, LAPSED = "111", "222", "444", "777"


class _Bot:
    user = type("U", (), {"id": 1})()


def _touch(path, age_s, size=10):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"x" * size)
    os.utime(path, (NOW - age_s, NOW - age_s))


class RetentionSweep(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="modlog_ret_")
        self._db, self._media = ml.DB_PATH, ml.MEDIA_DIR
        ml.DB_PATH = os.path.join(self.tmp, "messages.db")
        ml.MEDIA_DIR = os.path.join(self.tmp, "media_cache")
        ml.ARCHIVE_GUILDS = {OPERATOR}
        ml.OPERATOR_GUILDS = {OPERATOR}
        self.cog = ml.ModLog(_Bot())   # __init__ reloads the caches from the (empty) DB
        ml._CONSENT[PRO] = {"version": ml.TERMS_VERSION}
        ml._PRO[PRO] = {"expires_ts": NOW + 30 * D}
        ml._CONSENT[LAPSED] = {"version": ml.TERMS_VERSION}
        ml._PRO[LAPSED] = {"expires_ts": NOW - (ml.PRO_GRACE_DAYS + 1) * D}  # past grace
        # rows: one fresh + one old per guild
        rows = []
        for gid in (OPERATOR, PRO, FREE, LAPSED):
            for age, tag in ((1 * H, "fresh"), (2 * D, "2d"), (120 * D, "120d")):
                rows.append({"message_id": f"{gid}{tag}", "guild_id": gid, "channel_id": "1",
                             "author_id": "9", "author_name": "a", "bot": 0, "webhook": 0,
                             "created_ts": NOW - age, "content": tag, "reply_to": None,
                             "attachments": None, "stickers": None})
        for r in rows:
            self.cog._remember(r)
        self.cog._flush()
        with self.cog._conn() as c:
            c.execute("INSERT INTO edits VALUES (?,?,?,?,?)", (f"{FREE}2d", FREE, NOW - 2 * D, "a", "b"))
            c.execute("INSERT INTO identity_events (ts,guild_id,uid,kind) VALUES (?,?,?,?)",
                      (NOW - 2 * D, LAPSED, "u1", "join"))
            c.execute("INSERT INTO identity_events (ts,guild_id,uid,kind) VALUES (?,?,?,?)",
                      (NOW - 2 * D, PRO, "u2", "join"))
        # files: per-guild dirs + a legacy flat one
        for gid in (OPERATOR, PRO, FREE):
            _touch(os.path.join(ml.MEDIA_DIR, gid, f"{gid}fresh_0_a.png"), 1 * H)
            _touch(os.path.join(ml.MEDIA_DIR, gid, f"{gid}2d_0_b.png"), 2 * D)
            _touch(os.path.join(ml.MEDIA_DIR, gid, f"{gid}120d_0_c.png"), 120 * D)
        _touch(os.path.join(ml.MEDIA_DIR, "legacy_0_old.png"), 120 * D)
        _touch(os.path.join(ml.MEDIA_DIR, "legacy_0_new.png"), 1 * D)

    def tearDown(self):
        ml.DB_PATH, ml.MEDIA_DIR = self._db, self._media
        ml._CONSENT.clear()
        ml._PRO.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _ids(self, gid):
        with self.cog._conn() as c:
            return {r[0] for r in c.execute("SELECT message_id FROM messages WHERE guild_id=?", (gid,))}

    def test_rows_follow_each_guilds_window(self):
        self.cog._sweep_rows(NOW)
        self.assertEqual(self._ids(OPERATOR), {f"{OPERATOR}fresh", f"{OPERATOR}2d", f"{OPERATOR}120d"},
                         "operator guild is never swept")
        self.assertEqual(self._ids(PRO), {f"{PRO}fresh", f"{PRO}2d"}, "pro keeps 90d, drops 120d")
        self.assertEqual(self._ids(FREE), {f"{FREE}fresh"}, "free keeps only the 24h window")
        self.assertEqual(self._ids(LAPSED), {f"{LAPSED}fresh"}, "lapsed past grace = free window")
        with self.cog._conn() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM edits WHERE guild_id=?", (FREE,)).fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM identity_events WHERE guild_id=?",
                                       (LAPSED,)).fetchone()[0], 0, "ledger is a Pro feature")
            self.assertEqual(c.execute("SELECT COUNT(*) FROM identity_events WHERE guild_id=?",
                                       (PRO,)).fetchone()[0], 1)

    def test_in_memory_rows_do_not_outlive_db_rows(self):
        self.cog._sweep_rows(NOW)
        self.assertIn(f"{FREE}fresh", self.cog._recent)
        self.assertNotIn(f"{FREE}2d", self.cog._recent)
        self.assertIn(f"{OPERATOR}120d", self.cog._recent)

    def test_lapsed_inside_grace_keeps_pro_window(self):
        ml._PRO[LAPSED] = {"expires_ts": NOW - 1 * D}   # lapsed yesterday, grace = 3d
        self.cog._sweep_rows(NOW)
        self.assertEqual(self._ids(LAPSED), {f"{LAPSED}fresh", f"{LAPSED}2d"})

    def test_files_follow_each_guilds_window(self):
        removed = self.cog._sweep_files(NOW)
        left = {os.path.relpath(p, ml.MEDIA_DIR).replace(os.sep, "/") for p, _, _ in self.cog._all_media_entries()}
        self.assertIn(f"{OPERATOR}/{OPERATOR}2d_0_b.png", left, "operator media kept for MEDIA_DAYS")
        self.assertNotIn(f"{OPERATOR}/{OPERATOR}120d_0_c.png", left, "operator media past MEDIA_DAYS goes")
        self.assertIn(f"{PRO}/{PRO}2d_0_b.png", left)
        self.assertNotIn(f"{PRO}/{PRO}120d_0_c.png", left)
        self.assertIn(f"{FREE}/{FREE}fresh_0_a.png", left)
        self.assertNotIn(f"{FREE}/{FREE}2d_0_b.png", left, "free files swept after 24h")
        self.assertIn("legacy_0_new.png", left, "flat layout aged by the operator window")
        self.assertNotIn("legacy_0_old.png", left)
        self.assertEqual(removed, 5)  # op 120d, pro 120d, free 2d + 120d, legacy old

    def test_cached_media_finds_both_layouts(self):
        found = self.cog._cached_media(f"{FREE}fresh", FREE)
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].endswith(f"{FREE}fresh_0_a.png"))
        self.assertEqual(len(self.cog._cached_media("legacy")), 2)
        self.assertEqual(len(self.cog._cached_media(f"{PRO}fresh")), 1, "guild-less lookup still finds subdirs")

    def test_guild_byte_accounting(self):
        self.assertEqual(self.cog._guild_media_bytes(FREE), 30)
        path = os.path.join(ml.MEDIA_DIR, FREE, f"{FREE}2d_0_b.png")
        self.assertTrue(self.cog._remove_media_file(path))
        self.assertEqual(self.cog._guild_media_bytes(FREE), 20)
        self.assertFalse(self.cog._remove_media_file(path), "second remove is a no-op")

    def test_purge_guild_removes_its_directory(self):
        counts = self.cog._purge_guild(PRO)
        self.assertEqual(counts["messages"], 3)
        self.assertEqual(counts["files"], 3)
        self.assertFalse(os.path.isdir(os.path.join(ml.MEDIA_DIR, PRO)))
        self.assertTrue(os.path.isdir(os.path.join(ml.MEDIA_DIR, FREE)), "other guilds untouched")

    def test_windows(self):
        t, m = self.cog._guild_windows(OPERATOR, NOW)
        self.assertIsNone(t)
        t, m = self.cog._guild_windows(FREE, NOW)
        self.assertAlmostEqual(NOW - t, ml.RECENT_HOURS * H, delta=1)
        self.assertAlmostEqual(NOW - m, ml.RECENT_HOURS * H, delta=1)
        t, m = self.cog._guild_windows(PRO, NOW)
        self.assertAlmostEqual(NOW - t, ml.PRO_TEXT_DAYS * D, delta=1)
        self.assertAlmostEqual(NOW - m, ml.PRO_MEDIA_DAYS * D, delta=1)


if __name__ == "__main__":
    unittest.main(verbosity=1)
