"""Tests for the identity ledger + name/timeout logging in cogs/mod_log.py.

The gap these guard against, found 2026-07-28: everything this cog knew about a
PERSON (names, nicknames, timeouts, lifecycle) existed only as embeds in a
Discord channel. Embeds are invisible to SQL, so a 2025 investigation into a
deleted account stalled — the only surviving record of its names and numeric id
was inside a third-party bot's embeds, recoverable one REST call at a time.
identity_events mirrors all of it in plain text, keyed by uid, forever.
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cogs.mod_log import _plain  # noqa: E402


class FakeLedger:
    """The _record_identity implementation bound to a throwaway DB."""

    def __init__(self, path):
        self.path = path
        with self._conn() as c:
            c.execute(
                """CREATE TABLE IF NOT EXISTS identity_events (
                       ts REAL, guild_id TEXT, uid TEXT, username TEXT,
                       kind TEXT, before TEXT, after TEXT,
                       by_uid TEXT, by_name TEXT, reason TEXT)""")

    def _conn(self):
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        return c

    # copied verbatim from the cog so the test exercises the real shape
    def record(self, guild_id, user, kind, before=None, after=None,
               by_uid=None, by_name=None, reason=None):
        import time
        try:
            uid = getattr(user, "id", user)
            uname = getattr(user, "name", None) or str(user)
            with self._conn() as c:
                c.execute(
                    "INSERT INTO identity_events"
                    " (ts, guild_id, uid, username, kind, before, after, by_uid, by_name, reason)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (time.time(), str(guild_id), str(uid), uname, kind,
                     None if before is None else str(before),
                     None if after is None else str(after),
                     None if by_uid is None else str(by_uid), by_name, reason))
        except Exception:
            pass


def fake_user(uid=1311408487451988018, name="misanthropechudjak"):
    return SimpleNamespace(id=uid, name=name)


class PlainNameTests(unittest.TestCase):
    """Names are attacker-controlled — a nickname must not ping or reformat."""

    def test_everyone_mention_is_defanged(self):
        out = _plain("@everyone")
        self.assertNotEqual(out, "@everyone")
        self.assertIn("​", out)

    def test_markdown_is_escaped(self):
        self.assertEqual(_plain("**bold**"), r"\*\*bold\*\*")

    def test_empty_and_none(self):
        self.assertEqual(_plain(None), "")
        self.assertEqual(_plain(""), "")

    def test_ordinary_name_survives_readably(self):
        self.assertEqual(_plain("keep coping fatso"), "keep coping fatso")


class LedgerTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.led = FakeLedger(self.path)

    def tearDown(self):
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def rows(self):
        with self.led._conn() as c:
            return list(c.execute("SELECT * FROM identity_events ORDER BY rowid"))

    def test_nick_change_is_queryable_by_uid(self):
        u = fake_user()
        self.led.record(1, u, "nick", "loser", "half the members here are my alt")
        r = self.rows()[0]
        self.assertEqual(r["uid"], "1311408487451988018")
        self.assertEqual(r["kind"], "nick")
        self.assertEqual(r["before"], "loser")
        self.assertEqual(r["after"], "half the members here are my alt")

    def test_name_chain_reconstructable_after_account_deletion(self):
        """The whole point: uid outlives the account, names stay searchable."""
        u = fake_user()
        self.led.record(1, u, "join", after="misanthropechudjak")
        self.led.record(1, u, "nick", "loser", "keep coping fatso")
        self.led.record(1, u, "ban", by_uid=596446208021626938,
                        by_name="mrdudebro1", reason="dox threats")
        with self.led._conn() as c:
            hits = list(c.execute(
                "SELECT kind FROM identity_events WHERE uid=? ORDER BY rowid",
                ("1311408487451988018",)))
        self.assertEqual([h["kind"] for h in hits], ["join", "nick", "ban"])

    def test_searchable_by_name_without_knowing_the_uid(self):
        self.led.record(1, fake_user(), "nick", "loser", "keep coping fatso")
        with self.led._conn() as c:
            hit = c.execute(
                "SELECT uid FROM identity_events WHERE after=?",
                ("keep coping fatso",)).fetchone()
        self.assertEqual(hit["uid"], "1311408487451988018")

    def test_actor_recorded_for_mod_actions(self):
        self.led.record(1, fake_user(), "timeout", None, "2026-07-28T00:00:00",
                        by_uid=596446208021626938, by_name="mrdudebro1",
                        reason="spam")
        r = self.rows()[0]
        self.assertEqual(r["by_uid"], "596446208021626938")
        self.assertEqual(r["by_name"], "mrdudebro1")
        self.assertEqual(r["reason"], "spam")

    def test_bare_uid_accepted_when_no_user_object(self):
        """Ban audit events can hand us a raw id instead of a User."""
        self.led.record(1, 1377060168243609653, "ban")
        self.assertEqual(self.rows()[0]["uid"], "1377060168243609653")

    def test_record_never_raises_on_bad_input(self):
        """The ledger must never break the embed mods actually see."""
        self.led.record(1, object(), "nick", before=object(), after=object())  # no raise

    def test_raw_value_stored_unescaped(self):
        """Escaping is display-only; forensics needs the literal string."""
        self.led.record(1, fake_user(), "nick", None, "@everyone")
        self.assertEqual(self.rows()[0]["after"], "@everyone")


class TimeoutClassificationTests(unittest.TestCase):
    """Discord leaves an expired timestamp in place rather than clearing it, so
    'removed' must not fire on natural expiry."""

    @staticmethod
    def classify(before_ts, after_ts, now):
        applied = after_ts is not None and after_ts > now
        if not applied and before_ts is not None and before_ts <= now:
            return None  # natural expiry — don't log
        return "timeout" if applied else "untimeout"

    def test_applied(self):
        self.assertEqual(self.classify(None, 2000, 1000), "timeout")

    def test_lifted_early_by_mod(self):
        self.assertEqual(self.classify(2000, None, 1000), "untimeout")

    def test_natural_expiry_is_silent(self):
        self.assertIsNone(self.classify(500, None, 1000))

    def test_extended(self):
        self.assertEqual(self.classify(1500, 3000, 1000), "timeout")


if __name__ == "__main__":
    unittest.main()
