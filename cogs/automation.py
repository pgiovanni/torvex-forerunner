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
import time

import discord
from discord import app_commands
from discord.ext import commands

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from utils.security_config import get_config, set_config, is_enabled  # noqa: E402
from utils.quiet_removals import is_quiet  # noqa: E402

MAX_AUTOROLES = 10          # a runaway config shouldn't mean 50 role writes per join
MAX_DELAY = 3600
SYNC_PROGRESS_EVERY = 100   # members between progress edits of the ephemeral reply
SYNC_TOKEN_LIFE = 14 * 60   # interaction tokens die at 15 min; stop editing before then


def sync_targets(members, role, *, skip_pending=False, is_held=None):
    """Who `/welcome sync` grants `role` to. Pure, so it's testable.

    Everyone currently in the server who is human, doesn't already wear the
    role, isn't still in Discord onboarding (when the panel says to wait for
    it) and isn't held by the verification gate — a held member gets the
    join roles from the gate's release path, same as at join time.
    """
    out = []
    for m in members:
        if getattr(m, "bot", False) or role in m.roles:
            continue
        if skip_pending and getattr(m, "pending", False):
            continue
        if is_held is not None and is_held(m.id):
            continue
        out.append(m)
    return out


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
        self._syncing: set[int] = set()     # guild ids with a /welcome sync running

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

    def _held(self, uid: int) -> bool:
        """Is this member currently quarantined by AltGuard? Unlike
        `_gate_defers` this ignores whether the gate holds NEW joiners — a
        back-fill of existing members must still run while the gate is on."""
        ag = self.bot.get_cog("AltGuard")
        if ag is None:
            return False
        try:
            return bool(ag.is_held(uid))
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

    @group.command(name="sync",
                   description="Give one of the join roles to every current member who doesn't have it yet.")
    @app_commands.describe(
        role="Which join role to back-fill — must already be in this server's join-roles list")
    @app_commands.checks.has_permissions(administrator=True)
    async def sync(self, interaction: discord.Interaction, role: discord.Role):
        """Back-fill ONE join role across the existing membership.

        For the case where a role is added to the join list after the server
        already has members: joiners from now on get it at join, this catches
        everyone who was already here. One role at a time, and only a role
        that is on the join list — the other join roles may be ping roles
        people have deliberately dropped, and this must never become a
        generic "give everyone a role" that could hand out permissions.
        """
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        cfg = get_config(guild.id)
        join_ids = {int(x) for x in (cfg.get("autorole_ids") or []) if str(x).isdigit()}
        if not cfg.get("auto_enabled"):
            await interaction.followup.send(
                "🔴 Join & Welcome is **off** here — turn it on first (`/welcome enable on:True`).",
                ephemeral=True)
            return
        if role.id not in join_ids:
            await interaction.followup.send(
                f"{role.mention} isn't one of this server's join roles, so new members wouldn't "
                "get it either. Add it under **Join & Welcome → Roles given on join** on the "
                "dashboard first, then run this again.", ephemeral=True)
            return
        me = guild.me
        if role.managed or role.is_default() or not me or role >= me.top_role:
            await interaction.followup.send(
                f"I can't grant {role.mention} — it's managed, or at/above my top role.",
                ephemeral=True)
            return
        if guild.id in self._syncing:
            await interaction.followup.send(
                "A sync is already running in this server — let it finish first.", ephemeral=True)
            return
        if guild.member_count and len(guild.members) < guild.member_count * 0.9:
            try:
                await guild.chunk()
            except Exception:
                pass
        targets = sync_targets(guild.members, role,
                               skip_pending=bool(cfg.get("autorole_skip_pending")),
                               is_held=self._held)
        if not targets:
            await interaction.followup.send(
                f"Everyone already has {role.mention} — nothing to do.", ephemeral=True)
            return
        eta = len(targets)          # Discord paces role writes at roughly one a second
        await interaction.followup.send(
            f"⏳ Giving {role.mention} to **{len(targets)}** member(s) — about "
            f"{eta // 60}m {eta % 60}s. I'll update this message as it goes"
            + (" and DM you the result if it outlives this reply." if eta > SYNC_TOKEN_LIFE else "."),
            ephemeral=True)
        self._syncing.add(guild.id)
        self.bot.loop.create_task(self._sync_run(interaction, role, targets))

    async def _sync_run(self, interaction, role, targets):
        guild = interaction.guild
        started = time.monotonic()
        ok, gone, failed = 0, 0, []

        async def progress(text, final=False):
            # The ephemeral reply is editable only while the interaction token
            # lives (15 min); past that, the final word goes by DM.
            if time.monotonic() - started < SYNC_TOKEN_LIFE:
                try:
                    await interaction.edit_original_response(content=text)
                    return
                except (discord.HTTPException, discord.NotFound):
                    pass
            if final:
                try:
                    await interaction.user.send(text)
                except (discord.Forbidden, discord.HTTPException):
                    pass

        try:
            for i, m in enumerate(targets, 1):
                try:
                    await m.add_roles(role, reason=f"Join & Welcome: sync by {interaction.user}")
                    ok += 1
                except discord.NotFound:
                    gone += 1                       # left mid-run
                except (discord.Forbidden, discord.HTTPException):
                    failed.append(m)
                if i % SYNC_PROGRESS_EVERY == 0:
                    await progress(f"⏳ {role.mention}: **{i}/{len(targets)}** done "
                                   f"({ok} granted, {len(failed)} failed).")
        finally:
            self._syncing.discard(guild.id)
        names = ", ".join(f"{m.mention} (`{m.id}`)" for m in failed[:20])
        text = (f"✅ {role.mention} sync in **{guild.name}** finished: **{ok}** granted"
                + (f", {gone} left mid-run" if gone else "")
                + (f", **{len(failed)}** failed (perms/hierarchy): {names}" if failed else "")
                + ".")
        print(f"[WELCOME] sync {guild.id} role={role.id} ok={ok} gone={gone} failed={len(failed)}")
        await progress(text, final=True)


async def setup(bot):
    await bot.add_cog(Automation(bot))
