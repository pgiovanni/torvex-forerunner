"""AltGuard partner-role coexistence — the roles we must NEVER re-strip.

While a member is held, `on_member_update` re-strips every role they gain, so
an autorole bot can't race the gate. That is correct for MEE6 handing out
@Member; it is a trap when the role came from a *partner verification bot*.

The failure it caused: member is held by AltGuard, then clears carl-bot's
check, carl grants its verified role, AltGuard instantly yanks it back. Carl
believes they passed, AltGuard believes they're held, and the member is stuck
in both systems with no way out. Phase 1 added a `partner_roles` allowlist that
the re-strip skips.

Load-bearing assertions:
  * a gained partner role is left in place and produces NO role edit at all;
  * a non-partner role gained in the same event is still stripped;
  * ids configured as strings (which is what the dashboard stores) match the
    int ids discord.py hands us — the exact mismatch that silently disabled
    anti-nuke whitelists in production today.
"""
import asyncio
import os
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_TMP = tempfile.mkdtemp(prefix="partnerroles")
os.environ["TORVEX_SECURITY_DB"] = os.path.join(_TMP, "security_config.db")
os.environ.setdefault("ALTGUARD_GUILD_ID", "111")

import discord  # noqa: E402
import quarantine_store as qstore  # noqa: E402

qstore._PATH = os.path.join(_TMP, "altguard_quarantine.db")
qstore.init()

import cogs.altguard as ag  # noqa: E402
from utils import altguard_mode as agmode  # noqa: E402

HOME = ag.GUILD_ID
PARTNER_ROLE = 5001          # carl-bot's "Verified"
AUTOROLE = 5002              # MEE6's "@Member" — must still be stripped
QROLE = ag.QUARANTINE_ROLE_ID or 9999


class FakeRole:
    def __init__(self, rid, name="r", position=5, managed=False):
        self.id = rid
        self.name = name
        self.position = position
        self.managed = managed
        self.mention = f"@{name}"

    def is_default(self):
        return self.position == 0

    def __ge__(self, other):
        return self.position >= other.position

    def __gt__(self, other):
        return self.position > other.position

    def __lt__(self, other):
        return self.position < other.position

    def __le__(self, other):
        return self.position <= other.position

    def __hash__(self):
        return hash(self.id)

    def __eq__(self, other):
        return isinstance(other, FakeRole) and other.id == self.id


EVERYONE = FakeRole(1, "@everyone", position=0)
BOT_TOP = FakeRole(900, "bot", position=100)


class FakeChannel:
    def __init__(self, sink):
        self.sink = sink

    async def send(self, content=None, **kw):
        self.sink.append(content)


class FakeGuild:
    def __init__(self, gid, roles, sink):
        self.id = gid
        self._roles = {r.id: r for r in roles}
        self.me = types.SimpleNamespace(top_role=BOT_TOP)
        self._sink = sink

    def get_role(self, rid):
        return self._roles.get(rid)

    def get_channel(self, cid):
        return FakeChannel(self._sink)


class FakeMember:
    def __init__(self, uid, guild, roles, edits, pending=False):
        self.id = uid
        self.bot = False
        self.guild = guild
        self.roles = list(roles)
        self.mention = f"<@{uid}>"
        self.pending = pending
        self._edits = edits

    async def edit(self, roles=None, reason=None):
        self._edits.append([r.id for r in (roles or [])])


class PartnerRoleIdCoercion(unittest.TestCase):
    """utils.altguard_mode.partner_role_ids — the safe parser that exists for
    exactly this config key."""

    def test_string_ids_become_ints(self):
        self.assertEqual(agmode.partner_role_ids({"partner_roles": ["5001", "5002"]}),
                         {5001, 5002})

    def test_mixed_types_and_padding(self):
        self.assertEqual(agmode.partner_role_ids({"partner_roles": [5001, " 5002 "]}),
                         {5001, 5002})

    def test_junk_is_dropped_not_raised(self):
        self.assertEqual(agmode.partner_role_ids({"partner_roles": ["5001", "carl-bot", None]}),
                         {5001})

    def test_absent_and_empty(self):
        self.assertEqual(agmode.partner_role_ids({}), set())
        self.assertEqual(agmode.partner_role_ids({"partner_roles": []}), set())
        self.assertEqual(agmode.partner_role_ids({"partner_roles": None}), set())


