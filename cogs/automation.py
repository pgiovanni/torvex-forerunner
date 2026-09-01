"""Join & Welcome — join roles + welcome/goodbye messages.

Per-guild, opt-in, configured from the dashboard (or /welcome). Nothing here
runs until `auto_enabled` is set for that guild, so adding the bot to a server
never changes its behaviour on its own.

Deliberately small: this is the "MEE6 basics" layer. Anything that punishes or
restricts lives in the security cogs, not here.

NAMING: this used to be called Automation and own the /automation command. That
name now belongs to cogs/auto_rules.py — the rule builder, which is what people
mean by automation — so this is /welcome. The config keys stay `auto_*`, because
renaming them would orphan every guild's stored settings for the sake of a label.
"""
import asyncio
import os
import sys

import discord
from discord import app_commands
from discord.ext import commands

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from utils.security_config import get_config, set_config, is_enabled  # noqa: E402
from utils.quiet_removals import is_quiet  # noqa: E402

MAX_AUTOROLES = 10          # a runaway config shouldn't mean 50 role writes per join
MAX_DELAY = 3600


def render(template: str, member: discord.Member) -> str:
    """Substitute the handful of tokens we support. Kept to plain str.replace —
    no format()/f-string evaluation, so a template can never reach attributes."""
    guild = member.guild
    return (template
            .replace("{mention}", member.mention)
            .replace("{user}", member.display_name)
            .replace("{username}", member.name)
            .replace("{server}", guild.name)
            .replace("{count}", str(guild.member_count or 0)))[:2000]


class Automation(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # ───────────────────────────────────────────────────────────── join / leave
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot or not is_enabled(member.guild.id, "auto"):
            return
        cfg = get_config(member.guild.id)
        await self._welcome(member, cfg)
        # Onboarding-pending members can't see channels yet; granting now would
        # also race the verification gate, so wait for on_member_update instead.
        if cfg.get("autorole_skip_pending") and getattr(member, "pending", False):
            return
        if self._gate_defers(member):
            return
        await self._grant(member, cfg)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        """Grant join roles once Discord onboarding completes (pending -> False)."""
        if after.bot or not (before.pending and not after.pending):
            return
        if not is_enabled(after.guild.id, "auto"):
            return
        cfg = get_config(after.guild.id)
        if cfg.get("autorole_skip_pending") and not self._gate_defers(after):
            await self._grant(after, cfg)

    def _gate_defers(self, member) -> bool:
        """Tandem with AltGuard: while the verification gate holds this guild's
        joiners (or this member is already quarantined), join roles are granted
        by the gate's release path instead — granting here would race the
        quarantine reconciliation strip. No AltGuard cog → nothing defers."""
        ag = self.bot.get_cog("AltGuard")
        if ag is None:
            return False
        try:
            return ag.joins_held(member.guild) or ag.is_held(member.id)
        except Exception:
            return False

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        if member.bot or is_quiet(member.id) or not is_enabled(member.guild.id, "auto"):
            return
        cfg = get_config(member.guild.id)
        ch = member.guild.get_channel(int(cfg.get("goodbye_channel_id") or 0))
        msg = (cfg.get("goodbye_message") or "").strip()
        if ch and msg:
            await self._send(ch, render(msg, member))

    # ─────────────────────────────────────────────────────────────── internals
    async def _welcome(self, member, cfg):
        ch = member.guild.get_channel(int(cfg.get("welcome_channel_id") or 0))
        msg = (cfg.get("welcome_message") or "").strip()
        if ch and msg:
            await self._send(ch, render(msg, member))

    async def _send(self, channel, content):
        try:
            # No pings beyond the joining member — a welcome template must never
            # become an @everyone vector for whoever can edit the config.
            await channel.send(content, allowed_mentions=discord.AllowedMentions(
                everyone=False, roles=False, users=True))
        except (discord.Forbidden, discord.HTTPException):
            pass

    async def _grant(self, member, cfg):
        ids = (cfg.get("autorole_ids") or [])[:MAX_AUTOROLES]
        if not ids:
            return
        delay = max(0, min(MAX_DELAY, int(cfg.get("autorole_delay_sec") or 0)))
        if delay:
            await asyncio.sleep(delay)
            member = member.guild.get_member(member.id)   # may have left
            if member is None:
                return
        me = member.guild.me
        roles = []
        for rid in ids:
            r = member.guild.get_role(int(rid))
            # never grant a managed/booster role, and never one at or above our
            # own top role — that would either fail or escalate privilege
            if r and not r.managed and me and r < me.top_role and r not in member.roles:
                roles.append(r)
        if not roles:
            return
        try:
            await member.add_roles(*roles, reason="Automation: join roles")
        except (discord.Forbidden, discord.HTTPException):
            pass

    # ───────────────────────────────────────────────────────────────── commands
    group = app_commands.Group(
        name="welcome", description="Join roles + welcome messages (Admin only)",
        default_permissions=discord.Permissions(administrator=True))

    @group.command(name="status", description="Show this server's join & welcome settings.")
    @app_commands.checks.has_permissions(administrator=True)
    async def status(self, interaction: discord.Interaction):
        cfg = get_config(interaction.guild.id)

        def ch(cid):
            c = interaction.guild.get_channel(int(cid or 0))
            return c.mention if c else "—"

        roles = ", ".join(
            r.mention for r in (interaction.guild.get_role(int(i))
                                for i in (cfg.get("autorole_ids") or [])) if r) or "—"
        e = discord.Embed(
            title="👋 Join & Welcome",
            description=("🟢 **On**" if cfg.get("auto_enabled") else "🔴 **Off** — nothing runs"),
            color=0x5B8CFF)
        e.add_field(name="Join roles", value=roles, inline=False)
        e.add_field(name="Delay", value=f"{cfg.get('autorole_delay_sec', 0)}s", inline=True)
        e.add_field(name="Wait for onboarding",
                    value="yes" if cfg.get("autorole_skip_pending") else "no", inline=True)
        e.add_field(name="Welcome", value=ch(cfg.get("welcome_channel_id")), inline=True)
        e.add_field(name="Goodbye", value=ch(cfg.get("goodbye_channel_id")), inline=True)
        e.set_footer(text="Configure at dashboard.torvex.app")
        await interaction.response.send_message(embed=e, ephemeral=True)

    @group.command(name="enable",
                   description="Turn join roles + welcome messages on or off.")
    @app_commands.checks.has_permissions(administrator=True)
    async def enable(self, interaction: discord.Interaction, on: bool):
        set_config(interaction.guild.id, auto_enabled=1 if on else 0)
        await interaction.response.send_message(
            f"Join & Welcome is now **{'on' if on else 'off'}**.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(Automation(bot))
