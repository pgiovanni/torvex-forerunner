"""Guild-structure ledger (2026-08-29): role / channel / overwrite / expression /
AutoMod-rule / member-role events mirrored into guild_events as plain text,
swept under the guild's text window, purged with the guild.

    PYTHONIOENCODING=utf-8 python tests/test_mod_log_ledger.py
"""
import json
import os
import sys
import time
import shutil
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

import discord  # noqa: E402
import cogs.mod_log as ml  # noqa: E402

NOW = time.time()
H, D = 3600, 86400
OPERATOR, FREE = "111", "444"


class _Bot:
    user = type("U", (), {"id": 1})()


class _Diff:
    """Stands in for discord.AuditLogDiff: iterates as (attr, value)."""
    def __init__(self, **kw):
        self._kw = kw

    def __iter__(self):
        return iter(self._kw.items())


class _Changes:
    def __init__(self, before, after):
        self.before, self.after = before, after


class _User:
    def __init__(self, uid, name):
        self.id, self.name = uid, name

    def __str__(self):
        return self.name


class _Entry:
    def __init__(self, user_id, user, reason, changes):
        self.user_id, self.user, self.reason, self.changes = user_id, user, reason, changes


class Helpers(unittest.TestCase):
    def test_jsonable_scalars_and_objects(self):
        self.assertIsNone(ml._jsonable(None))
        self.assertEqual(ml._jsonable(3), 3)
        self.assertEqual(ml._jsonable("x"), "x")
        self.assertEqual(ml._jsonable(_User(5, "role")), "role (5)")
        self.assertEqual(ml._jsonable([_User(5, "a"), 2]), ["a (5)", 2])

    def test_jsonable_permissions_lists_granted_flags(self):
        p = discord.Permissions(kick_members=True, ban_members=True)
        self.assertEqual(ml._jsonable(p), ["ban_members", "kick_members"])

    def test_changes_json_walks_before_after(self):
        ch = _Changes(_Diff(name="old", hoist=False), _Diff(name="new", hoist=True, colour="#fff"))
        out = json.loads(ml.audit_changes_json(ch))
        by_key = {d["key"]: d for d in out}
        self.assertEqual(by_key["name"], {"key": "name", "before": "old", "after": "new"})
        self.assertEqual(by_key["hoist"]["after"], True)
        self.assertEqual(by_key["colour"], {"key": "colour", "before": None, "after": "#fff"})

    def test_changes_json_none_and_empty(self):
        self.assertIsNone(ml.audit_changes_json(None))
        self.assertIsNone(ml.audit_changes_json(_Changes(_Diff(), _Diff())))


class Ledger(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="modlog_ledger_")
        self._db, self._media = ml.DB_PATH, ml.MEDIA_DIR
        ml.DB_PATH = os.path.join(self.tmp, "messages.db")
        ml.MEDIA_DIR = os.path.join(self.tmp, "media_cache")
        ml.ARCHIVE_GUILDS = {OPERATOR}
        ml.OPERATOR_GUILDS = {OPERATOR}
        self.cog = ml.ModLog(_Bot())

    def tearDown(self):
        ml.DB_PATH, ml.MEDIA_DIR = self._db, self._media
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _rows(self, gid):
        with self.cog._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM guild_events WHERE guild_id=? ORDER BY ts", (gid,))]

    def test_record_from_audit_entry(self):
        entry = _Entry(77, _User(77, "paul"), "cleanup",
                       _Changes(_Diff(name="old"), _Diff(name="new")))
        self.cog._record_guild_event(FREE, "role", "update", "role", 123, "new",
                                     entry=entry, lines=["@new", "name: `old` → `new`"])
        rows = self._rows(FREE)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r["kind"], r["action"], r["target_type"], r["target_id"], r["target_name"]),
                         ("role", "update", "role", "123", "new"))
        self.assertEqual((r["by_uid"], r["by_name"], r["reason"]), ("77", "paul", "cleanup"))
        self.assertEqual(r["summary"], "@new\nname: `old` → `new`")
        self.assertEqual(json.loads(r["changes"])[0]["after"], "new")

    def test_record_member_roles_with_dict_changes(self):
        self.cog._record_guild_event(FREE, "member_roles", "update", "member", 9, "kid",
                                     lines=["+ Verified"], by_uid=9,
                                     by_name="self-assign (reaction role)",
                                     changes={"added": ["Verified (5)"], "removed": []})
        r = self._rows(FREE)[0]
        self.assertEqual(json.loads(r["changes"]), {"added": ["Verified (5)"], "removed": []})
        self.assertEqual(r["by_uid"], "9")

    def test_record_never_raises(self):
        class Boom:
            def __iter__(self):
                raise RuntimeError("bad diff")
        entry = _Entry(1, None, None, _Changes(Boom(), Boom()))
        self.cog._record_guild_event(FREE, "role", "create", "role", 1, "x", entry=entry)
        self.assertEqual(len(self._rows(FREE)), 1)  # row still written, changes=None
        self.assertIsNone(self._rows(FREE)[0]["changes"])

    def test_sweep_honours_text_window(self):
        with self.cog._conn() as c:
            for gid in (OPERATOR, FREE):
                for age in (1 * H, 2 * D):
                    c.execute("INSERT INTO guild_events (ts, guild_id, kind, action) VALUES (?,?,?,?)",
                              (NOW - age, gid, "channel", "create"))
        swept = self.cog._sweep_rows(NOW)
        self.assertEqual(len(self._rows(OPERATOR)), 2)          # operator: never swept
        free = self._rows(FREE)
        self.assertEqual(len(free), 1)                           # free: 24h window
        self.assertGreater(free[0]["ts"], NOW - ml.RECENT_HOURS * H)
        self.assertEqual(swept.get(FREE), 1)

    def test_purge_guild_drops_ledger(self):
        self.cog._record_guild_event(FREE, "channel", "delete", "channel", 5, "gone")
        self.cog._record_guild_event(OPERATOR, "channel", "delete", "channel", 6, "stays")
        counts = self.cog._purge_guild(FREE)
        self.assertEqual(counts["events"], 1)
        self.assertEqual(self._rows(FREE), [])
        self.assertEqual(len(self._rows(OPERATOR)), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
