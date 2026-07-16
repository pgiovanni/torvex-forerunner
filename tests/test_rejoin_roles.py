"""Safety tests for returning-member role restore — the filter must never hand
back staff/security/managed/dangerous roles."""
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import rejoin_roles  # noqa: E402

ADMIN = 1 << 3
BAN = 1 << 2
MANAGE_MSGS = 1 << 13
MODERATE = 1 << 40


class RestorablePredicate(unittest.TestCase):
    deny = {999}

    def ok(self, perms, rid=1, managed=False, default=False, above=False):
        return rejoin_roles.is_restorable(perms, managed, default, rid, self.deny, above)

    def test_permissionless_cosmetic_restorable(self):
        self.assertTrue(self.ok(0))                 # a color/level/age band role

    def test_admin_never(self):
        self.assertFalse(self.ok(ADMIN))

    def test_any_dangerous_perm_excluded(self):
        for p in (BAN, MANAGE_MSGS, MODERATE, ADMIN | 1):
            self.assertFalse(self.ok(p))

    def test_managed_excluded(self):
        self.assertFalse(self.ok(0, managed=True))   # bot/booster/integration

    def test_default_role_excluded(self):
        self.assertFalse(self.ok(0, default=True))   # @everyone

    def test_above_bot_excluded(self):
        self.assertFalse(self.ok(0, above=True))

    def test_deny_set_excluded_even_if_permissionless(self):
        self.assertFalse(self.ok(0, rid=999))        # e.g. 18+ Staff / quarantine


class SnapshotRead(unittest.TestCase):
    def setUp(self):
        self.fd, self.path = tempfile.mkstemp(suffix=".db")
        self._orig = rejoin_roles.DB_PATH
        rejoin_roles.DB_PATH = self.path
        with sqlite3.connect(self.path) as c:
            c.execute("CREATE TABLE member_events (ts REAL, uid TEXT, username TEXT, "
                      "display_name TEXT, roles TEXT, kind TEXT, by_uid TEXT)")
            c.execute("CREATE TABLE roster (uid TEXT PRIMARY KEY, username TEXT, "
                      "display_name TEXT, roles TEXT, joined_at REAL, first_seen REAL, last_seen REAL)")

    def tearDown(self):
        rejoin_roles.DB_PATH = self._orig
        os.close(self.fd)
        os.unlink(self.path)

    def _ev(self, uid, roles, kind, ts):
        with sqlite3.connect(self.path) as c:
            c.execute("INSERT INTO member_events VALUES (?,?,?,?,?,?,?)",
                      (ts, str(uid), "u", "u", roles, kind, None))

    def test_latest_leave_wins(self):
        self._ev(1, "[10, 20]", "leave", 100)
        self._ev(1, "[30, 40]", "leave", 200)   # newer
        self.assertEqual(rejoin_roles.last_known_role_ids(1), [30, 40])

    def test_kick_counts(self):
        self._ev(2, "[7]", "kick", 50)
        self.assertEqual(rejoin_roles.last_known_role_ids(2), [7])

    def test_join_events_ignored(self):
        self._ev(3, "[1]", "join", 10)
        self.assertEqual(rejoin_roles.last_known_role_ids(3), [])

    def test_roster_fallback(self):
        with sqlite3.connect(self.path) as c:
            c.execute("INSERT INTO roster (uid, roles) VALUES (?,?)", ("4", "[5, 6]"))
        self.assertEqual(rejoin_roles.last_known_role_ids(4), [5, 6])

    def test_no_record(self):
        self.assertEqual(rejoin_roles.last_known_role_ids(999), [])


if __name__ == "__main__":
    unittest.main()
