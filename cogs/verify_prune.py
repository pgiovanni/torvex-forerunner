"""verify_prune — removes members who never finish verification.

A standing quarantine is a held door: an account that joins, gets quarantined-
on-join, and then just *sits* there forever is the cheapest way to keep a
foothold (and to wear down whoever's watching the gate). This closes that —
after a grace window an unverified member is DM'd a heads-up and then removed.

Every part of that is the server's own call, set on the dashboard (AltGuard →
"If they never verify") or with `/prune-config`:

    prune_enabled       off = nobody is ever auto-removed; held members just sit
    prune_hours         the clock, in hours — 2 or 72 or 720, whatever suits
    prune_action        kick (they can rejoin and try again) or ban
    prune_enforce       off = name them in the log and act on nobody
    prune_spare_clean   honour a clean link-open instead of removing
    prune_spare_action  review (stay held, ask a mod) or release (auto-approve)
    prune_dm            what they're told; blank uses the built-in wording
    prune_max_per_cycle removals per sweep — a ceiling on any single mistake

Scope is deliberately narrow: ONLY members who currently hold that server's
quarantine role. Members who verified (role removed) or who predate the gate
(never had the role) are never touched — so this can't mass-prune an existing
server, however the settings are turned.

The clock starts when they were HELD, not when they joined, so a long-standing
member quarantined today gets the full window rather than an instant kick.

Global (stay in env — shared gate infrastructure, not per-server policy):
    ALTGUARD_GATE_URL / ALTGUARD_SECRET      the clean-pass lookup
    PRUNE_INTERVAL_MIN (60)                  how often the sweep runs
    PRUNE_WHITELIST ("")                     uids never pruned in any server
"""
import asyncio
import hashlib
import hmac
import logging
import os
import time

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

import quarantine_store as qstore
from utils import quarantine as qt
from utils.security_config import get_config, set_config
from cogs.altguard import ALMOST_ROLE_ID, _precap_conn

log = logging.getLogger("verify_prune")


def _env_int(name, default=0):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# Legacy single-guild wiring — now only used to seed that guild's config once
# (see _seed_legacy) and to decide where the AltGuard cog's release path works.
GUILD_ID = _env_int("ALTGUARD_GUILD_ID")
GATE_URL = os.environ.get("ALTGUARD_GATE_URL", "").rstrip("/")
SECRET = os.environ.get("ALTGUARD_SECRET", "")
DEFAULT_AGE = os.environ.get("ALTGUARD_DEFAULT_AGE", "").strip()
ROLES_CHANNEL_ID = _env_int("ALTGUARD_ROLES_CHANNEL_ID", 1355902892883706066)

INTERVAL_MIN = max(5, _env_int("PRUNE_INTERVAL_MIN", 60))
WHITELIST = {x for x in os.environ.get("PRUNE_WHITELIST", "").replace(",", " ").split() if x.strip()}
DM_DEFAULT = (
    "Hey — you've been removed from **{guild}** because verification wasn't "
    "completed in time (sorry, you took too long!). No hard feelings: you're "
    "welcome to rejoin and verify whenever you're ready."
)
# seconds between removals — keeps us under rate limits and well clear of any
# mass-action heuristic (the bot is self-exempt from anti-nuke, but be tidy)
_PACE = 2.0
# Sanity rails on whatever a server types in. The floor exists because a window
# under an hour would remove people who are simply asleep, or who opened the
# link and walked away mid-check.
MIN_HOURS, MAX_HOURS = 1, 8760


