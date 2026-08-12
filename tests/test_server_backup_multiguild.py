"""Per-guild scoping of server_backup + rejoin_roles.

The migration half needs the cog (which imports discord) so it only runs on the
VPS; the rejoin_roles half is pure sqlite and runs anywhere.

What these guard against, concretely: the pre-migration schema keyed `roster` by
uid ALONE, so one person in two servers had ONE row — the second server's
snapshot overwrote the first's role list, and a rejoin restore then handed the
wrong server's roles to the safety filter.
"""
import os
import sys
import json
import sqlite3
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import rejoin_roles  # noqa: E402

HOME = "1215140346800119868"
OTHER = "1533863919561605160"

OLD_SCHEMA = [
    """CREATE TABLE roster (uid TEXT PRIMARY KEY, username TEXT, display_name TEXT,
           roles TEXT, joined_at REAL, first_seen REAL, last_seen REAL)""",
    """CREATE TABLE member_events (ts REAL, uid TEXT, username TEXT, display_name TEXT,
           roles TEXT, kind TEXT, by_uid TEXT)""",
    "CREATE TABLE snapshots (ts REAL, member_count INTEGER)",
    "CREATE TABLE structure (ts REAL PRIMARY KEY, guild_name TEXT, roles TEXT, channels TEXT)",
]


def _connect(path):
    """Explicit open/close — `with sqlite3.connect()` commits but does NOT close,
    and an open handle makes the temp file undeletable on Windows."""
    return sqlite3.connect(path)


def _old_db(path):
    c = _connect(path)
    try:
        for stmt in OLD_SCHEMA:
            c.execute(stmt)
        c.execute("INSERT INTO roster VALUES ('7','ana','ana',?,1.0,1.0,2.0)", (json.dumps([10, 11]),))
        c.execute("INSERT INTO member_events VALUES (5.0,'7','ana','ana',?,'leave',NULL)",
                  (json.dumps([10, 11]),))
        c.execute("INSERT INTO snapshots VALUES (1.0, 1)")
        c.execute("INSERT INTO structure VALUES (1.0,'home','[]','[]')")
        c.commit()
    finally:
        c.close()


def _cols(path, table):
    c = _connect(path)
    try:
        return {r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}
    finally:
        c.close()


def _rm(path):
    try:
        os.remove(path)
    except OSError:
        pass  # rejoin_roles leaves its read connections to the GC (Windows only)


class MigrationTest(unittest.TestCase):
    """VPS-only — cogs.server_backup imports discord."""

    @classmethod
    def setUpClass(cls):
        try:
            import discord  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("discord.py not installed (run this on the VPS)")

    def _migrated(self):
        from cogs import server_backup as sb
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(path)
        _old_db(path)
        self.addCleanup(_rm, path)
        # pin both the DB and the home id so the expected backfill value doesn't
        # depend on whether a .env happens to be loaded
        orig_db, sb.DB_PATH = sb.DB_PATH, path
        orig_gid, sb.GUILD_ID = sb.GUILD_ID, int(HOME)
        self.addCleanup(lambda: setattr(sb, "DB_PATH", orig_db))
        self.addCleanup(lambda: setattr(sb, "GUILD_ID", orig_gid))
        cog = sb.ServerBackup.__new__(sb.ServerBackup)   # no bot / no gateway
        c = _connect(path)
        try:
            cog._migrate(c)
            c.commit()
        finally:
            c.close()
        return sb, path, cog

    def test_columns_added_and_rows_tagged_home(self):
        _, path, _ = self._migrated()
        for table in ("roster", "member_events", "snapshots", "structure"):
            self.assertIn("guild_id", _cols(path, table), table)
        c = _connect(path)
        try:
            self.assertEqual(c.execute("SELECT guild_id FROM roster").fetchone()[0], HOME)
            self.assertEqual(c.execute("SELECT guild_id FROM member_events").fetchone()[0], HOME)
            self.assertEqual(c.execute("SELECT guild_id FROM structure").fetchone()[0], HOME)
        finally:
            c.close()

    def test_no_data_lost(self):
        _, path, _ = self._migrated()
        c = _connect(path)
        try:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM roster").fetchone()[0], 1)
            self.assertEqual(c.execute("SELECT roles FROM roster").fetchone()[0], json.dumps([10, 11]))
            self.assertEqual(c.execute("SELECT COUNT(*) FROM structure").fetchone()[0], 1)
        finally:
            c.close()

    def test_idempotent(self):
        _, path, cog = self._migrated()
        c = _connect(path)
        try:
            cog._migrate(c)          # a restart must not re-run it
            cog._migrate(c)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM roster").fetchone()[0], 1)
        finally:
            c.close()

    def test_same_uid_two_guilds_coexist(self):
        """The whole point of the composite key — one person in two servers."""
        _, path, _ = self._migrated()
        c = _connect(path)
        try:
            c.execute("INSERT INTO roster(guild_id, uid, username, roles) VALUES (?,?,?,?)",
                      (OTHER, "7", "ana", json.dumps([99])))
            rows = c.execute("SELECT guild_id, roles FROM roster WHERE uid='7' ORDER BY guild_id").fetchall()
        finally:
            c.close()
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0][1], rows[1][1])


class RejoinScopeTest(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        c = _connect(self.path)
        try:
            c.execute("""CREATE TABLE roster (guild_id TEXT, uid TEXT, roles TEXT,
                             PRIMARY KEY (guild_id, uid))""")
            c.execute("""CREATE TABLE member_events (guild_id TEXT, ts REAL, uid TEXT,
                             roles TEXT, kind TEXT)""")
            c.execute("INSERT INTO member_events VALUES (?,9.0,'7',?,'leave')", (HOME, json.dumps([10, 11])))
            c.execute("INSERT INTO member_events VALUES (?,9.0,'7',?,'leave')", (OTHER, json.dumps([88])))
            c.execute("INSERT INTO roster VALUES (?,'8',?)", (HOME, json.dumps([21])))
            c.execute("INSERT INTO roster VALUES (?,'8',?)", (OTHER, json.dumps([99])))
            c.commit()
        finally:
            c.close()
        self._orig = rejoin_roles.DB_PATH
        rejoin_roles.DB_PATH = self.path

    def tearDown(self):
        rejoin_roles.DB_PATH = self._orig
        _rm(self.path)

    def test_events_scoped_to_guild(self):
        self.assertEqual(rejoin_roles.last_known_role_ids(7, HOME), [10, 11])
        self.assertEqual(rejoin_roles.last_known_role_ids(7, OTHER), [88])

    def test_roster_fallback_scoped_to_guild(self):
        self.assertEqual(rejoin_roles.last_known_role_ids(8, HOME), [21])
        self.assertEqual(rejoin_roles.last_known_role_ids(8, OTHER), [99])

    def test_unknown_guild_returns_nothing(self):
        self.assertEqual(rejoin_roles.last_known_role_ids(7, "999"), [])

    def test_unscoped_call_still_reads(self):
        """Back-compat: a caller that passes no guild must not start erroring."""
        self.assertIn(rejoin_roles.last_known_role_ids(7), ([10, 11], [88]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
