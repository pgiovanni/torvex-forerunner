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

    def test_text_is_never_swept_on_any_tier(self):
        # 2026-09-10 (Paul): text is never deleted by time — the free 24h text
        # window let Dismiss's #general wipe erase the audit trail. Tiers now
        # govern FILES only. Every row, every tier, every age survives.
        self.assertEqual(self.cog._sweep_rows(NOW), {})
        for gid in (OPERATOR, PRO, FREE, LAPSED):
            self.assertEqual(self._ids(gid), {f"{gid}fresh", f"{gid}2d", f"{gid}120d"},
                             f"{gid}: text rows must all survive the sweep")
        with self.cog._conn() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM edits WHERE guild_id=?", (FREE,)).fetchone()[0], 1)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM identity_events WHERE guild_id=?",
                                       (LAPSED,)).fetchone()[0], 1, "ledger text is kept on free too")
            self.assertEqual(c.execute("SELECT COUNT(*) FROM identity_events WHERE guild_id=?",
                                       (PRO,)).fetchone()[0], 1)

    def test_in_memory_rows_survive_too(self):
        self.cog._sweep_rows(NOW)
        self.assertIn(f"{FREE}fresh", self.cog._recent)
        self.assertIn(f"{FREE}2d", self.cog._recent)
        self.assertIn(f"{OPERATOR}120d", self.cog._recent)

    def test_lapsed_inside_grace_keeps_pro_file_window(self):
        ml._PRO[LAPSED] = {"expires_ts": NOW - 1 * D}   # lapsed yesterday, grace = 3d
        t, m = self.cog._guild_windows(LAPSED, NOW)
        self.assertIsNone(t)
        self.assertAlmostEqual(NOW - m, ml.PRO_MEDIA_DAYS * D, delta=1)
        ml._PRO[LAPSED] = {"expires_ts": NOW - (ml.PRO_GRACE_DAYS + 1) * D}
        t, m = self.cog._guild_windows(LAPSED, NOW)
        self.assertIsNone(t)
        self.assertAlmostEqual(NOW - m, ml.RECENT_HOURS * H, delta=1, msg="past grace = free file window")

    def test_purge_still_removes_text(self):
        # "never swept by time" is not "never deletable": guild purge is the exit.
        self.cog._purge_guild(FREE)
        self.assertEqual(self._ids(FREE), set())

    def test_files_follow_each_guilds_window(self):
        self.cog._index_existing_media()   # rows exist so the sweep can mark them
        removed = self.cog._sweep_files(NOW)
        left = {os.path.relpath(p, ml.MEDIA_DIR).replace(os.sep, "/") for p, _, _ in self.cog._all_media_entries()}
        self.assertIn(f"{OPERATOR}/{OPERATOR}2d_0_b.png", left, "operator media is never age-swept")
        self.assertIn(f"{OPERATOR}/{OPERATOR}120d_0_c.png", left, "operator media kept until reviewed by hand")
        self.assertIn(f"{PRO}/{PRO}2d_0_b.png", left)
        self.assertNotIn(f"{PRO}/{PRO}120d_0_c.png", left)
        self.assertIn(f"{FREE}/{FREE}fresh_0_a.png", left)
        self.assertNotIn(f"{FREE}/{FREE}2d_0_b.png", left, "free files swept after 24h")
        self.assertIn("legacy_0_new.png", left, "flat layout = operator, kept")
        self.assertIn("legacy_0_old.png", left)
        self.assertEqual(removed, 3)  # pro 120d, free 2d + 120d
        with self.cog._conn() as c:
            swept = {r[0]: r[1] for r in c.execute("SELECT path, swept_reason FROM media_index WHERE swept_ts IS NOT NULL")}
        self.assertEqual(len(swept), 3, "every removed file keeps its index row, marked swept")
        self.assertTrue(all(v == "window" for v in swept.values()))

    def test_cap_never_evicts_operator_files(self):
        ml.MEDIA_CAP_GB = 1
        self.cog._all_media_entries = lambda: [
            (os.path.join(ml.MEDIA_DIR, OPERATOR, "a"), NOW - 9 * D, 900 * 1024 ** 3),   # oldest, protected
            (os.path.join(ml.MEDIA_DIR, "legacy_0_x.png"), NOW - 8 * D, 50 * 1024 ** 3),  # flat = protected
            (os.path.join(ml.MEDIA_DIR, FREE, "b"), NOW - 7 * D, 100 * 1024 ** 3),        # evictable
            (os.path.join(ml.MEDIA_DIR, FREE, "c"), NOW - 1 * D, 10 * 1024 ** 3),
        ]
        gone = []
        self.cog._remove_media_file = lambda p, reason="window": gone.append((p, reason)) or True
        self.cog._enforce_media_cap()
        self.assertEqual([os.path.basename(p) for p, _ in gone], ["b", "c"],
                         "only tenant files go, oldest first, even though the operator's alone exceed the cap")
        self.assertTrue(all(r == "cap" for _, r in gone))

    def test_media_index_records_and_backfills(self):
        # startup pass hashes files that predate the index; a second pass adds nothing
        added = self.cog._index_existing_media()
        self.assertEqual(added, 11)   # 3 guilds x 3 files + 2 legacy
        self.assertEqual(self.cog._index_existing_media(), 0)
        with self.cog._conn() as c:
            r = dict(c.execute("SELECT * FROM media_index WHERE path=?",
                               (os.path.join(ml.MEDIA_DIR, FREE, f"{FREE}fresh_0_a.png"),)).fetchone())
        self.assertEqual((r["guild_id"], r["message_id"], r["kind"], r["size"]), (FREE, f"{FREE}fresh", "attachment", 10))
        self.assertEqual(r["author_id"], "9", "author/channel looked up from the archive row")
        self.assertEqual(len(r["sha256"]), 64)
        self.assertIsNone(r["swept_ts"])
        # removing the bytes keeps the row
        self.cog._remove_media_file(os.path.join(ml.MEDIA_DIR, FREE, f"{FREE}fresh_0_a.png"), "reposted")
        with self.cog._conn() as c:
            r2 = dict(c.execute("SELECT swept_reason, sha256 FROM media_index WHERE path=?",
                                (os.path.join(ml.MEDIA_DIR, FREE, f"{FREE}fresh_0_a.png"),)).fetchone())
        self.assertEqual(r2["swept_reason"], "reposted")
        self.assertEqual(r2["sha256"], r["sha256"])
        # a requested purge drops the guild's index rows
        self.cog._purge_guild(FREE)
        with self.cog._conn() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM media_index WHERE guild_id=?", (FREE,)).fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM media_index WHERE guild_id=?", (PRO,)).fetchone()[0], 3)

    def test_media_path_meta(self):
        self.assertEqual(ml.media_path_meta("/x/media_cache/123/456_0_pic.png"), ("123", "456", "attachment"))
        self.assertEqual(ml.media_path_meta("/x/media_cache/123/456_s1_peepo.png"), ("123", "456", "sticker"))
        self.assertEqual(ml.media_path_meta("/x/media_cache/789_2_a_b.gif"), (None, "789", "attachment"))
        self.assertEqual(ml.media_path_meta("/x/media_cache/weird.bin"), (None, None, "attachment"))

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
        self.assertIsNone(m, "operator files: kept until reviewed by hand")
        t, m = self.cog._guild_windows(FREE, NOW)
        self.assertIsNone(t, "text window: none, on every tier")
        self.assertAlmostEqual(NOW - m, ml.RECENT_HOURS * H, delta=1)
        t, m = self.cog._guild_windows(PRO, NOW)
        self.assertIsNone(t)
        self.assertAlmostEqual(NOW - m, ml.PRO_MEDIA_DAYS * D, delta=1)


if __name__ == "__main__":
    unittest.main(verbosity=1)
