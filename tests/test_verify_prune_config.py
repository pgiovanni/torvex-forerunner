"""Per-guild prune policy (cogs/verify_prune.Settings) + the shared quarantine
helpers (utils/quarantine).

What actually matters here is the failure direction. Every one of these settings
decides whether a real member gets removed, so a bad value must fall back to
something conservative rather than to zero: a prune window of 0 hours would kick
everyone currently held, and an unparseable one must not become that.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Point every store at a scratch file BEFORE the modules import them.
_TMP = tempfile.mkdtemp(prefix="prunecfg")
os.environ["TORVEX_SECURITY_DB"] = os.path.join(_TMP, "security_config.db")
os.environ.setdefault("ALTGUARD_GUILD_ID", "111")

import discord  # noqa: E402
import quarantine_store as qstore  # noqa: E402
from utils import security_config as sc  # noqa: E402
from utils import quarantine as qt  # noqa: E402


class FakeRole:
    def __init__(self, rid, name="r", position=1, managed=False, **perms):
        self.id = rid
        self.name = name
        self.position = position
        self.managed = managed
        self.mention = f"@{name}"
        self.permissions = discord.Permissions(**perms)

    def is_default(self):
        return self.position == 0

    def __le__(self, other):
        return self.position <= other.position

    def __lt__(self, other):
        return self.position < other.position

    def __ge__(self, other):
        return self.position >= other.position

    def __gt__(self, other):
        return self.position > other.position

    def __hash__(self):
        return hash(self.id)

    def __eq__(self, other):
        return isinstance(other, FakeRole) and other.id == self.id


class FakeGuild:
    def __init__(self, gid, roles=(), me=None, name="Test"):
        self.id = gid
        self.name = name
        self._roles = {r.id: r for r in roles}
        self.me = me

    def get_role(self, rid):
        return self._roles.get(rid)


class FakeMember:
    def __init__(self, uid, guild, roles=()):
        self.id = uid
        self.guild = guild
        self.roles = list(roles)


EVERYONE = FakeRole(1, "@everyone", position=0)


def _guild(gid, extra_roles=(), bot_pos=10):
    bot_role = FakeRole(900, "bot", position=bot_pos)
    me = FakeMember(99, None, [bot_role])
    me.top_role = bot_role
    g = FakeGuild(gid, [EVERYONE, bot_role, *extra_roles], me=me)
    me.guild = g
    return g


class SettingsTests(unittest.TestCase):
    """A server's prune policy, as read back out of its config."""

    def setUp(self):
        sc._cache.clear()
        from cogs import verify_prune as vp
        self.vp = vp

    def _settings(self, gid, **cfg):
        sc.set_config(gid, **cfg)
        sc._cache.clear()
        return self.vp.Settings(_guild(gid, [FakeRole(500, "Quarantined", position=9)]))

    def test_off_by_default(self):
        """A server that has never touched this must never remove anyone."""
        st = self._settings(1001)
        self.assertFalse(st.enabled)
        self.assertFalse(st.runnable)

    def test_needs_altguard_on(self):
        """Prune on but gate off: LinkGuard/anti-nuke put people in that same
        role, and removing them for 'not verifying' would be wrong."""
        st = self._settings(1002, prune_enabled=1, altguard_enabled=0,
                            quarantine_role_id=500)
        self.assertTrue(st.enabled)
        self.assertFalse(st.runnable)

    def test_needs_a_quarantine_role(self):
        st = self._settings(1003, prune_enabled=1, altguard_enabled=1,
                            quarantine_role_id=None)
        self.assertFalse(st.runnable)

    def test_fully_configured_runs(self):
        st = self._settings(1004, prune_enabled=1, altguard_enabled=1,
                            quarantine_role_id=500, prune_hours=2)
        self.assertTrue(st.runnable)
        self.assertEqual(st.hours, 2)

    def test_hours_floor(self):
        """0 hours would kick everyone currently held. It must clamp, not pass."""
        st = self._settings(1005, prune_enabled=1, altguard_enabled=1,
                            quarantine_role_id=500, prune_hours=0)
        self.assertEqual(st.hours, self.vp.MIN_HOURS)

    def test_hours_ceiling_and_junk(self):
        self.assertEqual(self._settings(1006, prune_hours=999999).hours, self.vp.MAX_HOURS)
        self.assertEqual(self._settings(1007, prune_hours="nonsense").hours, 72)
        self.assertEqual(self._settings(1008, prune_hours=None).hours, 72)

    def test_hours_accepts_numeric_string(self):
        """A form post is all strings; 2 typed in a box must mean 2 hours."""
        self.assertEqual(self._settings(1009, prune_hours="2").hours, 2)

    def test_action_defaults_to_kick(self):
        """Anything we don't recognise must be the reversible action."""
        self.assertEqual(self._settings(1010, prune_action="obliterate").action, "kick")
        self.assertEqual(self._settings(1011, prune_action=None).action, "kick")
        self.assertEqual(self._settings(1012, prune_action="BAN").action, "ban")

    def test_max_per_cycle_never_zero(self):
        self.assertEqual(self._settings(1013, prune_max_per_cycle=0).max_per_cycle, 1)
        self.assertEqual(self._settings(1014, prune_max_per_cycle="x").max_per_cycle, 25)

    def test_auto_release_only_in_the_gate_guild(self):
        """_auto_release runs through the AltGuard cog, which only knows the one
        legacy guild. Elsewhere the honest answer is 'hold for review'."""
        self.assertEqual(
            self._settings(2222, prune_spare_action="release").spare_action, "review")
        sc.set_config(111, prune_spare_action="release")
        sc._cache.clear()
        st = self.vp.Settings(_guild(111, [FakeRole(500, "Q", position=9)]))
        self.assertEqual(st.spare_action, "release")

    def test_whitelist_merges_server_list(self):
        st = self._settings(1015, whitelist=[4242])
        self.assertIn("4242", st.whitelist)

    def test_dm_default_when_blank(self):
        st = self._settings(1016, prune_dm="   ")
        self.assertEqual(st.dm, self.vp.DM_DEFAULT)

    def test_dm_renders_placeholders(self):
        st = self._settings(1017, prune_dm="Bye {guild}, you had {hours}h")
        self.assertEqual(st.render_dm(_guild(1017)), "Bye Test, you had 72h")

    def test_dm_survives_a_stray_brace(self):
        """A typo in the template must not turn a warned removal into a silent
        one — send the text as written rather than raising."""
        st = self._settings(1018, prune_dm="see you {soon}")
        self.assertEqual(st.render_dm(_guild(1018)), "see you {soon}")


