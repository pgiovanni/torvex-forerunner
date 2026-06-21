"""Quarantine-role lockdown maintainer for peepos-reclaimer (multi-guild).

Guarantees each opted-in guild's quarantine role is locked out of EVERY channel
and STAYS that way — can't view, send, thread, react, or talk in voice. Per-guild
and opt-in: only runs where `qlock` is enabled in security_config and a
quarantine_role_id is set. Enforced four ways so it can never drift:
  * startup sweep (on_ready) — every enabled guild
  * on_guild_channel_create — lock new channels instantly
  * on_guild_channel_update — revert overwrite drift
  * /quarantine-lock — manual full sweep

Per-guild quarantine role / verify channel / exempt list come from security_config.
"""
import asyncio
import os
import sys
import logging

import discord
from discord import app_commands
from discord.ext import commands

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from utils.security_config import get_config, is_enabled

log = logging.getLogger("qlock")

# the locked-down state: see nothing, do nothing. All explicitly DENIED.
LOCK = dict(
    view_channel=False, send_messages=False, send_messages_in_threads=False,
    create_public_threads=False, create_private_threads=False,
    add_reactions=False, connect=False, speak=False,
)
# the verify channel is the ONE maintained exception: held members must SEE it
# and read the panel to click Verify — but still can't type. Visible + silent.
VERIFY_OV = dict(
    view_channel=True, read_message_history=True, send_messages=False,
    send_messages_in_threads=False, create_public_threads=False,
    create_private_threads=False, add_reactions=False, connect=False, speak=False,
)


class QuarantineLock(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._swept = set()   # guild ids already swept this session

    def _role(self, guild, cfg):
        rid = cfg.get("quarantine_role_id")
        return guild.get_role(int(rid)) if rid else None

    def _desired(self, channel, cfg):
        return VERIFY_OV if channel.id == cfg.get("verify_channel_id") else LOCK

    def _needs_fix(self, channel, role, cfg):
        want = self._desired(channel, cfg)
        ov = channel.overwrites_for(role)
        return any(getattr(ov, k) is not v for k, v in want.items())

    async def _lock(self, channel, role, cfg, reason):
        exempt = set(cfg.get("lockdown_exempt") or [])
        if channel.id in exempt or not self._needs_fix(channel, role, cfg):
            return False
        ov = channel.overwrites_for(role)
        ov.update(**self._desired(channel, cfg))
        try:
            await channel.set_permissions(role, overwrite=ov, reason=reason)
            return True
        except discord.Forbidden:
            log.warning("can't lock quarantine role on %s (perms/hierarchy)", channel)
            return False
        except discord.HTTPException:
            return False

    async def sweep(self, guild):
        cfg = get_config(guild.id)
        role = self._role(guild, cfg)
        if not role:
            return 0, 0
        fixed = total = 0
        for ch in guild.channels:
            total += 1
            if await self._lock(ch, role, cfg, "quarantine lockdown (sweep)"):
                fixed += 1
                await asyncio.sleep(0.3)  # gentle: don't burst the API
        return fixed, total

    @commands.Cog.listener()
    async def on_ready(self):
        for guild in self.bot.guilds:
            if guild.id in self._swept or not is_enabled(guild.id, "qlock"):
                continue
            fixed, total = await self.sweep(guild)
            self._swept.add(guild.id)
            log.info("quarantine lockdown sweep [%s]: %d/%d channels corrected", guild.id, fixed, total)

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel):
        if not is_enabled(channel.guild.id, "qlock"):
            return
        cfg = get_config(channel.guild.id)
        role = self._role(channel.guild, cfg)
        if role:
            await self._lock(channel, role, cfg, "quarantine lockdown (new channel)")

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before, after):
        # someone changed this channel's perms — re-assert our denies if drifted
        if not is_enabled(after.guild.id, "qlock"):
            return
        cfg = get_config(after.guild.id)
        role = self._role(after.guild, cfg)
        if role and await self._lock(after, role, cfg, "quarantine lockdown (drift reverted)"):
            log.info("reverted quarantine overwrite drift on #%s", after)

    @app_commands.command(name="quarantine-lock",
                          description="Force-lock the quarantine role out of every channel (admin)")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def lock_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        cfg = get_config(interaction.guild.id)
        role = self._role(interaction.guild, cfg)
        if not role:
            await interaction.followup.send(
                "⚠️ No quarantine role set for this server — run `/security setup` first.", ephemeral=True)
            return
        fixed, total = await self.sweep(interaction.guild)
        ex = cfg.get("lockdown_exempt") or []
        exn = f" ({len(ex)} exempt)" if ex else ""
        await interaction.followup.send(
            f"🔒 Quarantine lockdown ensured on **{total}** channels{exn} — {fixed} needed fixing. "
            f"New channels and any future drift are auto-corrected.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(QuarantineLock(bot))
