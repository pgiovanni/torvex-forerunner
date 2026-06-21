"""recon_watch — surfaces reconnaissance against the bot's two surfaces.

Profiling the bot leaves a different trace than normal use. Two sensors, one
#modlog feed:

  (1) Discord slash-command probing — a non-privileged user tripping many
      DISTINCT permission-gated commands in a short window (CheckFailure spray).
      Captured via a global tree.on_error hook, which discord.py calls for EVERY
      app-command error even when a command has its own local handler.

  (2) Gate web probing — polls the AltGuard gate's /api/recon feed. Only the two
      TARGETED signals page: unauthorized bot-API hits (failed HMAC from off-box)
      and invalid-token fuzzing on /v/ or /api/fp — both require knowing the bot's
      own surface, so a generic botnet can't trip them. path_scan (wso.php/
      wp-admin/.env scanners = internet background radiation) is log-only.

Shadow-first, exactly like anti-nuke: alerts only. RECON_ENFORCE=1 adds a 10-min
timeout for a Discord sprayer (the gate side stays alert-only — IP bans belong to
fail2ban/nginx, not the bot). Owner / admins / bots / RECON_WHITELIST are exempt.

Reuses ALTGUARD_GUILD_ID / ALTGUARD_MODLOG_CHANNEL_ID / ALTGUARD_SECRET /
ALTGUARD_GATE_URL. Tunables:
    RECON_ENFORCE (0)            RECON_ALERT_COOLDOWN (1800s)
    RECON_DISTINCT_CMDS (4)      RECON_CMD_WINDOW (600s)
    RECON_GATE_WINDOW (900s)     RECON_GATE_API_ALERT (3)
    RECON_GATE_TOKEN_ALERT (8)   RECON_GATE_SCAN_ALERT (12)
    RECON_WHITELIST ("" — space/comma-separated user ids and/or IPs)
"""
import hashlib
import hmac
import logging
import os
import time
from collections import defaultdict, deque
from datetime import timedelta

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

log = logging.getLogger("recon_watch")


def _env_int(name, default=0):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


GUILD_ID = _env_int("ALTGUARD_GUILD_ID")
MODLOG_CHANNEL_ID = _env_int("ALTGUARD_MODLOG_CHANNEL_ID")
SECRET = os.environ.get("ALTGUARD_SECRET", "")
GATE_URL = os.environ.get("ALTGUARD_GATE_URL", "").rstrip("/")

ENFORCE = os.environ.get("RECON_ENFORCE", "0") != "0"
CMD_DISTINCT = _env_int("RECON_DISTINCT_CMDS", 4)
CMD_WINDOW = _env_int("RECON_CMD_WINDOW", 600)
GATE_WINDOW = _env_int("RECON_GATE_WINDOW", 900)
GATE_THRESHOLDS = {
    "api_unauth": _env_int("RECON_GATE_API_ALERT", 2),
    "bad_token": _env_int("RECON_GATE_TOKEN_ALERT", 4),
}
# Only these alert — they require knowledge of the bot's own surface, so a
# generic botnet can't trip them. path_scan (wso.php/wp-admin/.env — internet
# background radiation hitting every public IP) is LOG-ONLY: stored for
# forensics, never paged. Promote the targeted signals, ignore the noise.
ALERT_KINDS = {"api_unauth", "bad_token"}
ALERT_COOLDOWN = _env_int("RECON_ALERT_COOLDOWN", 1800)
WHITELIST = {x for x in os.environ.get("RECON_WHITELIST", "").replace(",", " ").split() if x.strip()}

_KIND_LABEL = {
    "api_unauth": "unauthorized bot-API hits",
    "bad_token": "invalid-token fuzzing",
    "path_scan": "scanner path probes",
}