class Settings:
    """One server's prune policy, resolved from its config."""

    __slots__ = ("enabled", "altguard_on", "enforce", "hours", "action", "max_per_cycle",
                 "spare_clean", "spare_action", "dm", "qrole_id", "modlog_id",
                 "whitelist")

    def __init__(self, guild):
        cfg = get_config(guild.id)
        self.enabled = bool(cfg.get("prune_enabled"))
        # The prune is the VERIFICATION clock. With AltGuard off there is no
        # verification to fail — but LinkGuard and anti-nuke still put people in
        # the same quarantine role, and removing one of those for "not verifying"
        # would be flatly wrong. So the gate has to be on for the clock to run.
        self.altguard_on = bool(cfg.get("altguard_enabled"))
        self.enforce = bool(cfg.get("prune_enforce"))
        self.hours = max(MIN_HOURS, min(MAX_HOURS, _int(cfg.get("prune_hours"), 72)))
        self.action = "ban" if str(cfg.get("prune_action") or "").lower() == "ban" else "kick"
        self.max_per_cycle = max(1, _int(cfg.get("prune_max_per_cycle"), 25))
        self.spare_clean = bool(cfg.get("prune_spare_clean"))
        # Auto-release runs through the AltGuard cog, which is still wired to the
        # one legacy guild. Anywhere else the honest answer is "hold for review"
        # rather than a release that would half-happen.
        want = str(cfg.get("prune_spare_action") or "review").lower()
        self.spare_action = "release" if (want == "release" and guild.id == GUILD_ID) else "review"
        self.dm = (cfg.get("prune_dm") or "").strip() or DM_DEFAULT
        self.qrole_id = qt.role_id_for(guild)
        self.modlog_id = _int(cfg.get("modlog_channel_id"), 0)
        self.whitelist = WHITELIST | {str(x) for x in (cfg.get("whitelist") or [])}

    @property
    def runnable(self) -> bool:
        """Enabled AND actually pointed at something. A prune with no quarantine
        role would iterate an empty set forever; better to skip it outright."""
        return self.enabled and self.altguard_on and bool(self.qrole_id)

    def render_dm(self, guild) -> str:
        """A server writes this text, so a stray {brace} must not break the DM
        (and silently turn a warned kick into an unwarned one)."""
        try:
            return self.dm.format(guild=guild.name, hours=self.hours)
        except (KeyError, IndexError, ValueError):
            return self.dm


