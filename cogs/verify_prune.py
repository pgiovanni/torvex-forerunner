"""verify_prune — removes members who never finish verification.

A standing quarantine is a held door: an account that joins, gets quarantined-
on-join, and then just *sits* there forever is the cheapest way to keep a
foothold (and to wear down whoever's watching the gate). This closes that —
after a grace window (default 72h) an unverified member is DM'd a heads-up and
then removed.

Scope is deliberately narrow: ONLY members who currently hold the AltGuard
quarantine role. Members who verified (role removed) or who predate the gate
(never had the role) are never touched — so this can't mass-prune the existing
server.

Action is a KICK by default (reversible — they can rejoin and verify); set
PRUNE_ACTION=ban for a hard removal. DM is always attempted *before* removal,
since once they're gone there's no shared server to DM through.

Shadow-first like the rest of the suite: with PRUNE_ENFORCE=0 it only posts the
candidate list to #modlog and takes no action. PRUNE_ENFORCE=1 acts.

Reuses ALTGUARD_GUILD_ID / ALTGUARD_QUARANTINE_ROLE_ID / ALTGUARD_MODLOG_CHANNEL_ID.
Tunables:
    PRUNE_ENFORCE (0)            PRUNE_HOURS (72)
    PRUNE_ACTION (kick)         PRUNE_INTERVAL_MIN (60)
    PRUNE_MAX_PER_CYCLE (25)    PRUNE_WHITELIST ("" — space/comma uids)
    PRUNE_DM (message; {guild} placeholder; empty = skip the DM)
"""
import asyncio
import logging
import os
import time
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks

import quarantine_store as qstore

log = logging.getLogger("verify_prune")


def _env_int(name, default=0):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


GUILD_ID = _env_int("ALTGUARD_GUILD_ID")
QUARANTINE_ROLE_ID = _env_int("ALTGUARD_QUARANTINE_ROLE_ID")
MODLOG_CHANNEL_ID = _env_int("ALTGUARD_MODLOG_CHANNEL_ID")

ENFORCE = os.environ.get("PRUNE_ENFORCE", "0") != "0"
HOURS = _env_int("PRUNE_HOURS", 72)
ACTION = os.environ.get("PRUNE_ACTION", "kick").strip().lower()
INTERVAL_MIN = max(5, _env_int("PRUNE_INTERVAL_MIN", 60))
MAX_PER_CYCLE = _env_int("PRUNE_MAX_PER_CYCLE", 25)
WHITELIST = {x for x in os.environ.get("PRUNE_WHITELIST", "").replace(",", " ").split() if x.strip()}
DM_DEFAULT = (
    "Hey — you've been removed from **{guild}** because verification wasn't "
    "completed in time (sorry, you took too long!). No hard feelings: you're "
    "welcome to rejoin and verify whenever you're ready."
)
DM_TEXT = os.environ.get("PRUNE_DM", DM_DEFAULT)
# seconds between removals — keeps us under rate limits and well clear of any
# mass-action heuristic (the bot is self-exempt from anti-nuke, but be tidy)
_PACE = 2.0