class ReStripSkipsPartnerRoles(unittest.IsolatedAsyncioTestCase):
    """The listener itself, with Discord faked out."""

    def setUp(self):
        self.edits = []       # role-id lists passed to member.edit()
        self.posts = []       # mod-log messages
        self.added = []       # qstore.add_roles calls
        self._orig = (ag.get_config, qstore.is_quarantined, qstore.add_roles,
                      ag.QUARANTINE_ROLE_ID)
        ag.QUARANTINE_ROLE_ID = QROLE
        qstore.is_quarantined = lambda uid: True
        qstore.add_roles = lambda uid, gid, ids, **kw: self.added.append(list(ids))
        self.cfg = {}
        ag.get_config = lambda gid: self.cfg

    def tearDown(self):
        (ag.get_config, qstore.is_quarantined, qstore.add_roles,
         ag.QUARANTINE_ROLE_ID) = self._orig

    def _members(self, gained_ids):
        """A held member who has just gained `gained_ids`."""
        qrole = FakeRole(QROLE, "Quarantined", position=9)
        gained = [FakeRole(rid, f"role{rid}") for rid in gained_ids]
        all_roles = [EVERYONE, qrole, BOT_TOP, *gained]
        guild = FakeGuild(HOME, all_roles, self.posts)
        before = FakeMember(42, guild, [EVERYONE, qrole], self.edits)
        after = FakeMember(42, guild, [EVERYONE, qrole, *gained], self.edits)
        return before, after

    async def test_partner_role_alone_is_left_in_place(self):
        self.cfg = {"partner_roles": [PARTNER_ROLE]}
        before, after = self._members([PARTNER_ROLE])
        await ag.AltGuard.on_member_update(
            ag.AltGuard.__new__(ag.AltGuard), before, after)
        self.assertEqual(self.edits, [], "a partner role must never trigger a role edit")
        self.assertEqual(self.added, [], "and must not be folded into the restore set")
        self.assertTrue(any("partner role" in (p or "") for p in self.posts),
                        "the mod-log should say it was left in place")

    async def test_non_partner_role_is_still_stripped(self):
        """The protection this listener exists for must survive the allowlist."""
        self.cfg = {"partner_roles": [PARTNER_ROLE]}
        before, after = self._members([AUTOROLE])
        await ag.AltGuard.on_member_update(
            ag.AltGuard.__new__(ag.AltGuard), before, after)
        self.assertEqual(len(self.edits), 1, "the autorole should have been stripped")
        self.assertNotIn(AUTOROLE, self.edits[0])
        self.assertEqual(self.added, [[AUTOROLE]], "stored for restore on pass")

    async def test_mixed_grant_strips_only_the_autorole(self):
        self.cfg = {"partner_roles": [PARTNER_ROLE]}
        before, after = self._members([PARTNER_ROLE, AUTOROLE])
        await ag.AltGuard.on_member_update(
            ag.AltGuard.__new__(ag.AltGuard), before, after)
        self.assertEqual(len(self.edits), 1)
        kept = self.edits[0]
        self.assertIn(PARTNER_ROLE, kept, "partner role must survive the edit")
        self.assertNotIn(AUTOROLE, kept, "autorole must not")
        self.assertEqual(self.added, [[AUTOROLE]])

    async def test_dashboard_string_ids_match_int_role_ids(self):
        """The dashboard stores id[] fields as digit STRINGS. If the comparison
        doesn't coerce, the allowlist silently does nothing and the member is
        back to being stuck in both systems — the same class of bug that made
        anti-nuke whitelists inert in production."""
        self.cfg = {"partner_roles": [str(PARTNER_ROLE)]}
        before, after = self._members([PARTNER_ROLE])
        await ag.AltGuard.on_member_update(
            ag.AltGuard.__new__(ag.AltGuard), before, after)
        self.assertEqual(self.edits, [],
                         "string-configured partner role was not recognised")

    async def test_no_partner_roles_configured_behaves_as_before(self):
        self.cfg = {}
        before, after = self._members([AUTOROLE])
        await ag.AltGuard.on_member_update(
            ag.AltGuard.__new__(ag.AltGuard), before, after)
        self.assertEqual(len(self.edits), 1)

    async def test_junk_in_partner_roles_must_not_break_the_listener(self):
        """REGRESSION (fixed 2026-08-23) — cogs/altguard.py:822 uses a raw `{int(x) for x in ...}`
        instead of utils.altguard_mode.partner_role_ids(), so one non-numeric
        entry raises inside the listener and the whole re-strip is skipped:
        the autorole race protection silently switches off for every held
        member until the config is corrected. Identical in shape to the
        anti-nuke id-coercion bug fixed today in 65f5407, which is why the safe
        parser already exists a module away. Expected-failure so the suite stays
        green while documenting it; flip to a normal test once it's fixed."""
        self.cfg = {"partner_roles": ["carl-bot"]}     # an admin typed a name
        before, after = self._members([AUTOROLE])
        await ag.AltGuard.on_member_update(
            ag.AltGuard.__new__(ag.AltGuard), before, after)
        self.assertEqual(len(self.edits), 1,
                         "junk config must fail closed, not abort the listener")


if __name__ == "__main__":
    unittest.main(verbosity=1)
