"""Standalone anti-nuke logic harness — exercises the REAL AntiNuke cog against
synthetic events, no live Discord. Proves the rate/threshold/exemption decisions
still fire correctly after refactors.

Lives in the repo (versioned). Needs discord.py, so RUN it on the bot host:
    /opt/peepos-reclaimer/venv/bin/python tests/test_antinuke.py
Exits non-zero if any scenario fails.

It injects attribution (cog._executor) and stubs external side-effects
(quarantine_store.save, recovery invites) so it tests the DECISION logic — the
part that matters — and asserts on the real enforcement calls (member.edit =
strip+quarantine, member.timeout, member.kick, guild.unban).
"""
import asyncio
import os
import sys
import types
from unittest.mock import AsyncMock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

import quarantine_store as qstore  # noqa: E402
qstore.save = lambda *a, **k: None  # don't touch the real store during tests

import discord  # noqa: E402
import cogs.antinuke as an  # noqa: E402

GID, OWNER, BOTID, WL = 999_000_000_000_000_001, 1, 2, 3

CFG = {
    "antinuke_enabled": 1, "antinuke_enforce": 1, "antinuke_restore_bans": 1,
    "antinuke_timeout_min": 10, "quarantine_role_id": 555, "modlog_channel_id": 777,
    "whitelist": [WL],
}
# the cog bound get_config/is_enabled by name at import — patch them on the module
an.get_config = lambda gid: dict(CFG)
an.is_enabled = lambda gid, feat: bool(CFG.get(feat + "_enabled"))


class Role:
    def __init__(self, rid, pos=1, managed=False, default=False, perms=0):
        self.id = rid; self.position = pos; self.managed = managed; self._d = default
        self.name = "role%d" % rid; self.permissions = types.SimpleNamespace(value=perms)

    def is_default(self): return self._d
    def __ge__(self, o): return self.position >= o.position
    def __lt__(self, o): return self.position < o.position


class Member:
    def __init__(self, mid, roles=None, is_bot=False, admin=False):
        self.id = mid; self.roles = roles or [Role(1000 + mid, pos=5)]; self.bot = is_bot
        self.guild_permissions = types.SimpleNamespace(administrator=admin)
        self.mention = "<@%d>" % mid
        self.edit = AsyncMock(); self.timeout = AsyncMock(); self.kick = AsyncMock()

    def __str__(self): return "user%d" % self.id


class Guild:
    def __init__(self):
        self.id = GID; self.owner_id = OWNER; self.name = "TestGuild"
        self.me = types.SimpleNamespace(top_role=Role(900, pos=500), id=BOTID)
        self._members = {}; self._roles = {555: Role(555, pos=10)}
        self._modlog = types.SimpleNamespace(send=AsyncMock())
        self.unban = AsyncMock(); self.system_channel = None; self.text_channels = []

    def add(self, m): self._members[m.id] = m; return m
    def get_member(self, uid): return self._members.get(uid)
    def get_role(self, rid): return self._roles.get(rid)
    def get_channel(self, cid): return self._modlog


class Msg:
    def __init__(self, guild, author, mentions=0, role_mentions=0, everyone=False):
        self.guild = guild; self.author = author; self.webhook_id = None
        self.mentions = [None] * mentions; self.role_mentions = [None] * role_mentions
        self.mention_everyone = everyone


BOT = types.SimpleNamespace(user=types.SimpleNamespace(id=BOTID))


def fresh(executor=None):
    """A fresh cog+guild; executor (if given) is the attributed actor for
    audit-log events and is added to the guild."""
    cog = an.AntiNuke(BOT)
    cog._recovery_invite = AsyncMock(return_value=None)
    g = Guild()
    if executor is not None:
        g.add(executor)
        cog._executor = AsyncMock(return_value=executor)
    return cog, g


# ----------------------------------------------------------------- scenarios
async def s_mass_channel_delete():
    ex = Member(100); cog, g = fresh(ex)
    for i in range(3):  # limit is (3, 12)
        await cog._record_action(g, "channel_delete", 5000 + i)
    return ex.edit.called  # strip + quarantine

