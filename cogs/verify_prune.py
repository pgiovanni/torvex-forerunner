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
    PRUNE_SPARE_CLEAN (1)       PRUNE_SPARE_ACTION (review | release)
    PRUNE_DEFAULT_AGE ("" — age band stamped on an auto-approved member)
"""
import asyncio
import hashlib
import hmac
import logging
import os
import time
from datetime import timedelta

import aiohttp
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
GATE_URL = os.environ.get("ALTGUARD_GATE_URL", "").rstrip("/")
SECRET = os.environ.get("ALTGUARD_SECRET", "")
# Honour a clean, high-confidence link-open instead of kicking. 0 disables and
# the prune goes back to kicking purely on the clock.
SPARE_CLEAN = os.environ.get("PRUNE_SPARE_CLEAN", "1") != "0"
# What to do with a spared member at the 72h line:
#   review  — leave them quarantined, ask a mod (the original behaviour)
#   release — auto-approve the verification and let them in
# The owner's call (2026-07-26): a clean-scoring open is real evidence of a real
# device on a clean network, and the accounts the gate exists to stop (alts,
# device twins, Tor/VPN exits, evaders) can't reach a clean score in the first
# place — is_clean_pass() requires no device match, no spoof, clean environment,
# geo trust, sub-trigger fraud and high timing. Auto-release trades the one thing
# a precapture can't give us — an OAuth binding proving the opener IS the target —
# for the ~40% of genuine joiners who stall at Discord's authorize screen.
# Watchlisted accounts are excluded and always fall back to review: they're the
# population with both a motive and a history of working the gate sideways, and a
# forwarded link is the one attack this path is actually open to.
SPARE_ACTION = os.environ.get("PRUNE_SPARE_ACTION", "review").strip().lower()
# Age band to stamp on an auto-approved member, who never reached the verify
# page's age picker and would otherwise land with no band at all. A key from
# ALTGUARD_AGE_ROLES ("13-15" … "28+"); empty = leave them unlabelled.
#
# Defaulting DOWN is the safe direction: the cost of mislabelling an adult as a
# minor is a wrong badge they can fix themselves, while the cost of the reverse
# is a minor sitting in the server labelled as an adult. Only applied when the
# member has no band already — a returning member's restored pick wins.
DEFAULT_AGE = os.environ.get("PRUNE_DEFAULT_AGE", "").strip()
# where members can correct the band themselves
ROLES_CHANNEL_ID = _env_int("ALTGUARD_ROLES_CHANNEL_ID", 1355902892883706066)

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

    async def _clean_passes(self) -> dict:
        """{uid: row} for accounts whose latest HIGH-confidence link-open scored
        a clean pass at the gate.

        These are people who opened the verify link, let the trust page
        fingerprint them, and then stopped at the Discord login — usually
        because "log in with Discord" is exactly what phishing looks like. We
        hold real evidence about them, and kicking someone we've already scored
        clean is the one prune outcome that's purely destructive: it burns a
        genuine member AND throws away the device print.

        Fails CLOSED — if the gate is unreachable we return nothing, and the
        prune proceeds on the clock as it always did. A gate outage must not
        silently suspend enforcement.
        """
        if not (SPARE_CLEAN and GATE_URL and SECRET):
            return {}
        ts = str(time.time())
        sig = hmac.new(SECRET.encode(), ts.encode(), hashlib.sha256).hexdigest()
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(f"{GATE_URL}/api/clean-passes",
                                 headers={"X-AltGuard-TS": ts, "X-AltGuard-Auth": sig},
                                 timeout=aiohttp.ClientTimeout(total=10)) as r:
                    if r.status != 200:
                        log.warning("clean-pass lookup failed: HTTP %s", r.status)
                        return {}
                    rows = (await r.json()).get("candidates", [])
        except Exception as e:
            log.warning("clean-pass lookup failed: %s", e)
            return {}
        return {str(x["target_uid"]): x for x in rows}

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

        # Stay of execution: anyone the gate scored clean off a high-confidence
        # link-open is pulled out of the kick list. With PRUNE_SPARE_ACTION=release
        # their verification is auto-approved here; otherwise they stay quarantined
        # and a mod decides. Members with low or no timing confidence (including
        # everyone who never opened the link) are untouched by this and get kicked
        # exactly as before.
        clean = await self._clean_passes()
        spared = [m for m in candidates if str(m.id) in clean]
        candidates = [m for m in candidates if str(m.id) not in clean]
        for m in spared:
            row = clean[str(m.id)]
            if SPARE_ACTION == "release" and not qstore.is_watched(m.id):
                await self._auto_release(guild, m, row)
                await asyncio.sleep(_PACE)
                continue
            first = qstore.record_spared(m.id, row.get("scored_verdict"), row.get("scored_risk"))
            if first:
                await self._spared_alert(guild, m, row)

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

    # --------------------------------------------------------- auto-approve
    async def _auto_release(self, guild, member, row):
        """Approve the verification at the 72h line off a clean link-open.

        Delegates to the AltGuard cog so a released member goes through the exact
        same path as `/altguard-release` — stored roles restored, rejoin roles
        re-applied, defaults granted, quarantine dropped — and gets marked
        `cleared`, which keeps their device a live detector without ever
        re-flagging them. No age role is granted (the age pick lives on the
        verify page they never reached), so they land with the age panel in
        #roles as their path to one.
        """
        ag = self.bot.get_cog("AltGuard")
        if not ag:
            log.warning("auto-release: AltGuard cog not loaded, falling back to review")
            if qstore.record_spared(member.id, row.get("scored_verdict"), row.get("scored_risk")):
                await self._spared_alert(guild, member, row)
            return
        try:
            ok, restored = await ag._release(member)
        except discord.HTTPException as e:
            log.warning("auto-release: %s failed: %s", member.id, e)
            return
        qstore.clear(member.id, f"auto-approved: clean link-open at {HOURS}h")
        qstore.set_status(member.id, "released")
        # they never saw the age picker, so stamp the default band — unless a real
        # one came back with them in the restore
        aged = None
        if ok and DEFAULT_AGE and not ag._has_age_role(member):
            await ag._apply_age_role(guild, member, {"age": DEFAULT_AGE})
            aged = DEFAULT_AGE
        if ok:
            fix = (f"\n\nYou've been listed as **{aged}** for now, since you never got to the age "
                   f"question — if that's not right, set your own in <#{ROLES_CHANNEL_ID}>."
                   if aged else "")
            try:
                await member.send(
                    f"You're in — verification for **{guild.name}** has been approved. "
                    f"You never finished the Discord login step, but we could see enough "
                    f"from your first visit to clear you. Welcome in!{fix}"
                )
            except discord.HTTPException:
                pass
        await self._released_alert(guild, member, row, restored, ok, aged)

    async def _released_alert(self, guild, member, row, restored, ok, aged=None):
        ch = self._modlog()
        if not ch:
            return
        roles = ", ".join(r.mention for r in restored) if restored else "no stored roles"
        e = discord.Embed(
            title="✅ Auto-approved at the prune line — clean link-open",
            color=0x3BA55D,
            description=(
                f"{member.mention} (`{member.id}`) passed **{HOURS}h** without verifying. Instead of "
                f"{ACTION}ing them, the gate's score on their link-open was honoured and their "
                f"verification is **approved**.\n\n"
                f"They opened the verify link, let the page fingerprint them, and stopped at the "
                f"Discord login — the step that looks like phishing."
            ),
        )
        e.add_field(name="⚖️ Replayed verdict",
                    value=f"✅ **PASS** · risk **{row.get('scored_risk', 0)}**", inline=True)
        e.add_field(name="🕒 Timing", value=(row.get("timing") or "—")[:64], inline=True)
        e.add_field(name="🧬 Best device match",
                    value=f"{row.get('top_pct', 0)}% (below alt threshold)", inline=True)
        e.add_field(name="Roles restored", value=roles[:1024], inline=False)
        if aged:
            e.add_field(
                name="🎂 Age band",
                value=(f"Defaulted to **{aged}** — they never reached the age picker. "
                       f"They've been told to correct it in <#{ROLES_CHANNEL_ID}>."),
                inline=False)
        elif DEFAULT_AGE:
            e.add_field(name="🎂 Age band",
                        value="Kept their existing band (not overwritten).", inline=False)
        if not ok:
            e.add_field(name="⚠️ Partial",
                        value="Couldn't fully restore roles — check my perms/role order.", inline=False)
        e.add_field(name="Undo",
                    value=f"`/altguard-check user_id:{member.id} quarantine:True` puts them back.",
                    inline=False)
        e.set_footer(text="No OAuth binding — attribution rests on the timing signal, not a login")
        try:
            await ch.send(embed=e)
        except discord.Forbidden:
            pass

    # ------------------------------------------------------------- reporting
    async def _spared_alert(self, guild, member, row):
        """Ask a human to finish the job the clock would have finished badly."""
        ch = self._modlog()
        if not ch:
            return
        risk = row.get("scored_risk", 0)
        e = discord.Embed(
            title="🛟 Prune held off — clean link-open on file",
            color=0x3BA55D,
            description=(
                f"{member.mention} (`{member.id}`) passed **{HOURS}h** without verifying, so the "
                f"prune would normally {ACTION} them. It didn't: they opened their verify link, let "
                f"the page fingerprint them, and stopped at the Discord login — and the gate scored "
                f"that open **clean**.\n\n"
                f"They are **still quarantined**. Nothing was released."
            ),
        )
        e.add_field(name="⚖️ Replayed verdict",
                    value=f"✅ **PASS** · risk **{risk}**", inline=True)
        e.add_field(name="🕒 Timing confidence", value=(row.get("timing") or "—")[:1024], inline=False)
        e.add_field(
            name="Your call",
            value=(f"`/altguard-release {member.id}` to let them in, or leave them — "
                   f"they'll stay quarantined and won't be re-flagged."),
            inline=False)
        e.set_footer(text="No OAuth binding — attribution rests on the timing signal, not a login")
        try:
            await ch.send(embed=e)
        except discord.Forbidden:
            pass

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
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def prune_status(self, interaction: discord.Interaction):
        guild = self.bot.get_guild(GUILD_ID)
        candidates = self._candidates(guild) if guild else []
        e = discord.Embed(title="🧹 Verify-prune", color=0x5B8CFF)
        e.add_field(name="Mode", value="**ENFORCE**" if ENFORCE else "**shadow** (alert-only)", inline=True)
        e.add_field(name="Action", value=ACTION, inline=True)
        e.add_field(name="Grace", value=f"{HOURS}h", inline=True)
        e.add_field(name="Interval", value=f"{INTERVAL_MIN}m (cap {MAX_PER_CYCLE}/cycle)", inline=True)
        if SPARE_CLEAN:
            spare = ("**auto-approve** (clean link-open → released)"
                     if SPARE_ACTION == "release" else "hold for mod review")
            if SPARE_ACTION == "release" and DEFAULT_AGE:
                spare += f"\n-# age defaults to **{DEFAULT_AGE}**"
        else:
            spare = "off (kick purely on the clock)"
        e.add_field(name="Clean link-open", value=spare, inline=True)
        e.add_field(name="Last sweep",
                    value=(f"<t:{int(self.last_run)}:R>" if self.last_run else "not yet"), inline=True)
        names = "\n".join(f"• {m.mention} — {self._ago(m)}" for m in candidates[:15]) or "none"
        extra = f"\n…and {len(candidates) - 15} more" if len(candidates) > 15 else ""
        e.add_field(name=f"Overdue now ({len(candidates)})", value=names + extra, inline=False)
        await interaction.response.send_message(embed=e, ephemeral=True)

    @app_commands.command(name="prune-run",
                          description="Run the verify-prune sweep right now (admin).")
    @app_commands.default_permissions(administrator=True)
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