class VerifyPrune(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.last_run = 0.0
        self.last_pruned = 0

    async def cog_load(self):
        if GUILD_ID and QUARANTINE_ROLE_ID:
            self.sweep.start()

    async def cog_unload(self):
        self.sweep.cancel()

    # ------------------------------------------------------------- helpers
    def _modlog(self):
        return self.bot.get_channel(MODLOG_CHANNEL_ID)

    @property
    def _tag(self) -> str:
        return "🧹 Verify-prune" if ENFORCE else "🧹 Verify-prune (shadow)"

    def _exempt(self, member: discord.Member) -> bool:
        if member.bot or str(member.id) in WHITELIST:
            return True
        if member.guild.owner_id == member.id:
            return True
        perms = member.guild_permissions
        if perms.administrator or perms.manage_guild:
            return True
        return False

    def _candidates(self, guild: discord.Guild):
        """Members holding the quarantine role who joined > HOURS ago and have
        not passed verification. joined_at is authoritative (live gateway)."""
        qrole = guild.get_role(QUARANTINE_ROLE_ID)
        if not qrole:
            return []
        cutoff = time.time() - HOURS * 3600
        out = []
        for m in qrole.members:
            if self._exempt(m):
                continue
            started = self._held_since(m)
            if started is None or started > cutoff:
                continue  # clock starts at QUARANTINE time, not join — a long-time
                          # member quarantined today gets a fresh 72h, not an instant kick
            v = qstore.verification(m.id)
            if v and v.get("status") == "passed":
                continue  # passed but role lingered — never prune a verified member
            out.append(m)
        return out

    def _held_since(self, m: discord.Member):
        """Epoch seconds when this member's verify clock started: when the
        quarantine role was applied. Falls back to when a link was issued, then
        to join time. This is the fix for kicking the just-quarantined."""
        ts = qstore.quarantined_since(m.id)
        if ts is None:
            v = qstore.verification(m.id)
            ts = v.get("issued_at") if v else None
        if ts is None and m.joined_at:
            ts = m.joined_at.timestamp()
        return ts

    # ------------------------------------------------------------- the sweep
    @tasks.loop(minutes=INTERVAL_MIN)
    async def sweep(self):
        guild = self.bot.get_guild(GUILD_ID)
        if not guild:
            return
        self.last_run = time.time()
        candidates = self._candidates(guild)
        if not candidates:
            return

        if not ENFORCE:
            await self._shadow_report(guild, candidates)
            return

        pruned, dm_failed, act_failed = [], 0, []
        for m in candidates[:MAX_PER_CYCLE]:
            # DM first — must happen while we still share the server
            if DM_TEXT:
                try:
                    await m.send(DM_TEXT.format(guild=guild.name))
                except discord.HTTPException:
                    dm_failed += 1
            reason = f"AltGuard: did not verify within {HOURS}h"
            try:
                if ACTION == "ban":
                    await m.ban(reason=reason, delete_message_seconds=0)
                    qstore.set_status(m.id, "banned")
                else:
                    await m.kick(reason=reason)
                    qstore.set_status(m.id, "pruned")
                pruned.append(m)
            except discord.Forbidden:
                act_failed.append(m)
                log.warning("prune: lack permission to %s %s", ACTION, m.id)
            except discord.HTTPException as e:
                act_failed.append(m)
                log.warning("prune: %s %s failed: %s", ACTION, m.id, e)
            await asyncio.sleep(_PACE)

        self.last_pruned = len(pruned)
        await self._enforce_report(guild, candidates, pruned, dm_failed, act_failed)

    @sweep.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(45)  # let the member cache chunk before first sweep

    # ------------------------------------------------------------- reporting
    async def _shadow_report(self, guild, candidates):
        ch = self._modlog()
        if not ch:
            return
        names = "\n".join(f"• {m.mention} `{m.id}` — {self._ago(m)}" for m in candidates[:25])
        extra = f"\n…and {len(candidates) - 25} more" if len(candidates) > 25 else ""
        e = discord.Embed(
            title=f"{self._tag} — {len(candidates)} would be {ACTION}ed",
            description=(
                f"These hold the quarantine role and joined over **{HOURS}h** ago "
                f"without verifying. **No action taken** (shadow mode).\n\n{names}{extra}"
            ),
            color=0xFFB020,
        )
        e.set_footer(text="Set PRUNE_ENFORCE=1 to act.")
        try:
            await ch.send(embed=e)
        except discord.HTTPException:
            pass

    async def _enforce_report(self, guild, candidates, pruned, dm_failed, act_failed):
        ch = self._modlog()
        if not ch:
            return
        verb = "Banned" if ACTION == "ban" else "Kicked"
        lines = "\n".join(f"• {m} `{m.id}`" for m in pruned[:25]) or "—"
        e = discord.Embed(
            title=f"🧹 Verify-prune — {verb.lower()} {len(pruned)} unverified",
            description=(
                f"Held the quarantine role and joined over **{HOURS}h** ago without "
                f"verifying.\n\n**{verb}:**\n{lines}"
            ),
            color=0xE03B3B,
        )
        if dm_failed:
            e.add_field(name="DMs not delivered", value=f"{dm_failed} (closed DMs)", inline=True)
        if act_failed:
            e.add_field(name="⚠️ Failed", value=f"{len(act_failed)} (check my perms/role order)", inline=True)
        remaining = len(candidates) - len(pruned) - len(act_failed)
        if remaining > 0:
            e.add_field(name="Deferred", value=f"{remaining} (cycle cap {MAX_PER_CYCLE})", inline=True)
        try:
            await ch.send(embed=e)
        except discord.HTTPException:
            pass

    def _ago(self, m: discord.Member) -> str:
        started = self._held_since(m)
        if not started:
            return "?"
        h = int((time.time() - started) // 3600)
        return f"held {h // 24}d" if h >= 24 else f"held {h}h"

    # ------------------------------------------------------------- commands
    @app_commands.command(name="prune-status",
                          description="Show verify-prune config + who's currently overdue (admin).")
    @app_commands.checks.has_permissions(administrator=True)
    async def prune_status(self, interaction: discord.Interaction):
        guild = self.bot.get_guild(GUILD_ID)
        candidates = self._candidates(guild) if guild else []
        e = discord.Embed(title="🧹 Verify-prune", color=0x5B8CFF)
        e.add_field(name="Mode", value="**ENFORCE**" if ENFORCE else "**shadow** (alert-only)", inline=True)
        e.add_field(name="Action", value=ACTION, inline=True)
        e.add_field(name="Grace", value=f"{HOURS}h", inline=True)
        e.add_field(name="Interval", value=f"{INTERVAL_MIN}m (cap {MAX_PER_CYCLE}/cycle)", inline=True)
        e.add_field(name="Last sweep",
                    value=(f"<t:{int(self.last_run)}:R>" if self.last_run else "not yet"), inline=True)
        names = "\n".join(f"• {m.mention} — {self._ago(m)}" for m in candidates[:15]) or "none"
        extra = f"\n…and {len(candidates) - 15} more" if len(candidates) > 15 else ""
        e.add_field(name=f"Overdue now ({len(candidates)})", value=names + extra, inline=False)
        await interaction.response.send_message(embed=e, ephemeral=True)

    @app_commands.command(name="prune-run",
                          description="Run the verify-prune sweep right now (admin).")
    @app_commands.checks.has_permissions(administrator=True)
    async def prune_run(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            f"Running a verify-prune sweep ({'enforce' if ENFORCE else 'shadow'})… "
            f"results post to <#{MODLOG_CHANNEL_ID}>.", ephemeral=True)
        await self.sweep()

    @prune_status.error
    @prune_run.error
    async def _err(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "You need **Administrator** for that.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(VerifyPrune(bot))
