"""Quarantine-role lockdown maintainer for peepos-reclaimer.

Guarantees the AltGuard quarantine role is locked out of EVERY channel and STAYS
that way — can't view, can't send, can't thread, can't react, can't talk in
voice. Enforced three ways so it can never drift:
  * startup sweep (on_ready) — fix every existing channel
  * on_guild_channel_create — lock new channels the instant they appear
  * on_guild_channel_update — revert any overwrite drift (someone re-allows it)
  * /quarantine-lock — manual full sweep

Reuses ALTGUARD_GUILD_ID + ALTGUARD_QUARANTINE_ROLE_ID. Optional
ALTGUARD_LOCKDOWN_EXEMPT = space/comma channel IDs to leave visible (e.g. a
#verify channel for closed-DM members). Default: lock everything.
"""
import asyncio
import os
import logging

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger("qlock")


def _env_int(name, default=0):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


GUILD_ID = _env_int("ALTGUARD_GUILD_ID")
QROLE_ID = _env_int("ALTGUARD_QUARANTINE_ROLE_ID")
EXEMPT = {int(x) for x in os.environ.get("ALTGUARD_LOCKDOWN_EXEMPT", "").replace(",", " ").split() if x.strip().isdigit()}

# the locked-down state: see nothing, do nothing. All explicitly DENIED.
LOCK = dict(
    view_channel=False, send_messages=False, send_messages_in_threads=False,
    create_public_threads=False, create_private_threads=False,
    add_reactions=False, connect=False, speak=False,
)

VERIFY_CHANNEL_ID = _env_int("ALTGUARD_VERIFY_CHANNEL_ID")
# the verify channel is the ONE maintained exception: held members must SEE it and
# read the panel to click the Verify button — but still can't type. Visible + silent.
VERIFY_OV = dict(
    view_channel=True, read_message_history=True, send_messages=False,
    send_messages_in_threads=False, create_public_threads=False,
    create_private_threads=False, add_reactions=False, connect=False, speak=False,
)


class QuarantineLock(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._swept = False

    def _role(self, guild):
        return guild.get_role(QROLE_ID)

    def _desired(self, channel):
        return VERIFY_OV if channel.id == VERIFY_CHANNEL_ID else LOCK

    def _needs_fix(self, channel, role):
        want = self._desired(channel)
        ov = channel.overwrites_for(role)
        return any(getattr(ov, k) is not v for k, v in want.items())

    async def _lock(self, channel, role, reason):
        if channel.id in EXEMPT or not self._needs_fix(channel, role):
            return False
        ov = channel.overwrites_for(role)
        ov.update(**self._desired(channel))
        try:
            await channel.set_permissions(role, overwrite=ov, reason=reason)
            return True
        except discord.Forbidden:
            log.warning("can't lock quarantine role on %s (perms/hierarchy)", channel)
            return False
        except discord.HTTPException:
            return False

    async def sweep(self, guild):
        role = self._role(guild)
        if not role:
            return 0, 0
        fixed = total = 0
        for ch in guild.channels:
            total += 1
            if await self._lock(ch, role, "quarantine lockdown (sweep)"):
                fixed += 1
                await asyncio.sleep(0.3)  # gentle: don't burst the API / trip Wick
        return fixed, total

    @commands.Cog.listener()
    async def on_ready(self):
        if self._swept:
            return
        guild = self.bot.get_guild(GUILD_ID)
        if guild:
            fixed, total = await self.sweep(guild)
            self._swept = True
            log.info("quarantine lockdown sweep: %d/%d channels corrected", fixed, total)

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel):
        if channel.guild.id != GUILD_ID:
            return
        role = self._role(channel.guild)
        if role:
            await self._lock(channel, role, "quarantine lockdown (new channel)")

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before, after):
        # someone changed this channel's perms — re-assert our denies if drifted
        if after.guild.id != GUILD_ID:
            return
        role = self._role(after.guild)
        if role and await self._lock(after, role, "quarantine lockdown (drift reverted)"):
            log.info("reverted quarantine overwrite drift on #%s", after)

    @app_commands.command(name="quarantine-lock",
                          description="Force-lock the quarantine role out of every channel (admin)")
    @app_commands.default_permissions(administrator=True)
    async def lock_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        role = self._role(interaction.guild)
        if not role:
            await interaction.followup.send("⚠️ Quarantine role not found — check ALTGUARD_QUARANTINE_ROLE_ID.", ephemeral=True)
            return
        fixed, total = await self.sweep(interaction.guild)
        ex = f" ({len(EXEMPT)} exempt)" if EXEMPT else ""
        await interaction.followup.send(
            f"🔒 Quarantine lockdown ensured on **{total}** channels{ex} — {fixed} needed fixing. "
            f"New channels and any future drift are auto-corrected.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(QuarantineLock(bot))