class QuarantineHelperTests(unittest.TestCase):
    def setUp(self):
        sc._cache.clear()

    def test_role_id_from_config(self):
        sc.set_config(3001, quarantine_role_id=777)
        sc._cache.clear()
        self.assertEqual(qt.role_id_for(_guild(3001)), 777)

    def test_role_id_junk_is_zero_not_a_crash(self):
        sc.set_config(3002, quarantine_role_id="not-an-id")
        sc._cache.clear()
        self.assertEqual(qt.role_id_for(_guild(3002)), 0)

    def test_removable_skips_managed_and_high_roles(self):
        g = _guild(3003, bot_pos=10)
        qrole = FakeRole(500, "Q", position=9)
        booster = FakeRole(501, "Booster", position=3, managed=True)
        normal = FakeRole(502, "Member", position=2)
        above = FakeRole(503, "Admin", position=20)
        m = FakeMember(7, g, [EVERYONE, qrole, booster, normal, above])
        got = qt.removable_roles(m, qrole)
        self.assertEqual([r.id for r in got], [502])

    def test_blocked_reports_powerful_roles_we_cannot_strip(self):
        """The dangerous outcome is reporting a successful quarantine on someone
        who kept their admin role."""
        g = _guild(3004, bot_pos=10)
        qrole = FakeRole(500, "Q", position=9)
        admin = FakeRole(504, "Owner", position=30, administrator=True)
        harmless = FakeRole(505, "Colour", position=25)
        m = FakeMember(8, g, [EVERYONE, qrole, admin, harmless])
        blocked = qt.blocked_roles(m, qrole)
        self.assertEqual([r.id for r in blocked], [504])

    def test_is_held(self):
        sc.set_config(3005, quarantine_role_id=500)
        sc._cache.clear()
        g = _guild(3005)
        qrole = FakeRole(500, "Q", position=9)
        self.assertTrue(qt.is_held(FakeMember(9, g, [EVERYONE, qrole])))
        self.assertFalse(qt.is_held(FakeMember(9, g, [EVERYONE])))