class VerifyPrune(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.last_run = 0.0
        self.last_pruned = 0

    async def cog_load(self):
        self.sweep.start()

    async def cog_unload(self):
        self.sweep.cancel()

    # ------------------------------------------------------------- migration
    @staticmethod
    def _seed_legacy():
        """One-time PRUNE_* env → config copy for the original guild.

        Without this the refactor would silently switch that server's prune off
        (config default is disabled), which is a protection gap dressed up as a
        no-op. Runs once; after that the dashboard is the only authority.
        """
        if not GUILD_ID:
            return
        cfg = get_config(GUILD_ID)
        if cfg.get("prune_seeded"):
            return
        hours = max(MIN_HOURS, min(MAX_HOURS, _env_int("PRUNE_HOURS", 72)))
        action = "ban" if os.environ.get("PRUNE_ACTION", "kick").strip().lower() == "ban" else "kick"
        spare = "release" if os.environ.get("PRUNE_SPARE_ACTION", "review").strip().lower() == "release" else "review"
        set_config(
            GUILD_ID,
            prune_seeded=1,
            # it was running before this change iff the env had it wired up
            prune_enabled=1 if _env_int("ALTGUARD_QUARANTINE_ROLE_ID") else 0,
            prune_enforce=1 if os.environ.get("PRUNE_ENFORCE", "0") != "0" else 0,
            prune_hours=hours,
            prune_action=action,
            prune_max_per_cycle=_env_int("PRUNE_MAX_PER_CYCLE", 25),
            prune_spare_clean=0 if os.environ.get("PRUNE_SPARE_CLEAN", "1") == "0" else 1,
            prune_spare_action=spare,
            prune_dm=os.environ.get("PRUNE_DM", "") or "",
        )
        log.info("verify-prune: seeded guild %s from legacy PRUNE_* env", GUILD_ID)

    # ------------------------------------------------------------- helpers
    def _modlog(self, guild, st):
        return guild.get_channel(st.modlog_id) if st.modlog_id else None

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
        if not (GATE_URL and SECRET):
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

    def _exempt(self, member: discord.Member, st: Settings) -> bool:
        if member.bot or str(member.id) in st.whitelist:
            return True
        # "Almost Verified" holders are OFF the clock entirely (Paul, 2026-07-26).
        # They opened their link, scored a clean high-timing-confidence pass, and
        # can talk in #verify — so the answer to "they haven't finished" is to
        # remind them there, not to remove them. Note this also means the
        # auto-approve never fires for them: same population, and the point is
        # that they finish verification rather than be let in without it.
        # Revoking the role puts them straight back on the clock.
        if ALMOST_ROLE_ID and any(r.id == ALMOST_ROLE_ID for r in member.roles):
            return True
        if member.guild.owner_id == member.id:
            return True
        perms = member.guild_permissions
        if perms.administrator or perms.manage_guild:
            return True
        return False

    def _candidates(self, guild: discord.Guild, st: Settings):
        """Members holding the quarantine role whose clock ran out and who have
        not passed verification."""
        qrole = guild.get_role(st.qrole_id) if st.qrole_id else None
        if not qrole:
            return []
        cutoff = time.time() - st.hours * 3600
        out = []
        for m in qrole.members:
            if self._exempt(m, st):
                continue
            started = self._held_since(m)
            if started is None or started > cutoff:
                continue  # clock starts at QUARANTINE time, not join — a long-time
                          # member quarantined today gets a fresh window, not an instant kick
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
        # Join-time fallback is HOME-GUILD ONLY. Pointed at another server's
        # long-standing "unverified" role, join time makes every member of that
        # pen instantly >72h overdue with no verification record — a mass kick
        # on day one. Elsewhere: no real hold record, no clock.
        if ts is None and m.guild.id == GUILD_ID and m.joined_at:
            ts = m.joined_at.timestamp()
        return ts

    # ------------------------------------------------------------- the sweep
    @tasks.loop(minutes=INTERVAL_MIN)
    async def sweep(self):
        self.last_run = time.time()
        total = 0
        # Only fetched if a guild actually needs it — one gate call per sweep,
        # shared across servers, and skipped entirely when nobody is overdue.
        clean = None
        for guild in list(self.bot.guilds):
            try:
                st = Settings(guild)
                if not st.runnable:
                    continue
                candidates = self._candidates(guild, st)
                if not candidates:
                    continue
                if not st.enforce:
                    await self._shadow_report(guild, st, candidates)
                    continue
                guild_clean = {}
                if st.spare_clean:
                    if clean is None:          # fetched at most once per sweep
                        clean = await self._clean_passes()
                    guild_clean = clean
                total += await self._enforce(guild, st, candidates, guild_clean)
            except Exception:
                log.exception("verify-prune sweep failed for guild %s", guild.id)
        self.last_pruned = total

    async def _enforce(self, guild, st, candidates, clean):
        # Stay of execution: anyone the gate scored clean off a high-confidence
        # link-open is pulled out of the kick list. With prune_spare_action=release
        # their verification is auto-approved here; otherwise they stay quarantined
        # and a mod decides. Members with low or no timing confidence (including
        # everyone who never opened the link) are untouched by this and get removed
        # exactly as before.
        spared = [m for m in candidates if str(m.id) in clean]
        candidates = [m for m in candidates if str(m.id) not in clean]
        for m in spared:
            row = clean[str(m.id)]
            if st.spare_action == "release" and not qstore.is_watched(m.id):
                await self._auto_release(guild, st, m, row)
                await asyncio.sleep(_PACE)
                continue
            if qstore.record_spared(m.id, row.get("scored_verdict"), row.get("scored_risk")):
                await self._spared_alert(guild, st, m, row)

        dm_text = st.render_dm(guild)
        pruned, dm_failed, act_failed = [], 0, []
        for m in candidates[:st.max_per_cycle]:
            # DM first — must happen while we still share the server
            if dm_text:
                try:
                    await m.send(dm_text)
                except discord.HTTPException:
                    dm_failed += 1
            reason = f"AltGuard: did not verify within {st.hours}h"
            try:
                if st.action == "ban":
                    await m.ban(reason=reason, delete_message_seconds=0)
                    qstore.set_status(m.id, "banned")
                else:
                    await m.kick(reason=reason)
                    qstore.set_status(m.id, "pruned")
                pruned.append(m)
            except discord.Forbidden:
                act_failed.append(m)
                log.warning("prune: lack permission to %s %s", st.action, m.id)
            except discord.HTTPException as e:
                act_failed.append(m)
                log.warning("prune: %s %s failed: %s", st.action, m.id, e)
            await asyncio.sleep(_PACE)

        await self._enforce_report(guild, st, candidates, pruned, dm_failed, act_failed)
        return len(pruned)

    @sweep.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()
        try:
            self._seed_legacy()
        except Exception:
            log.exception("verify-prune: legacy seed failed")
        await asyncio.sleep(45)  # let the member cache chunk before first sweep

    # --------------------------------------------------------- auto-approve
    async def _auto_release(self, guild, st, member, row):
        """Approve the verification at the prune line off a clean link-open.

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
                await self._spared_alert(guild, st, member, row)
            return
        try:
            # _release owns the age default now (ALTGUARD_DEFAULT_AGE) so every
            # release path — manual, verdict-pass, auto-approve — behaves alike
            ok, restored, aged = await ag._release(member)
        except discord.HTTPException as e:
            log.warning("auto-release: %s failed: %s", member.id, e)
            return
        qstore.clear(member.id, f"auto-approved: clean link-open at {st.hours}h")
        qstore.set_status(member.id, "released")
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
        await self._released_alert(guild, st, member, row, restored, ok, aged)

    async def _released_alert(self, guild, st, member, row, restored, ok, aged=None):
        ch = self._modlog(guild, st)
        if not ch:
            return
        roles = ", ".join(r.mention for r in restored) if restored else "no stored roles"
        e = discord.Embed(
            title="✅ Auto-approved at the prune line — clean link-open",
            color=0x3BA55D,
            description=(
                f"{member.mention} (`{member.id}`) passed **{st.hours}h** without verifying. Instead of "
                f"{st.action}ing them, the gate's score on their link-open was honoured and their "
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
        e.add_field(name="🌐 Connection", value=_precap_conn(row)[:1024], inline=False)
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
    async def _spared_alert(self, guild, st, member, row):
        """Ask a human to finish the job the clock would have finished badly."""
        ch = self._modlog(guild, st)
        if not ch:
            return
        risk = row.get("scored_risk", 0)
        e = discord.Embed(
            title="🛟 Prune held off — clean link-open on file",
            color=0x3BA55D,
            description=(
                f"{member.mention} (`{member.id}`) passed **{st.hours}h** without verifying, so the "
                f"prune would normally {st.action} them. It didn't: they opened their verify link, let "
                f"the page fingerprint them, and stopped at the Discord login — and the gate scored "
                f"that open **clean**.\n\n"
                f"They are **still quarantined**. Nothing was released."
            ),
        )
        e.add_field(name="⚖️ Replayed verdict",
                    value=f"✅ **PASS** · risk **{risk}**", inline=True)
        e.add_field(name="🕒 Timing confidence", value=(row.get("timing") or "—")[:1024], inline=False)
        e.add_field(name="🌐 Connection", value=_precap_conn(row)[:1024], inline=False)
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

    async def _shadow_report(self, guild, st, candidates):
        ch = self._modlog(guild, st)
        if not ch:
            return
        names = "\n".join(f"• {m.mention} `{m.id}` — {self._ago(m)}" for m in candidates[:25])
        extra = f"\n…and {len(candidates) - 25} more" if len(candidates) > 25 else ""
        e = discord.Embed(
            title=f"🧹 Verify-prune (shadow) — {len(candidates)} would be {st.action}ed",
            description=(
                f"These hold the quarantine role and were held over **{st.hours}h** ago "
                f"without verifying. **No action taken** (shadow mode).\n\n{names}{extra}"
            ),
            color=0xFFB020,
        )
        e.set_footer(text="Turn on 'Actually remove them' in the dashboard to act.")
        try:
            await ch.send(embed=e)
        except discord.HTTPException:
            pass

    async def _enforce_report(self, guild, st, candidates, pruned, dm_failed, act_failed):
        ch = self._modlog(guild, st)
        if not ch:
            return
        verb = "Banned" if st.action == "ban" else "Kicked"
        lines = "\n".join(f"• {m} `{m.id}`" for m in pruned[:25]) or "—"
        e = discord.Embed(
            title=f"🧹 Verify-prune — {verb.lower()} {len(pruned)} unverified",
            description=(
                f"Held the quarantine role for over **{st.hours}h** without "
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
            e.add_field(name="Deferred", value=f"{remaining} (cycle cap {st.max_per_cycle})", inline=True)
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
        guild = interaction.guild
        if not guild:
            await interaction.response.send_message("❌ Must be used in a server.", ephemeral=True)
            return
        st = Settings(guild)
        candidates = self._candidates(guild, st) if st.qrole_id else []
        e = discord.Embed(title="🧹 Verify-prune", color=0x5B8CFF)
        if not st.enabled:
            e.description = ("⚪ **Off** — held members are never auto-removed. They stay "
                             "quarantined until someone deals with them.")
        elif not st.altguard_on:
            e.description = ("⚪ **Not running** — it's switched on, but AltGuard is off here, "
                             "so there's no verification to fail. Anyone in the quarantine role "
                             "right now was put there by LinkGuard or anti-nuke, and removing "
                             "them for 'not verifying' would be wrong.")
        e.add_field(name="Mode",
                    value=("**ENFORCE**" if st.enforce else "**shadow** (alert-only)")
                    if st.runnable else "off", inline=True)
        e.add_field(name="Action", value=st.action, inline=True)
        e.add_field(name="Grace", value=f"{st.hours}h", inline=True)
        e.add_field(name="Sweep", value=f"every {INTERVAL_MIN}m (cap {st.max_per_cycle}/cycle)", inline=True)
        if st.spare_clean:
            spare = ("**auto-approve** (clean link-open → released)"
                     if st.spare_action == "release" else "hold for mod review")
            if st.spare_action == "release" and DEFAULT_AGE:
                spare += f"\n-# age defaults to **{DEFAULT_AGE}**"
        else:
            spare = "off (remove purely on the clock)"
        e.add_field(name="Clean link-open", value=spare, inline=True)
        e.add_field(name="Last sweep",
                    value=(f"<t:{int(self.last_run)}:R>" if self.last_run else "not yet"), inline=True)
        if not st.qrole_id:
            e.add_field(name="⚠️ Not set up",
                        value="No quarantine role here — run `/security setup`.", inline=False)
        names = "\n".join(f"• {m.mention} — {self._ago(m)}" for m in candidates[:15]) or "none"
        extra = f"\n…and {len(candidates) - 15} more" if len(candidates) > 15 else ""
        e.add_field(name=f"Overdue now ({len(candidates)})", value=names + extra, inline=False)
        e.set_footer(text="/prune-config to change it, or the AltGuard page on the dashboard")
        await interaction.response.send_message(embed=e, ephemeral=True)

    @app_commands.command(name="prune-config",
                          description="Set what happens to members who never verify (admin).")
    @app_commands.describe(
        enabled="Off = nobody is ever auto-removed; held members just stay held",
        hours="How long they get, in hours, from the moment they're held (1–8760)",
        action="Kick lets them rejoin and try again; ban doesn't",
        enforce="Off = only name them in the mod-log, remove nobody",
        spare_clean="Spare anyone the gate already scored clean off their link-open",
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="Kick — they can rejoin and verify", value="kick"),
        app_commands.Choice(name="Ban — permanent", value="ban"),
    ])
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def prune_config(self, interaction: discord.Interaction,
                           enabled: bool = None, hours: int = None,
                           action: app_commands.Choice[str] = None,
                           enforce: bool = None, spare_clean: bool = None):
        guild = interaction.guild
        if not guild:
            await interaction.response.send_message("❌ Must be used in a server.", ephemeral=True)
            return
        changes = {}
        if enabled is not None:
            changes["prune_enabled"] = 1 if enabled else 0
        if hours is not None:
            if not MIN_HOURS <= hours <= MAX_HOURS:
                await interaction.response.send_message(
                    f"❌ Give a window between **{MIN_HOURS}** and **{MAX_HOURS}** hours. "
                    f"Anything under an hour removes people who are simply asleep.", ephemeral=True)
                return
            changes["prune_hours"] = hours
        if action is not None:
            changes["prune_action"] = action.value
        if enforce is not None:
            changes["prune_enforce"] = 1 if enforce else 0
        if spare_clean is not None:
            changes["prune_spare_clean"] = 1 if spare_clean else 0
        if changes:
            set_config(guild.id, **changes)
        st = Settings(guild)
        if not changes:
            summary = "Nothing changed — here's what's set:"
        else:
            summary = "✅ Updated."
        if not st.enabled:
            state = "⚪ **off** — nobody is auto-removed"
        elif not st.altguard_on:
            state = ("⚪ **on, but idle** — AltGuard is off here, so there's no verification "
                     "to fail and nobody is removed")
        elif st.enforce:
            state = f"🔴 **{st.action}** after **{st.hours}h**"
        else:
            state = f"🟡 **shadow** — would {st.action} after **{st.hours}h**, acts on nobody"
        await interaction.response.send_message(
            f"{summary}\n{state}\n"
            f"-# Clean link-opens are {'spared' if st.spare_clean else 'not spared'}. "
            f"Full settings on the AltGuard page of the dashboard.", ephemeral=True)

    @app_commands.command(name="prune-run",
                          description="Run the verify-prune sweep right now (admin).")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def prune_run(self, interaction: discord.Interaction):
        guild = interaction.guild
        if not guild:
            await interaction.response.send_message("❌ Must be used in a server.", ephemeral=True)
            return
        st = Settings(guild)
        if not st.runnable:
            if not st.enabled:
                why = "it's switched **off** — `/prune-config enabled:True` turns it on"
            elif not st.altguard_on:
                why = "**AltGuard is off** here, so there's no verification to fail"
            else:
                why = "no **quarantine role** is set — run `/security setup`"
            await interaction.response.send_message(f"⚪ Nothing to run: {why}.", ephemeral=True)
            return
        where = (f"results post to <#{st.modlog_id}>." if st.modlog_id
                 else "⚠️ no mod-log channel is set, so there's nowhere to report to.")
        await interaction.response.send_message(
            f"Running a verify-prune sweep ({'enforce' if st.enforce else 'shadow'})… {where}",
            ephemeral=True)
        candidates = self._candidates(guild, st)
        if not candidates:
            return
        if not st.enforce:
            await self._shadow_report(guild, st, candidates)
            return
        clean = await self._clean_passes() if st.spare_clean else {}
        await self._enforce(guild, st, candidates, clean)

    @prune_status.error
    @prune_run.error
    @prune_config.error
    async def _err(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "You need **Administrator** for that.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(VerifyPrune(bot))
