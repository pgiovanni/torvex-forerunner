"""Quarantine-role auto-provisioning (cogs/security.py::_provision_quarantine_role).

The placement rule under test: the role must end up directly beneath the bot's
own top role. That is not cosmetic — a role BELOW a staff role can be removed by
that staff member, so a badly placed quarantine role can be undone by anyone with
Manage Roles, which quietly defeats the gate.

Fakes stand in for discord.py objects; only the ordering/permission logic is ours.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import discord  # noqa: E402
from cogs.security import Security  # noqa: E402


class FakeRole:
    def __init__(self, name, position, manage_roles=False, managed=False):
        self.id = abs(hash(name)) % 10**8
        self.name = name
        self.position = position
        self.managed = managed
        self.permissions = discord.Permissions(manage_roles=manage_roles)
        self.mention = f"@{name}"
        self.edit_calls = []
        self.raise_on_edit = None

    async def edit(self, **kw):
        if self.raise_on_edit:
            raise self.raise_on_edit
        self.edit_calls.append(kw)
        if "position" in kw:
            self.position = kw["position"]

    # discord.Role orders by position; the cog relies on `me.top_role <= role`
    def __le__(self, other):
        return self.position <= other.position

    def __lt__(self, other):
        return self.position < other.position


class FakeMe:
    def __init__(self, top_role, administrator=True):
        self.top_role = top_role
        self.guild_permissions = discord.Permissions(administrator=administrator,
                                                     manage_roles=True)


class FakeGuild:
    def __init__(self, me, roles=(), can_create=True):
        self.me = me
        self.roles = list(roles)
        self.can_create = can_create
        self.created = None

    async def create_role(self, **kw):
        if not self.can_create:
            raise discord.Forbidden(_FakeResp(), "no")
        role = FakeRole(kw.get("name", "Quarantined"), position=1)
        role.created_with = kw
        self.created = role
        self.roles.append(role)
        return role


class _FakeResp:
    status = 403
    reason = "Forbidden"


def cog():
    return Security.__new__(Security)   # no __init__: the method touches no state


class ProvisionTests(unittest.IsolatedAsyncioTestCase):

    async def test_creates_role_with_no_permissions(self):
        bot_top = FakeRole("Bot", 10)
        guild = FakeGuild(FakeMe(bot_top))
        role, created, notes = await cog()._provision_quarantine_role(guild)
        self.assertTrue(created)
        self.assertEqual(guild.created.created_with["permissions"], discord.Permissions.none())
        self.assertFalse(guild.created.created_with["hoist"])
        self.assertFalse(guild.created.created_with["mentionable"])

    async def test_lands_directly_below_the_bots_top_role(self):
        bot_top = FakeRole("Bot", 10)
        guild = FakeGuild(FakeMe(bot_top))
        role, _, _ = await cog()._provision_quarantine_role(guild)
        self.assertEqual(role.position, 9)

    async def test_never_targets_the_everyone_position(self):
        # bot sits at the very bottom: position 0 is @everyone and must be left alone
        bot_top = FakeRole("Bot", 1)
        guild = FakeGuild(FakeMe(bot_top))
        role, _, _ = await cog()._provision_quarantine_role(guild)
        self.assertEqual(role.position, 1)

    async def test_existing_role_is_repositioned_not_recreated(self):
        bot_top = FakeRole("Bot", 20)
        existing = FakeRole("Old Quarantine", 2)
        guild = FakeGuild(FakeMe(bot_top), roles=[existing])
        role, created, _ = await cog()._provision_quarantine_role(guild, existing)
        self.assertFalse(created)
        self.assertIs(role, existing)
        self.assertEqual(role.position, 19)
        self.assertIsNone(guild.created)

    async def test_already_correct_position_is_left_alone(self):
        bot_top = FakeRole("Bot", 10)
        existing = FakeRole("Quarantined", 9)
        guild = FakeGuild(FakeMe(bot_top), roles=[existing])
        role, _, notes = await cog()._provision_quarantine_role(guild, existing)
        self.assertEqual(role.edit_calls, [])
        self.assertEqual(notes, [])

    async def test_role_above_the_bot_is_refused_with_an_explanation(self):
        bot_top = FakeRole("Bot", 5)
        too_high = FakeRole("Admin-ish Quarantine", 9)
        guild = FakeGuild(FakeMe(bot_top, administrator=False), roles=[too_high])
        role, created, notes = await cog()._provision_quarantine_role(guild, too_high)
        self.assertFalse(created)
        self.assertEqual(role.edit_calls, [])          # never attempted
        self.assertTrue(any("below" in n for n in notes))

    async def test_warns_when_staff_roles_could_undo_the_quarantine(self):
        bot_top = FakeRole("Bot", 10)
        mod = FakeRole("Mod", 4, manage_roles=True)
        member = FakeRole("Member", 2)
        guild = FakeGuild(FakeMe(bot_top), roles=[mod, member])
        role, _, notes = await cog()._provision_quarantine_role(guild)
        self.assertEqual(role.position, 9)
        self.assertTrue(any("Manage Roles" in n for n in notes),
                        "should say the move put it above a role that could undo it")

    async def test_reposition_denied_still_returns_a_usable_role(self):
        bot_top = FakeRole("Bot", 10)
        existing = FakeRole("Quarantined", 2)
        existing.raise_on_edit = discord.Forbidden(_FakeResp(), "nope")
        guild = FakeGuild(FakeMe(bot_top), roles=[existing])
        role, _, notes = await cog()._provision_quarantine_role(guild, existing)
        self.assertIs(role, existing)
        self.assertEqual(role.position, 2)             # unchanged
        self.assertTrue(any("Manage Roles" in n for n in notes))

    async def test_create_forbidden_propagates_for_the_caller_to_explain(self):
        bot_top = FakeRole("Bot", 10)
        guild = FakeGuild(FakeMe(bot_top), can_create=False)
        with self.assertRaises(discord.Forbidden):
            await cog()._provision_quarantine_role(guild)


if __name__ == "__main__":
    unittest.main(verbosity=2)