async def s_below_threshold():
    ex = Member(101); cog, g = fresh(ex)
    for i in range(2):  # below the 3 limit
        await cog._record_action(g, "channel_delete", 5000 + i)
    return not ex.edit.called

async def s_mass_ban():
    ex = Member(102); cog, g = fresh(ex)
    for i in range(5):  # limit (5, 20), distinct victims
        await cog._record_action(g, "ban", 6000 + i)
    return ex.edit.called and g.unban.called  # quarantined + victims unbanned

async def s_owner_exempt():
    owner = Member(OWNER); cog, g = fresh(owner)
    for i in range(6):
        await cog._record_action(g, "channel_delete", 5000 + i)
    return not owner.edit.called

async def s_whitelist_exempt():
    wl = Member(WL); cog, g = fresh(wl)
    for i in range(6):
        await cog._record_action(g, "role_delete", 5000 + i)
    return not wl.edit.called

async def s_mention_bomb():
    user = Member(103); cog, g = fresh(); g.add(user)
    await cog.on_message(Msg(g, user, mentions=15))  # >= MENTION_BOMB
    return user.timeout.called

async def s_everyone_spam():
    user = Member(104); cog, g = fresh(); g.add(user)
    for _ in range(4):  # EVERYONE_RATE (4, 20)
        await cog.on_message(Msg(g, user, everyone=True))
    return user.timeout.called

async def s_flood():
    user = Member(105); cog, g = fresh(); g.add(user)
    for _ in range(12):  # FLOOD_RATE (12, 7)
        await cog.on_message(Msg(g, user))
    return user.timeout.called

async def s_legit_announcement():
    user = Member(106); cog, g = fresh(); g.add(user)
    await cog.on_message(Msg(g, user, everyone=True))  # ONE @everyone = fine
    return not user.timeout.called

async def s_legit_few_pings():
    user = Member(107); cog, g = fresh(); g.add(user)
    await cog.on_message(Msg(g, user, mentions=5))  # tagging 5 people = fine
    return not user.timeout.called

async def s_role_grant_nuke():
    ex = Member(108); cog, g = fresh(ex)
    before = Role(700, pos=3, perms=0)
    after = Role(700, pos=3, perms=discord.Permissions(administrator=True).value)
    after.edit = AsyncMock()
    after.guild = g
    await cog.on_guild_role_update(before, after)
    return after.edit.called and ex.edit.called  # reverted + actor stripped

async def s_bot_add_untrusted():
    adder = Member(109); cog, g = fresh(adder)
    newbot = Member(110, is_bot=True, admin=True)
    newbot.guild = g  # on_member_join reads member.guild
    await cog.on_member_join(newbot)
    return newbot.kick.called  # untrusted bot add -> kicked


SCENARIOS = [
    ("mass channel-delete -> strip+quarantine", s_mass_channel_delete),
    ("below threshold (2 deletes) -> no action", s_below_threshold),
    ("mass-ban -> quarantine + unban victims", s_mass_ban),
    ("owner exempt -> no action", s_owner_exempt),
    ("whitelist exempt -> no action", s_whitelist_exempt),
    ("mention-bomb (15) -> timeout", s_mention_bomb),
    ("@everyone spam (4/20s) -> timeout", s_everyone_spam),
    ("message flood (12/7s) -> timeout", s_flood),
    ("legit 1 announcement -> no action", s_legit_announcement),
    ("legit 5 pings -> no action", s_legit_few_pings),
    ("role granted nuke perms -> revert + strip", s_role_grant_nuke),
    ("untrusted bot add -> kick bot", s_bot_add_untrusted),
]


async def main():
    passed = 0
    for name, fn in SCENARIOS:
        try:
            ok = await fn()
        except Exception as e:  # a crash is a failure
            ok = False
            name += "  [EXCEPTION: %s]" % e
        print("  %s  %s" % ("PASS" if ok else "FAIL", name))
        passed += bool(ok)
    print("\n%d/%d scenarios passed" % (passed, len(SCENARIOS)))
    return passed == len(SCENARIOS)


if __name__ == "__main__":
    sys.exit(0 if asyncio.run(main()) else 1)
