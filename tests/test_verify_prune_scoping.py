"""verify_prune clock scoping — the join-time fallback is HOME-GUILD ONLY.

This is the highest-blast-radius line in the multi-server work. `_held_since`
answers "when did this member's verify clock start"; `_candidates` removes
anyone whose clock ran out. The fallback chain is:

    quarantine record  ->  verify-link issue time  ->  join time

That last step is fine at home, where the quarantine role is *ours* and every
holder was put there by the gate. Pointed at another server's long-standing
"unverified" role it is a disaster: those members have no quarantine record and
no verification record, so join time becomes the clock, every one of them is
instantly weeks overdue, and the first sweep mass-kicks the entire holding pen.

So the load-bearing assertion here is a REFUSAL: for any guild that is not the
home guild, a member with no real hold record must return None — no clock, and
therefore never a prune candidate. Everything else in this file exists to prove
that narrowing didn't break the home-guild behaviour it was carved out of.
"""
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Point every store at scratch files BEFORE the modules import them.
_TMP = tempfile.mkdtemp(prefix="prunescope")
os.environ["TORVEX_SECURITY_DB"] = os.path.join(_TMP, "security_config.db")
os.environ.setdefault("ALTGUARD_GUILD_ID", "111")

import discord  # noqa: E402
import quarantine_store as qstore  # noqa: E402

qstore._PATH = os.path.join(_TMP, "altguard_quarantine.db")
qstore.init()

from utils import security_config as sc  # noqa: E402
from cogs import verify_prune as vp  # noqa: E402

HOME = vp.GUILD_ID          # 111, from ALTGUARD_GUILD_ID above
REMOTE = 222222222222222222
DAY = 86400.0


class FakeRole:
    def __init__(self, rid, name="r", position=1, managed=False, members=()):
        self.id = rid
        self.name = name
        self.position = position
        self.managed = managed
        self.mention = f"@{name}"
        self.members = list(members)

    def is_default(self):
        return self.position == 0


class FakePerms:
    def __init__(self, administrator=False, manage_guild=False):
        self.administrator = administrator
        self.manage_guild = manage_guild


class FakeGuild:
    def __init__(self, gid, roles=(), owner_id=1):
        self.id = gid
        self.name = "Test"
        self.owner_id = owner_id
        self._roles = {r.id: r for r in roles}

    def get_role(self, rid):
        return self._roles.get(rid)


class FakeMember:
    """Only the attributes _held_since / _exempt actually touch."""

    def __init__(self, uid, guild, joined_days_ago=None, roles=(),
                 bot=False, perms=None):
        self.id = uid
        self.guild = guild
        self.bot = bot
        self.roles = list(roles)
        self.guild_permissions = perms or FakePerms()
        self.mention = f"<@{uid}>"
        self.joined_at = None
        if joined_days_ago is not None:
            self.joined_at = discord.utils.utcnow() - __import__("datetime").timedelta(
                days=joined_days_ago)


def _cog():
    return vp.VerifyPrune.__new__(vp.VerifyPrune)   # no bot / no loops needed


class HeldSinceScoping(unittest.TestCase):
    """The fallback chain, and where the last link is allowed to fire."""

    def setUp(self):
        self.cog = _cog()
        # A clean store each time: these tests are entirely about which record
        # exists, so a leftover row from another test would hide the bug.
        with qstore._conn() as c:
            c.execute("DELETE FROM quarantined")
            c.execute("DELETE FROM verifications")

    # ---- the refusal this file exists for ----------------------------------
    def test_remote_guild_with_no_record_has_no_clock(self):
        """The mass-kick guard. A member of someone else's server sitting in
        their own 'unverified' role for a year must return None, not a clock
        that reads as a year overdue."""
        m = FakeMember(5001, FakeGuild(REMOTE), joined_days_ago=365)
        self.assertIsNone(self.cog._held_since(m))

    def test_remote_guild_join_time_is_ignored_however_old(self):
        for age in (0, 1, 30, 3650):
            with self.subTest(days=age):
                m = FakeMember(5002, FakeGuild(REMOTE), joined_days_ago=age)
                self.assertIsNone(self.cog._held_since(m))

    # ---- home-guild behaviour must be exactly what it was ------------------
    def test_home_guild_still_falls_back_to_join_time(self):
        m = FakeMember(5003, FakeGuild(HOME), joined_days_ago=10)
        ts = self.cog._held_since(m)
        self.assertIsNotNone(ts, "home guild lost its join-time fallback")
        self.assertAlmostEqual(ts, m.joined_at.timestamp(), places=3)

    def test_home_guild_id_as_string_still_matches(self):
        """FakeGuild mirrors discord.py, which always hands us an int id — this
        guards the comparison rather than the data source."""
        m = FakeMember(5004, FakeGuild(int(HOME)), joined_days_ago=10)
        self.assertIsNotNone(self.cog._held_since(m))

    # ---- the earlier links in the chain work everywhere --------------------
    def test_quarantine_record_wins_in_the_home_guild(self):
        qstore.save(5005, HOME, [], "test hold")
        m = FakeMember(5005, FakeGuild(HOME), joined_days_ago=365)
        ts = self.cog._held_since(m)
        self.assertAlmostEqual(ts, time.time(), delta=30,
                               msg="should be the hold time, not the year-old join")

    def test_quarantine_record_wins_in_a_remote_guild(self):
        """A real hold record IS a clock anywhere — the narrowing removed only
        the join-time guess, not genuine evidence."""
        qstore.save(5006, REMOTE, [], "test hold")
        m = FakeMember(5006, FakeGuild(REMOTE), joined_days_ago=365)
        ts = self.cog._held_since(m)
        self.assertIsNotNone(ts)
        self.assertAlmostEqual(ts, time.time(), delta=30)

    def test_issued_at_fallback_works_in_a_remote_guild(self):
        """They were sent a verify link, so the clock legitimately started."""
        qstore.record_issue(5007, REMOTE, True)
        m = FakeMember(5007, FakeGuild(REMOTE), joined_days_ago=365)
        ts = self.cog._held_since(m)
        self.assertIsNotNone(ts)
        self.assertAlmostEqual(ts, time.time(), delta=30)

    def test_issued_at_beats_join_time_at_home(self):
        qstore.record_issue(5008, HOME, True)
        m = FakeMember(5008, FakeGuild(HOME), joined_days_ago=365)
        self.assertAlmostEqual(self.cog._held_since(m), time.time(), delta=30)

    # ---- shapes that must not raise ---------------------------------------
    def test_member_with_no_joined_at_does_not_crash(self):
        for gid in (HOME, REMOTE):
            with self.subTest(guild=gid):
                m = FakeMember(5009, FakeGuild(gid), joined_days_ago=None)
                self.assertIsNone(self.cog._held_since(m))


