"""Conduct record store (utils/conduct.py).

What matters here is the difference between CLEARING and ERASING, because the
two look similar from a command line and are opposites in the data:

  clear  -> the row stays, stamped with who cleared it and why. A mod who wipes
            a record leaves their own trail. Evidence files are untouched.
  forget -> the row and its files are really gone. This is the purge path, and
            it must reach disk — evidence on disk is exactly the thing that
            survives a purge everyone believed was complete.

The other invariant under test is guild scoping: an entry id from one server
must never be readable, clearable or countable from another.
"""
import os
import sys
import time
import hashlib
import sqlite3
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Point the store at scratch paths BEFORE importing it.
_TMP = tempfile.mkdtemp(prefix="conduct")
os.environ["TORVEX_CONDUCT_DB"] = os.path.join(_TMP, "conduct.db")
os.environ["TORVEX_CONDUCT_EVIDENCE"] = os.path.join(_TMP, "evidence")

from utils import conduct as store  # noqa: E402

G, G2, U, MOD, BOSS = "111", "222", "999", "555", "777"


def _fake_evidence(entry_id, guild_id, user_id, name="shot.png", data=b"\x89PNG bytes"):
    """Write an evidence file + row directly — save_attachment() needs a live
    discord.Attachment, and what these tests care about is the store, not HTTP.

    Uses the real evidence_path() rather than hand-rolling a name: when the test
    built its own, it stopped matching the purge glob and hid a dead sweep.
    """
    path = store.evidence_path(guild_id, user_id, entry_id, 4242, name)
    os.makedirs(store.EVIDENCE_DIR, exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    with sqlite3.connect(store.DB_PATH) as c:
        c.execute("INSERT INTO evidence(entry_id,guild_id,user_id,filename,path,bytes,"
                  "content_type,sha256,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                  (entry_id, guild_id, user_id, name, path, len(data), "image/png",
                   hashlib.sha256(data).hexdigest(), time.time()))
    return path, data