class EditableMember(FakeMember):
    """FakeMember that records the single bulk role edit apply/lift performs."""

    def __init__(self, uid, guild, roles=(), raises=None):
        super().__init__(uid, guild, roles)
        self.edits = []
        self.raises = raises

    async def edit(self, roles=None, reason=None):
        if self.raises:
            raise self.raises
        self.edits.append(list(roles))
        self.roles = [EVERYONE] + list(roles)


class ApplyLiftTests(unittest.IsolatedAsyncioTestCase):
    """The round trip that matters: what they lose going in is exactly what they
    get back coming out."""

    def setUp(self):
        sc._cache.clear()
        qstore.init()
        for uid in (7001, 7002, 7003, 7004):
            qstore.pop(uid)

    async def test_round_trip_restores_exactly(self):
        sc.set_config(4001, quarantine_role_id=500)
        sc._cache.clear()
        g = _guild(4001, bot_pos=10)
        qrole = FakeRole(500, "Q", position=9)
        g._roles[500] = qrole
        mod, colour = FakeRole(601, "Mod", position=5), FakeRole(602, "Blue", position=2)
        g._roles.update({601: mod, 602: colour})
        m = EditableMember(7001, g, [EVERYONE, mod, colour])

        ok, removed, err = await qt.apply(m, "testing")
        self.assertTrue(ok, err)
        self.assertEqual({r.id for r in removed}, {601, 602})
        self.assertIn(qrole, m.roles)
        self.assertNotIn(mod, m.roles)

        ok, restored, err = await qt.lift(m, "testing")
        self.assertTrue(ok, err)
        self.assertEqual({r.id for r in restored}, {601, 602})
        self.assertNotIn(qrole, m.roles)
        self.assertIn(mod, m.roles)

    async def test_roles_are_stored_before_they_are_removed(self):
        """If the role edit fails, the snapshot must still exist — otherwise a
        half-applied quarantine loses roles with no way back."""
        sc.set_config(4002, quarantine_role_id=500)
        sc._cache.clear()
        g = _guild(4002)
        g._roles[500] = FakeRole(500, "Q", position=9)
        mod = FakeRole(603, "Mod", position=5)
        g._roles[603] = mod
        m = EditableMember(7002, g, [EVERYONE, mod], raises=discord.Forbidden.__new__(discord.Forbidden))

        ok, removed, err = await qt.apply(m, "testing")
        self.assertFalse(ok)
        self.assertIsNotNone(err)
        self.assertEqual(qstore.get(7002), [603])   # recoverable

    async def test_lift_in_another_guild_does_not_eat_the_snapshot(self):
        """Held in server A, someone runs /unquarantine in server B. B must not
        consume A's saved roles — A could then never restore them."""
        qstore.save(7003, 4003, [701, 702], "held in A")
        sc.set_config(4004, quarantine_role_id=500)
        sc._cache.clear()
        g_b = _guild(4004)
        g_b._roles[500] = FakeRole(500, "Q", position=9)
        m = EditableMember(7003, g_b, [EVERYONE, g_b._roles[500]])

        ok, restored, err = await qt.lift(m, "wrong server")
        self.assertTrue(ok, err)
        self.assertEqual(restored, [])
        self.assertEqual(qstore.get(7003), [701, 702])   # server A's roles survive

    async def test_apply_refuses_without_a_role(self):
        """Better to say so than to report a quarantine that never happened."""
        sc.set_config(4005, quarantine_role_id=None)
        sc._cache.clear()
        m = EditableMember(7004, _guild(4005), [EVERYONE])
        ok, removed, err = await qt.apply(m, "testing")
        self.assertFalse(ok)
        self.assertIn("quarantine role", err)
        self.assertEqual(m.edits, [])


if __name__ == "__main__":
    unittest.main()