class CandidateSelection(unittest.TestCase):
    """_candidates() turns clocks into removals. A member with no clock must
    never reach the list, whatever the window is set to."""

    def setUp(self):
        self.cog = _cog()
        sc._cache.clear()
        with qstore._conn() as c:
            c.execute("DELETE FROM quarantined")
            c.execute("DELETE FROM verifications")

    def _settings(self, gid, **cfg):
        sc.set_config(gid, prune_enabled=1, altguard_enabled=1,
                      quarantine_role_id=500, **cfg)
        sc._cache.clear()
        return vp.Settings(FakeGuild(gid, [FakeRole(500, "Quarantined", position=9)]))

    def _run(self, gid, members, **cfg):
        qrole = FakeRole(500, "Quarantined", position=9, members=members)
        guild = FakeGuild(gid, [qrole])
        for m in members:
            m.guild = guild
        st = self._settings(gid, **cfg)
        return self.cog._candidates(guild, st)

    def test_remote_holding_pen_yields_nobody(self):
        """The scenario the narrowing prevents: a server's existing unverified
        role, full of members who joined long ago and have no gate record."""
        pen = [FakeMember(6000 + i, None, joined_days_ago=400) for i in range(5)]
        self.assertEqual(self._run(REMOTE, pen), [],
                         "a remote holding pen must never become a kick list")

    def test_home_overdue_member_is_still_selected(self):
        """Proof the guard didn't disable the feature where it belongs."""
        m = FakeMember(6100, None, joined_days_ago=400)
        got = self._run(HOME, [m])
        self.assertEqual([x.id for x in got], [6100])

    def test_remote_member_with_a_real_hold_is_selected(self):
        """Once there IS a record, remote servers prune normally."""
        qstore.save(6200, REMOTE, [], "held")
        with qstore._conn() as c:      # backdate the hold past the window
            c.execute("UPDATE quarantined SET ts=? WHERE uid=?",
                      (time.time() - 10 * DAY, "6200"))
        m = FakeMember(6200, None, joined_days_ago=400)
        got = self._run(REMOTE, [m])
        self.assertEqual([x.id for x in got], [6200])

    def test_recently_held_member_is_spared(self):
        """The original bug this clock design fixed — a long-standing member
        quarantined today gets the full window, not an instant kick."""
        qstore.save(6300, HOME, [], "held just now")
        m = FakeMember(6300, None, joined_days_ago=400)
        self.assertEqual(self._run(HOME, [m]), [])

    def test_passed_verification_is_never_pruned(self):
        m = FakeMember(6400, None, joined_days_ago=400)
        qstore.record_issue(6400, HOME, True)
        with qstore._conn() as c:
            c.execute("UPDATE verifications SET issued_at=?, status='passed' WHERE uid=?",
                      (time.time() - 10 * DAY, "6400"))
        self.assertEqual(self._run(HOME, [m]), [])

    def test_bots_and_staff_are_exempt(self):
        bot = FakeMember(6500, None, joined_days_ago=400, bot=True)
        admin = FakeMember(6501, None, joined_days_ago=400,
                           perms=FakePerms(administrator=True))
        modish = FakeMember(6502, None, joined_days_ago=400,
                            perms=FakePerms(manage_guild=True))
        self.assertEqual(self._run(HOME, [bot, admin, modish]), [])

    def test_guild_owner_is_exempt(self):
        owner = FakeMember(7, None, joined_days_ago=400)
        qrole = FakeRole(500, "Quarantined", position=9, members=[owner])
        guild = FakeGuild(HOME, [qrole], owner_id=7)
        owner.guild = guild
        self.assertEqual(self.cog._candidates(guild, self._settings(HOME)), [])

    def test_no_quarantine_role_yields_nobody(self):
        st = self._settings(HOME)
        self.assertEqual(self.cog._candidates(FakeGuild(HOME, []), st), [])


if __name__ == "__main__":
    unittest.main(verbosity=1)