class ReconWatch(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.session: aiohttp.ClientSession | None = None
        self._orig_on_error = None
        # Discord: uid -> deque[(command_name, ts)] of permission denials
        self.denials = defaultdict(deque)
        # gate: (ip, kind) -> deque[{ts, route, ja4, ua}] within GATE_WINDOW
        self.gate_events = defaultdict(deque)
        # alert de-dupe: key -> last alert ts
        self.alerted = {}

    async def cog_load(self):
        self.session = aiohttp.ClientSession()
        # chain in front of whatever tree error handler is already set
        self._orig_on_error = self.bot.tree.on_error
        self.bot.tree.on_error = self._on_tree_error
        if GATE_URL and SECRET:
            self.poll_gate.start()

    async def cog_unload(self):
        self.poll_gate.cancel()
        if self._orig_on_error is not None:
            self.bot.tree.on_error = self._orig_on_error
        if self.session:
            await self.session.close()

    # ------------------------------------------------------------- helpers
    def _hmac(self) -> dict:
        ts = str(time.time())
        sig = hmac.new(SECRET.encode(), ts.encode(), hashlib.sha256).hexdigest()
        return {"X-AltGuard-TS": ts, "X-AltGuard-Auth": sig}

    def _exempt(self, user) -> bool:
        if str(user.id) in WHITELIST or getattr(user, "bot", False):
            return True
        g = getattr(user, "guild", None)
        if g is not None and g.owner_id == user.id:
            return True
        perms = getattr(user, "guild_permissions", None)
        if perms is not None and (perms.administrator or perms.manage_guild):
            return True
        return False

    def _cooling(self, key, now) -> bool:
        """True if this subject already alerted within the cooldown (and we should
        stay quiet). Otherwise stamps now and returns False."""
        if now - self.alerted.get(key, 0) < ALERT_COOLDOWN:
            return True
        self.alerted[key] = now
        return False

    def _modlog(self):
        return self.bot.get_channel(MODLOG_CHANNEL_ID)

    @property
    def _tag(self) -> str:
        return "🛰️ Recon" if ENFORCE else "🛰️ Recon (shadow)"

    # ------------------------------------------------- (1) Discord command spray
    async def _on_tree_error(self, interaction: discord.Interaction, error):
        try:
            await self._note_denial(interaction, error)
        except Exception:
            log.exception("recon: denial note failed")
        # never swallow — hand back to the prior handler (default logs it)
        if self._orig_on_error is not None:
            await self._orig_on_error(interaction, error)

    async def _note_denial(self, interaction: discord.Interaction, error):
        if not isinstance(error, app_commands.CheckFailure):
            return
        if not interaction.guild or interaction.guild.id != GUILD_ID:
            return
        user = interaction.user
        if self._exempt(user):
            return
        cmd = interaction.command.qualified_name if interaction.command else "?"
        now = time.time()
        dq = self.denials[user.id]
        dq.append((cmd, now))
        while dq and now - dq[0][1] > CMD_WINDOW:
            dq.popleft()
        distinct = {c for c, _ in dq}
        if len(distinct) >= CMD_DISTINCT and not self._cooling(("cmd", user.id), now):
            await self._alert_cmd(user, distinct, len(dq))
            if ENFORCE and isinstance(user, discord.Member):
                try:
                    await user.timeout(timedelta(minutes=10),
                                       reason="recon_watch: command-probing spray")
                except discord.HTTPException:
                    pass

    async def _alert_cmd(self, user, distinct_cmds, total):
        ch = self._modlog()
        if not ch:
            return
        cmds = ", ".join(f"`/{c}`" for c in sorted(distinct_cmds))
        mins = CMD_WINDOW // 60
        e = discord.Embed(
            title=f"{self._tag} — command probing",
            description=(
                f"{user.mention} (`{user.id}`) tripped **{len(distinct_cmds)} gated "
                f"commands** ({total} denials) in ~{mins}m — looks like someone mapping "
                f"what the bot can do."
            ),
            color=0xFFB020,
        )
        e.add_field(name="Denied commands", value=cmds[:1024], inline=False)
        if ENFORCE:
            e.set_footer(text="enforce on — applied a 10m timeout")
        try:
            await ch.send(embed=e)
        except discord.HTTPException:
            pass

    # ---------------------------------------------------- (2) gate web probing
    @tasks.loop(seconds=30)
    async def poll_gate(self):
        if not self.session:
            return
        try:
            async with self.session.get(f"{GATE_URL}/api/recon",
                                        headers=self._hmac(), timeout=10) as r:
                if r.status != 200:
                    return
                events = (await r.json()).get("events", [])
        except Exception as e:
            log.warning("recon: gate poll failed: %s", e)
            return
        if not events:
            return

        ids = []
        for ev in events:
            if ev.get("id") is not None:
                ids.append(ev["id"])
            ip = ev.get("ip") or "?"
            kind = ev.get("kind") or "?"
            if ip in WHITELIST:
                continue
            dq = self.gate_events[(ip, kind)]
            dq.append({"ts": ev.get("ts") or time.time(), "route": ev.get("route") or "",
                       "ja4": ev.get("ja4") or "", "ua": ev.get("ua") or ""})

        # ack everything we pulled so it isn't re-served
        if ids:
            try:
                await self.session.post(f"{GATE_URL}/api/recon/ack",
                                        headers=self._hmac(), json={"ids": ids}, timeout=10)
            except Exception as e:
                log.warning("recon: gate ack failed: %s", e)

        now = time.time()
        for key, dq in list(self.gate_events.items()):
            while dq and now - dq[0]["ts"] > GATE_WINDOW:
                dq.popleft()
            if not dq:
                del self.gate_events[key]
                continue
            ip, kind = key
            if kind not in ALERT_KINDS:
                continue  # path_scan = generic botnet noise → log-only, never page
            if len(dq) >= GATE_THRESHOLDS.get(kind, 1 << 30) and not self._cooling(("gate", ip, kind), now):
                await self._alert_gate(ip, kind, dq)

    @poll_gate.before_loop
    async def _before_poll(self):
        await self.bot.wait_until_ready()

    async def _alert_gate(self, ip, kind, dq):
        ch = self._modlog()
        if not ch:
            return
        last = dq[-1]
        mins = GATE_WINDOW // 60
        e = discord.Embed(
            title=f"{self._tag} — targeted gate probing",
            description=(
                f"**{len(dq)}× {_KIND_LABEL.get(kind, kind)}** from `{ip}` in ~{mins}m — "
                f"this hits the bot's own surface, so it's not a generic scanner: "
                f"someone who knows what this gate is, is poking it."
            ),
            color=0xFF6B6B,
        )
        if last.get("route"):
            e.add_field(name="Last route", value=f"`{last['route'][:200]}`", inline=False)
        if last.get("ja4"):
            e.add_field(name="JA4", value=f"`{last['ja4']}`", inline=True)
        if last.get("ua"):
            e.add_field(name="User-Agent", value=last["ua"][:200], inline=False)
        e.set_footer(text="gate side is alert-only — block at fail2ban/nginx if needed")
        try:
            await ch.send(embed=e)
        except discord.HTTPException:
            pass

    # ------------------------------------------------------------- status cmd
    @app_commands.command(name="recon-status",
                          description="Recent reconnaissance signals against the bot and gate.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def recon_status(self, interaction: discord.Interaction):
        now = time.time()
        # live Discord sprayers (within window)
        cmd_lines = []
        for uid, dq in self.denials.items():
            recent = [c for c, t in dq if now - t <= CMD_WINDOW]
            if recent:
                cmd_lines.append(f"<@{uid}> — {len(set(recent))} distinct ({len(recent)} denials)")
        # live gate IPs (within window)
        gate_lines = []
        for (ip, kind), dq in self.gate_events.items():
            n = sum(1 for ev in dq if now - ev["ts"] <= GATE_WINDOW)
            if n:
                gate_lines.append(f"`{ip}` — {n}× {kind}")

        e = discord.Embed(title="🛰️ Recon watch", color=0x5B8CFF)
        e.add_field(
            name="Mode",
            value=("**ENFORCE**" if ENFORCE else "**shadow** (alert-only)"),
            inline=True,
        )
        e.add_field(
            name="Thresholds",
            value=(f"cmd: {CMD_DISTINCT} distinct / {CMD_WINDOW // 60}m\n"
                   f"gate api: {GATE_THRESHOLDS['api_unauth']} · token: "
                   f"{GATE_THRESHOLDS['bad_token']} · scan: {GATE_THRESHOLDS['path_scan']} "
                   f"/ {GATE_WINDOW // 60}m"),
            inline=True,
        )
        e.add_field(name="Command probers (live)",
                    value=("\n".join(cmd_lines[:10]) or "none"), inline=False)
        e.add_field(name="Gate probers (live)",
                    value=("\n".join(gate_lines[:10]) or "none"), inline=False)
        await interaction.response.send_message(embed=e, ephemeral=True)

    @recon_status.error
    async def _status_err(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "You need **Manage Server** to view recon status.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(ReconWatch(bot))