class ConductStoreTests(unittest.TestCase):

    def setUp(self):
        # Each test starts from an empty store.
        with sqlite3.connect(store.DB_PATH) as c:
            c.execute("DELETE FROM entries")
            c.execute("DELETE FROM evidence")
        for f in os.scandir(store.EVIDENCE_DIR) if os.path.isdir(store.EVIDENCE_DIR) else []:
            os.remove(f.path)

    # ── recording ────────────────────────────────────────────────────────────

    def test_warnings_and_notes_share_one_timeline(self):
        """Positives are first-class: a note is an entry, not a second system."""
        w = store.add_entry(G, U, "warn", "spam in #general", MOD, "mod#1")
        n = store.add_entry(G, U, "note", "apologised, resolved warmly", MOD, "mod#1")
        rows = store.list_entries(G, U)
        self.assertEqual([r["id"] for r in rows], [n, w])  # newest first
        self.assertEqual(rows[0]["kind"], "note")
        c = store.counts(G, U)
        self.assertEqual((c["warns"], c["notes"], c["cleared"]), (1, 1, 0))

    def test_unknown_kind_is_rejected(self):
        with self.assertRaises(ValueError):
            store.add_entry(G, U, "banana", "x", MOD)

    def test_safe_filename_cannot_escape_the_evidence_dir(self):
        for hostile in ("../../etc/passwd", r"..\..\windows\win.ini", "a/b/c.png"):
            safe = store.safe_filename(hostile)
            self.assertNotIn("/", safe)
            self.assertNotIn("\\", safe)
            self.assertNotIn("..", safe)

    # ── guild scoping ────────────────────────────────────────────────────────

    def test_entries_never_leak_across_guilds(self):
        w = store.add_entry(G, U, "warn", "here", MOD)
        store.add_entry(G2, U, "warn", "elsewhere", MOD)
        self.assertEqual(len(store.list_entries(G, U)), 1)
        self.assertEqual(len(store.list_entries(G2, U)), 1)
        # an id from guild G is invisible — and unclearable — from G2
        self.assertIsNone(store.get_entry(G2, w))
        self.assertFalse(store.clear_entry(G2, w, BOSS, "boss", "not mine to clear"))
        self.assertIsNone(store.get_entry(G, w)["cleared_at"])

    def test_evidence_bytes_are_counted_per_guild(self):
        w = store.add_entry(G, U, "warn", "x", MOD)
        _, data = _fake_evidence(w, G, U)
        self.assertEqual(store.guild_evidence_bytes(G), len(data))
        self.assertEqual(store.guild_evidence_bytes(G2), 0)

    # ── clearing is soft ─────────────────────────────────────────────────────

    def test_clear_keeps_the_row_and_stamps_who_did_it(self):
        w = store.add_entry(G, U, "warn", "spam in #general", MOD)
        self.assertTrue(store.clear_entry(G, w, BOSS, "boss#2", "was a misread"))

        e = store.get_entry(G, w)
        self.assertIsNotNone(e, "clearing must not delete the row")
        self.assertEqual(e["cleared_by"], BOSS)
        self.assertEqual(e["cleared_reason"], "was a misread")
        self.assertEqual(e["reason"], "spam in #general", "original must survive")

        self.assertNotIn(w, [r["id"] for r in store.list_entries(G, U)])
        self.assertIn(w, [r["id"] for r in store.list_entries(G, U, include_cleared=True)])

    def test_clearing_twice_is_a_no_op(self):
        w = store.add_entry(G, U, "warn", "x", MOD)
        self.assertTrue(store.clear_entry(G, w, BOSS, "boss", "first"))
        self.assertFalse(store.clear_entry(G, w, MOD, "mod", "second"))
        self.assertEqual(store.get_entry(G, w)["cleared_reason"], "first")

    def test_clear_does_not_touch_evidence(self):
        """A cleared warning can still need its evidence — that's the point of a
        record that keeps cleared entries rather than deleting them."""
        w = store.add_entry(G, U, "warn", "x", MOD)
        path, _ = _fake_evidence(w, G, U)
        store.clear_entry(G, w, BOSS, "boss", "amnesty")
        self.assertEqual(len(store.evidence_for(w)), 1)
        self.assertTrue(os.path.exists(path))

    def test_bulk_clear_only_touches_standing_entries(self):
        store.add_entry(G, U, "warn", "a", MOD)
        store.add_entry(G, U, "warn", "b", MOD)
        store.add_entry(G, U, "note", "c", MOD)
        self.assertEqual(store.clear_all(G, U, BOSS, "boss", "amnesty"), 3)
        self.assertEqual(store.clear_all(G, U, BOSS, "boss", "again"), 0)
        self.assertEqual(store.counts(G, U)["warns"], 0)
        self.assertEqual(store.counts(G, U)["cleared"], 3)

    # ── erasure is hard, and reaches disk ────────────────────────────────────

    def test_forget_deletes_rows_and_files(self):
        w = store.add_entry(G, U, "warn", "x", MOD)
        path, _ = _fake_evidence(w, G, U)
        res = store.forget_user(U, G)
        self.assertEqual((res["entries"], res["evidence"], res["files"]), (1, 1, 1))
        self.assertEqual(store.list_entries(G, U, include_cleared=True), [])
        self.assertFalse(os.path.exists(path), "evidence must not survive a purge")

    def test_forget_scoped_to_one_guild_leaves_others(self):
        store.add_entry(G, U, "warn", "here", MOD)
        store.add_entry(G2, U, "warn", "elsewhere", MOD)
        store.forget_user(U, G)
        self.assertEqual(store.list_entries(G, U, include_cleared=True), [])
        self.assertEqual(len(store.list_entries(G2, U, include_cleared=True)), 1)

    def test_forget_everywhere_reaches_every_guild(self):
        """The underage / right-to-be-forgotten path. A per-guild sweep would
        leave copies in servers nobody thought to check."""
        store.add_entry(G, U, "warn", "here", MOD)
        store.add_entry(G2, U, "warn", "elsewhere", MOD)
        res = store.forget_user(U)
        self.assertEqual(res["entries"], 2)
        self.assertEqual(store.list_entries(G, U, include_cleared=True), [])
        self.assertEqual(store.list_entries(G2, U, include_cleared=True), [])

    def test_forget_everywhere_sweeps_orphaned_files(self):
        """A file whose row already vanished still has to go."""
        w = store.add_entry(G, U, "warn", "x", MOD)
        path, _ = _fake_evidence(w, G, U)
        with sqlite3.connect(store.DB_PATH) as c:
            c.execute("DELETE FROM evidence")          # row gone, file orphaned
        res = store.forget_user(U)
        self.assertFalse(os.path.exists(path))
        self.assertEqual(res["files"], 1)

    def test_forget_guild_clears_a_whole_server(self):
        w = store.add_entry(G, U, "warn", "x", MOD)
        path, _ = _fake_evidence(w, G, U)
        store.add_entry(G2, U, "warn", "elsewhere", MOD)
        res = store.forget_guild(G)
        self.assertEqual((res["entries"], res["files"]), (1, 1))
        self.assertFalse(os.path.exists(path))
        self.assertEqual(len(store.list_entries(G2, U, include_cleared=True)), 1)


if __name__ == "__main__":
    unittest.main()
