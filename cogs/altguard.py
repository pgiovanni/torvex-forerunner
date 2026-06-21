"""AltGuard cog for peepos-reclaimer (discord.py 2.x).

Two modes, picked by ALTGUARD_QUARANTINE_ON_JOIN:

  Forced-gate (ALTGUARD_QUARANTINE_ON_JOIN=1):
    * EVERY human is quarantined the instant they join (access stripped) and
      immediately DMed a verify link
    * PASS  -> quarantine lifted automatically, roles restored, mod-log note
    * FAIL  -> they stay quarantined; alert + alt cascade as below
    * needs a #verify-style channel the quarantine role CAN see (so closed-DM
      members can still run /verify)

  Detect-only (ALTGUARD_QUARANTINE_ON_JOIN=0, the default):
    * members join with normal access; on join they're DMed a verify link
    * PASS  -> nothing happens, they keep their access
    * FAIL  -> STRIP their roles (stored for restore), apply quarantine role,
               post a mod-log alert, AND cascade to every fingerprint-matched alt

In both modes a false positive is fully reversible with /altguard-release
(re-adds the exact roles that were removed).

Load it with:  await bot.load_extension("altguard_cog")
(keep altguard_cog.py, tokens.py, quarantine_store.py together)

Required env vars:
    ALTGUARD_SECRET, ALTGUARD_GATE_URL, ALTGUARD_GUILD_ID,
    ALTGUARD_QUARANTINE_ROLE_ID, ALTGUARD_MODLOG_CHANNEL_ID
Optional:
    ALTGUARD_MIN_ACCOUNT_AGE_DAYS (default 7)
    ALTGUARD_DM_ON_JOIN (default 1)
    ALTGUARD_QUARANTINE_ON_JOIN (default 0)
"""
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
from tokens import make_token, pack

log = logging.getLogger("altguard")


def _env_int(name, default=0):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


SECRET = os.environ.get("ALTGUARD_SECRET", "")
GATE_URL = os.environ.get("ALTGUARD_GATE_URL", "").rstrip("/")
GUILD_ID = _env_int("ALTGUARD_GUILD_ID")
QUARANTINE_ROLE_ID = _env_int("ALTGUARD_QUARANTINE_ROLE_ID")
MODLOG_CHANNEL_ID = _env_int("ALTGUARD_MODLOG_CHANNEL_ID")
VERIFY_CHANNEL_ID = _env_int("ALTGUARD_VERIFY_CHANNEL_ID")
MIN_ACCOUNT_AGE_DAYS = _env_int("ALTGUARD_MIN_ACCOUNT_AGE_DAYS", 7)
# opt-out default roles auto-granted when a member gains access (at join if not
# gating, or on release after they verify). Replaces MEE6 autorole. Members can
# remove any they don't want.
DEFAULT_ROLE_IDS = [int(x) for x in os.environ.get("ALTGUARD_DEFAULT_ROLES", "").replace(",", " ").split() if x.strip().isdigit()]
DM_ON_JOIN = os.environ.get("ALTGUARD_DM_ON_JOIN", "1") != "0"
# Forced-gate mode: quarantine EVERY human the moment they join (strip access),
# DM them the link, and auto-release them on a PASS verdict. Off = detect-only.
QUARANTINE_ON_JOIN = os.environ.get("ALTGUARD_QUARANTINE_ON_JOIN", "0") != "0"
# When a verifier's device/GPU matches a BANNED account, auto-ban the new one too.
AUTOBAN_EVASION = os.environ.get("ALTGUARD_AUTOBAN_EVASION", "0") != "0"
# Spoof score (0-100) at/above which a member is auto-BANNED. 0 disables.
SPOOF_BAN_THRESHOLD = _env_int("ALTGUARD_SPOOF_BAN", 60)


def _verify_link(uid: int, gid: int) -> str:
    return f"{GATE_URL}/v/{pack(make_token(SECRET, uid, gid))}"


def _device_profile(attrs: dict) -> str:
    """Readable device line from the captured fingerprint attributes."""
    if not attrs:
        return "—"
    ua = attrs.get("ua", "") or ""
    os_name = (
        "Windows" if "Windows" in ua else
        "Android" if "Android" in ua else
        "iOS" if ("iPhone" in ua or "iPad" in ua) else
        "macOS" if "Mac OS" in ua or "Macintosh" in ua else
        "Linux" if "Linux" in ua else "?"
    )
    browser = (
        "Edge" if "Edg/" in ua else
        "Chrome" if "Chrome" in ua or "CriOS" in ua else
        "Firefox" if "Firefox" in ua or "FxiOS" in ua else
        "Safari" if "Safari" in ua else "?"
    )
    gpu = (attrs.get("glRenderer") or "").replace("ANGLE (", "").rstrip(")")
    if len(gpu) > 60:
        gpu = gpu[:60] + "…"
    parts = [p for p in (os_name, browser, gpu) if p and p != "?"]
    for key, fmt in (("cores", "{} cores"), ("memory", "{}GB"), ("screen", "{}"), ("tz", "{}")):
        v = attrs.get(key)
        if v:
            parts.append(fmt.format(v))
    return " · ".join(parts) or "—"


def _hmac_headers() -> dict:
    ts = str(time.time())
    sig = hmac.new(SECRET.encode(), ts.encode(), hashlib.sha256).hexdigest()
    return {"X-AltGuard-TS": ts, "X-AltGuard-Auth": sig}


class VerifyView(discord.ui.View):
    def __init__(self, url: str):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(label="Verify now", url=url, emoji="🔒"))


class VerifyPanel(discord.ui.View):
    """Persistent click-to-verify panel for a verify channel. Clicking the button
    replies EPHEMERALLY with the clicker's OWN personal link (built from their
    user id) — no typing, no shared link, and OAuth makes it impossible to verify
    as anyone else. Survives restarts via the registered custom_id."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Verify", style=discord.ButtonStyle.primary,
                       emoji="🔒", custom_id="altguard:verify_panel")
    async def verify(self, interaction: discord.Interaction, button: discord.ui.Button):
        url = _verify_link(interaction.user.id, interaction.guild_id)
        qstore.record_issue(interaction.user.id, interaction.guild_id, True)
        await interaction.response.send_message(
            "🔒 Here's **your** verification link — only you can see this. Click it, let the "
            "page finish its quick automated check, and your access unlocks once it passes.",
            view=VerifyView(url), ephemeral=True,
        )


class AltGuard(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.session: aiohttp.ClientSession | None = None
        # live, runtime-toggleable forced-gate flag (env is just the seed default)
        self.quarantine_on_join = QUARANTINE_ON_JOIN

    async def cog_load(self):
        qstore.init()
        # a persisted /altguard-gate toggle wins over the env default
        persisted = qstore.get_setting("quarantine_on_join")
        if persisted is not None:
            self.quarantine_on_join = persisted == "1"
        self.session = aiohttp.ClientSession()
        self.bot.add_view(VerifyPanel())  # persistent verify button — works after restarts
        self.poll_results.start()

    async def cog_unload(self):
        self.poll_results.cancel()
        if self.session:
            await self.session.close()

    # ------------------------------------------------------------------ quarantine
    def _removable_roles(self, member: discord.Member):
        """Roles we're allowed to strip: not @everyone, not the quarantine role,
        not managed (bot/booster/integration) roles, and below the bot's top role."""
        me = member.guild.me
        out = []
        for r in member.roles:
            if r.is_default() or r.id == QUARANTINE_ROLE_ID or r.managed:
                continue
            if me and r >= me.top_role:
                continue  # can't touch roles at/above the bot
            out.append(r)
        return out

    async def _quarantine(self, member: discord.Member, reason: str):
        """Strip + store roles, apply quarantine role. Returns (ok, removed_roles)."""
        qrole = member.guild.get_role(QUARANTINE_ROLE_ID)
        if not qrole:
            log.warning("Quarantine role %s not found", QUARANTINE_ROLE_ID)
            return False, []
        if qstore.is_quarantined(member.id) and qrole in member.roles:
            return True, []  # already handled

        removable = self._removable_roles(member)
        # store BEFORE removing so a crash mid-op is still recoverable
        qstore.save(member.id, member.guild.id, [r.id for r in removable], reason)
        # one bulk role edit: keep everything else, drop removable, add quarantine
        rm = set(removable)
        target = [r for r in member.roles if not r.is_default() and r not in rm]
        if qrole not in target:
            target.append(qrole)
        try:
            await member.edit(roles=target, reason=f"AltGuard: {reason}")
            return True, removable
        except discord.Forbidden:
            log.warning("Missing permissions to quarantine %s", member)
            return False, removable

    def _default_roles(self, guild):
        """Opt-out default roles that exist and the bot can assign (below its top
        role, not managed). Empty unless ALTGUARD_DEFAULT_ROLES is set."""
        me = guild.me
        out = []
        for rid in DEFAULT_ROLE_IDS:
            r = guild.get_role(rid)
            if r and not r.managed and me and r < me.top_role:
                out.append(r)
        return out

    async def _release(self, member: discord.Member):
        """Remove quarantine role, restore the exact roles we removed, AND grant
        the opt-out default roles — in a single bulk edit."""
        qrole = member.guild.get_role(QUARANTINE_ROLE_ID)
        me = member.guild.me
        stored = qstore.pop(member.id)
        restore = []
        for rid in stored:
            r = member.guild.get_role(rid)
            if r and not r.managed and me and r < me.top_role:
                restore.append(r)
        # final set: current roles, minus quarantine, plus restored, plus defaults
        target = [r for r in member.roles if not r.is_default() and r != qrole]
        for r in restore + self._default_roles(member.guild):
            if r not in target:
                target.append(r)
        try:
            await member.edit(roles=target, reason="AltGuard: quarantine cleared (restore + defaults)")
            return True, restore
        except discord.Forbidden:
            return False, restore

    async def _ban_status(self, guild: discord.Guild, uid: int) -> str:
        """Classify a non-member matched account: 'banned' | 'left' | 'unknown'."""
        try:
            await guild.fetch_ban(discord.Object(id=uid))
            return "banned"
        except discord.NotFound:
            return "left"
        except discord.Forbidden:
            return "unknown"  # bot lacks Ban Members
        except discord.HTTPException:
            return "left"

    async def _dm_user(self, user, guild, locked: bool = False) -> bool:
        """DM a verify link to any user/member. Returns False if unreachable
        (closed DMs, or an ex-user the bot shares no server with). `locked`
        switches the copy for members whose access is held until they verify."""
        url = _verify_link(user.id, guild.id)
        if locked:
            embed = discord.Embed(
                title="🔒 Verify to unlock the server",
                description=(
                    f"Welcome to **{guild.name}**! As an anti-raid measure, your access is "
                    "**temporarily restricted** until you finish a quick automated check. "
                    "Click below — it takes a second, and you'll get full access the moment it passes."
                ),
                color=0x5B8CFF,
            )
        else:
            embed = discord.Embed(
                title="🔒 Quick verification",
                description=(
                    f"To keep **{guild.name}** safe from raids and alt accounts, "
                    "please click below and let the page finish its quick automated check. "
                    "Takes a second."
                ),
                color=0x5B8CFF,
            )
        # Anti-phishing trust block: "verify to log in" DMs look exactly like scams,
        # so newcomers hesitate to click. Name the fear and defuse it — explain it's
        # the real bot, the login is on discord.com, and we can't act as them.
        embed.add_field(
            name="✅ Why this is safe",
            value=(
                "• You'll log in **on discord.com** — never on our site. Check the address bar.\n"
                "• We only see your **username** — we **can't** post, DM, or do anything as you.\n"
                "• We will **never** ask for your password or a QR-code scan."
            ),
            inline=False,
        )
        embed.set_footer(text=f"Official {guild.name} verification • link goes to verify.torvex.app")
        try:
            await user.send(embed=embed, view=VerifyView(url))
            return True
        except discord.Forbidden:
            return False

    async def _dm_link(self, member: discord.Member, locked: bool = False) -> bool:
        return await self._dm_user(member, member.guild, locked=locked)

    async def _issue(self, member: discord.Member, force: bool = False, locked: bool = False) -> str:
        """Issue a verify link with tracking. Auto-paths (force=False) never
        re-DM someone already on record. Returns a short status string."""
        prior = qstore.verification(member.id)
        if prior and not force:
            return f"already issued ({prior.get('status', 'pending')}) — not re-DMing"
        dmed = await self._dm_link(member, locked=locked)
        qstore.record_issue(member.id, member.guild.id, dmed)
        return "DMed ✅" if dmed else "DMs closed ⚠️"

    # ------------------------------------------------------------------ events
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot or member.guild.id != GUILD_ID:
            return
        # forced-gate: strip access on the way in, before anything else
        quarantined = False
        if self.quarantine_on_join:
            quarantined, _ = await self._quarantine(member, "awaiting verification (quarantine-on-join)")
            if not quarantined:
                log.warning("quarantine-on-join failed for %s — check Manage Roles + hierarchy", member)
        else:
            # detect-only mode: grant opt-out defaults right away. (Gated mode
            # grants them on release instead, so the reconciliation listener
            # doesn't strip them while the member is held.)
            defaults = self._default_roles(member.guild)
            if defaults:
                try:
                    await member.add_roles(*defaults, reason="AltGuard: default roles on join")
                except discord.Forbidden:
                    pass
        # gate ON always DMs the link (a held member has no other way in);
        # detect-only mode DMs only when DM_ON_JOIN is set
        status = ""
        if self.quarantine_on_join or DM_ON_JOIN:
            status = await self._issue(member, locked=quarantined)
        dm_failed = "closed" in status
        # visible fallback: ping them in the verify channel so a closed/unseen DM
        # isn't a dead end — they just tap the panel button there.
        # skip while mid-onboarding: they can't see the verify channel yet, and
        # _on_onboarding_complete will post the ping once they can — avoids a
        # duplicate prompt for members who join through Discord onboarding.
        if quarantined and VERIFY_CHANNEL_ID and not member.pending:
            vch = member.guild.get_channel(VERIFY_CHANNEL_ID)
            if vch:
                try:
                    await vch.send(
                        f"👋 {member.mention} — your access is held for a quick anti-raid check. "
                        f"Tap **🔒 Verify** above to unlock (we also DMed you the link)."
                    )
                except discord.Forbidden:
                    pass
        age_days = (discord.utils.utcnow() - member.created_at).days
        if self.quarantine_on_join or age_days < MIN_ACCOUNT_AGE_DAYS or dm_failed:
            ch = member.guild.get_channel(MODLOG_CHANNEL_ID)
            if ch:
                note = []
                if self.quarantine_on_join:
                    note.append("🔒 **quarantined on join**" if quarantined
                                else "⚠️ **quarantine-on-join FAILED** — check my perms/role order")
                if age_days < MIN_ACCOUNT_AGE_DAYS:
                    note.append(f"account only **{age_days}d** old")
                if dm_failed:
                    note.append("**DMs closed** — couldn't deliver link; tell them to run `/verify`")
                await ch.send(f"👀 AltGuard: {member.mention} (`{member.id}`) joined — {', '.join(note)}.")

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        """Beat the autorole race (MEE6 etc.). While a member is held, any role
        they GAIN is re-stripped and folded into their restore set — so it
        doesn't matter who wins the join race or how late the autorole lands."""
        if after.bot or after.guild.id != GUILD_ID:
            return
        # Onboarding finished (Discord 'pending' just cleared): a member held during
        # onboarding couldn't SEE the verify channel and is easy to miss; now they
        # can. Re-point them at it (and retry the DM if it never landed) so they
        # don't finish onboarding into a dead end with no way to verify.
        if before.pending and not after.pending:
            await self._on_onboarding_complete(after)
        if not qstore.is_quarantined(after.id):
            return
        qrole = after.guild.get_role(QUARANTINE_ROLE_ID)
        if not qrole or qrole not in after.roles:
            return  # not actually wearing the quarantine role (e.g. mid-release)
        before_ids = {r.id for r in before.roles}
        gained = [r for r in after.roles if r.id not in before_ids]
        strip = [r for r in self._removable_roles(after) if r in gained]
        if not strip:
            return  # nothing new to strip (also how our own edit avoids a loop)
        qstore.add_roles(after.id, after.guild.id, [r.id for r in strip])
        keep = set(strip)
        target = [r for r in after.roles if r not in keep]
        try:
            await after.edit(roles=target, reason="AltGuard: role granted while quarantined — re-stripped")
        except discord.Forbidden:
            log.warning("Couldn't re-strip autorole from quarantined %s", after)
            return
        ch = after.guild.get_channel(MODLOG_CHANNEL_ID)
        if ch:
            names = ", ".join(r.mention for r in strip)
            await ch.send(
                f"🧹 AltGuard re-stripped {names} from {after.mention} (`{after.id}`) — "
                f"added while quarantined (autorole race); stored for restore on pass."
            )

    async def _on_onboarding_complete(self, member: discord.Member):
        """A held member just finished Discord onboarding. Nudge them in the
        now-visible verify channel and retry the DM if the first never landed.
        No-op for anyone not held or already passed."""
        if not qstore.is_quarantined(member.id):
            return
        v = qstore.verification(member.id)
        if v and v.get("status") == "passed":
            return
        # verify channel is reachable now — point them at the panel button
        if VERIFY_CHANNEL_ID:
            vch = member.guild.get_channel(VERIFY_CHANNEL_ID)
            if vch:
                try:
                    await vch.send(
                        f"👋 {member.mention} — welcome! Your access is held for a quick "
                        f"anti-raid check. Tap **🔒 Verify** above to unlock the server."
                    )
                except discord.Forbidden:
                    pass
        # if the join-time DM never delivered, try again now that they're settled
        if not (v and v.get("dm_delivered")):
            dmed = await self._dm_link(member, locked=True)
            qstore.record_issue(member.id, member.guild.id, dmed)
        ch = member.guild.get_channel(MODLOG_CHANNEL_ID)
        if ch:
            try:
                await ch.send(
                    f"🎬 AltGuard: {member.mention} (`{member.id}`) finished onboarding "
                    f"while held — re-pointed to verify."
                )
            except discord.Forbidden:
                pass

    # ------------------------------------------------------------------ poller
    @tasks.loop(seconds=10)
    async def poll_results(self):
        if not self.session or not GATE_URL:
            return
        try:
            async with self.session.get(
                f"{GATE_URL}/api/results", headers=_hmac_headers(), timeout=10
            ) as r:
                if r.status != 200:
                    return
                data = await r.json()
        except Exception as e:
            log.debug("poll failed: %s", e)
            return

        guild = self.bot.get_guild(GUILD_ID)
        results = data.get("results", [])
        if not results:
            await self._poll_shares(guild)  # still surface link-sharing
            return

        acked = []
        for res in results:
            acked.append(res["uid"])
            qstore.set_status(res["uid"], "passed" if res["verdict"] == "pass" else "quarantined")
            if qstore.is_watched(res["uid"]):
                await self._watch_alert(guild, res)
            # spoof auto-ban — overrides everything; a faked fingerprint = out
            if guild and SPOOF_BAN_THRESHOLD and res.get("spoof", 0) >= SPOOF_BAN_THRESHOLD:
                if await self._spoof_ban(guild, res):
                    continue
            if not guild:
                continue
            member = guild.get_member(int(res["uid"]))
            if res["verdict"] == "pass":
                # auto-release ONLY join-gate quarantines (by stored reason) — not
                # fail/cascade ones, and independent of the live flag so toggling
                # the gate off mid-verification can't strand a passing member.
                if member and qstore.is_quarantined(member.id) and \
                        "quarantine-on-join" in (qstore.quarantine_reason(member.id) or ""):
                    ok, restored = await self._release(member)
                    await self._released_alert(guild, member, res, restored, ok)
                continue
            if member is None:
                continue
            _, removed = await self._quarantine(member, "verification flagged")

            # classify every fingerprint-matched account: in-server / banned / left
            cascaded, banned, left = [], [], []
            for alt_uid in res.get("alt_uids", []):
                aid = int(alt_uid)
                alt = guild.get_member(aid)
                if alt:
                    if not qstore.is_quarantined(alt.id):
                        ok, _ = await self._quarantine(alt, f"alt of {member.id} (same device)")
                        if ok:
                            cascaded.append(alt)
                    else:
                        cascaded.append(alt)
                else:
                    status = await self._ban_status(guild, aid)
                    (banned if status == "banned" else left).append(aid)

            # ban-evasion: device matches a banned account
            evaded = False
            if banned and AUTOBAN_EVASION:
                try:
                    await guild.ban(member, reason=f"AltGuard: ban evasion — device matches banned {banned}", delete_message_seconds=0)
                    evaded = True
                except discord.Forbidden:
                    log.warning("Wanted to ban %s for evasion but lack permission", member)

            await self._alert(guild, member, res, removed, cascaded, banned, left, evaded)

        try:
            async with self.session.post(
                f"{GATE_URL}/api/ack", headers=_hmac_headers(), json={"uids": acked}, timeout=10
            ):
                pass
        except Exception as e:
            log.debug("ack failed: %s", e)

        await self._poll_shares(guild)

    async def _poll_shares(self, guild):
        """Surface link-sharing: a link issued for A opened by B (verified as B)."""
        try:
            async with self.session.get(
                f"{GATE_URL}/api/shares", headers=_hmac_headers(), timeout=10
            ) as r:
                if r.status != 200:
                    return
                shares = (await r.json()).get("shares", [])
        except Exception:
            return
        if not shares:
            return
        ch = guild.get_channel(MODLOG_CHANNEL_ID) if guild else None
        ids = []
        for s in shares:
            ids.append(s["id"])
            if not ch:
                continue
            tag = ""
            if guild:
                aid = int(s["clicker_uid"])
                tag = " `in-server`" if guild.get_member(aid) else f" `{await self._ban_status(guild, aid)}`"
            embed = discord.Embed(
                title="🚩 Link sharing detected",
                color=0xE0A23B,
                description=(
                    f"A verification link **issued for** <@{s['target_uid']}> "
                    f"(`{s['target_uid']}`) was **opened by** <@{s['clicker_uid']}> "
                    f"(`{s['clicker_uid']}` @{s.get('clicker_name','?')}){tag}.\n"
                    f"They were verified as themselves — but these two accounts are **connected**."
                ),
            )
            embed.add_field(name="Opener IP", value=f"`{s.get('ip','?')}`", inline=True)
            embed.set_footer(text="Someone passed their link around — worth a look.")
            await ch.send(embed=embed)
        try:
            async with self.session.post(
                f"{GATE_URL}/api/shares/ack", headers=_hmac_headers(), json={"ids": ids}, timeout=10
            ):
                pass
        except Exception:
            pass

    async def _spoof_ban(self, guild, res):
        """A fingerprint manipulated past the spoof threshold = instant ban."""
        member = guild.get_member(int(res["uid"]))
        spoof = res.get("spoof", 0)
        if member is None:
            return False  # not in server (can't ban) — fall through to normal handling
        try:
            await member.ban(reason=f"AltGuard: spoofed fingerprint ({spoof}%)", delete_message_seconds=0)
        except discord.Forbidden:
            log.warning("Wanted to spoof-ban %s but lack permission", member)
            return False
        qstore.set_status(res["uid"], "banned")
        ch = guild.get_channel(MODLOG_CHANNEL_ID)
        if ch:
            embed = discord.Embed(
                title="🔨 Auto-banned — spoofed fingerprint",
                color=0x8B0000,
                description=f"{member.mention} `{member.id}` was **banned**: spoof score **{spoof}%** "
                            f"(≥ {SPOOF_BAN_THRESHOLD}% threshold).",
            )
            embed.add_field(name="Why", value="\n".join(f"• {r}" for r in res.get("reasons", []))[:1024] or "—", inline=False)
            embed.add_field(name="🖥️ Device", value=_device_profile(res.get("attrs") or {})[:1024], inline=False)
            embed.set_footer(text="Manipulated environment — unban manually if this was a mistake.")
            await ch.send(embed=embed)
        return True

    async def _released_alert(self, guild, member, res, restored, ok):
        """Forced-gate: a quarantined member passed and was auto-released."""
        ch = guild.get_channel(MODLOG_CHANNEL_ID) if guild else None
        if not ch:
            return
        roles = ", ".join(r.mention for r in restored) if restored else "none stored"
        embed = discord.Embed(
            title="✅ Verified — access restored" if ok else "⚠️ Passed but auto-release failed",
            color=0x3BA55D if ok else 0xE0A23B,
            description=(
                f"{member.mention} `{member.id}` passed verification and the quarantine "
                f"was lifted automatically." if ok else
                f"{member.mention} `{member.id}` passed, but I couldn't lift the quarantine — "
                f"check my Manage Roles permission / role order, then `/altguard-release`."
            ),
        )
        embed.add_field(name="Top device match", value=f"{res.get('risk', 0)}%", inline=True)
        embed.add_field(name="Connection", value=f"{res.get('country', '?')} · {res.get('isp', '?')}", inline=True)
        if ok:
            embed.add_field(name="Roles restored", value=roles, inline=False)
        await ch.send(embed=embed)

    async def _watch_alert(self, guild, res):
        """A watchlisted (banned/wanted) account just completed verification."""
        ch = guild.get_channel(MODLOG_CHANNEL_ID) if guild else None
        if not ch:
            return
        reason = qstore.watch_reason(res["uid"]) or "—"
        embed = discord.Embed(
            title="🚨 WANTED account surfaced",
            color=0x8B0000,
            description=(
                f"Watchlisted account <@{res['uid']}> (`{res['uid']}`) **just completed verification**.\n"
                f"Their device is now **on file** — any future alt on it will trip ban-evasion.\n"
                f"Reason watched: *{reason}*"
            ),
        )
        embed.add_field(name="Verdict", value=res.get("verdict", "?"), inline=True)
        embed.add_field(name="Match", value=f"{res.get('match_pct',0)}%", inline=True)
        embed.add_field(name="🖥️ Device", value=_device_profile(res.get("attrs") or {})[:1024], inline=False)
        embed.add_field(name="Connection", value=f"{res.get('country','?')} · {res.get('isp','?')} · `{res.get('ip','?')}`", inline=False)
        await ch.send(content="@here", embed=embed)

    @poll_results.before_loop
    async def _before_poll(self):
        await self.bot.wait_until_ready()

    async def _alert(self, guild, member, res, removed, cascaded, banned, left, evaded):
        ch = guild.get_channel(MODLOG_CHANNEL_ID)
        if not ch:
            return
        age_days = (discord.utils.utcnow() - member.created_at).days
        reasons = "\n".join(f"• {r}" for r in res.get("reasons", [])) or "• (none)"
        removed_txt = ", ".join(r.mention for r in removed) if removed else "none"
        cascade_txt = ", ".join(f"{m.mention} (`{m.id}`)" for m in cascaded) if cascaded else "—"

        ban_evasion = bool(banned)
        if ban_evasion:
            title = "🚨 BAN EVASION — device matches a banned account"
            color = 0x8B0000
            desc = (
                f"{member.mention} `{member.id}` shares a device/GPU signature with a **banned** account.\n"
                + ("**Auto-banned.**" if evaded else "Quarantined — review and ban if confirmed.")
            )
        else:
            title = "⚠️ AltGuard quarantined a member"
            color = 0xE03B3B
            desc = f"{member.mention} `{member.id}` was auto-quarantined."

        env = res.get("environment") or "?"
        conf = res.get("confidence")
        conf_txt = f" · conf {conf}" if conf is not None else ""
        embed = discord.Embed(title=title, color=color, description=desc)
        embed.add_field(name="Top device match", value=f"**{res.get('risk', 0)}%**{conf_txt}", inline=True)
        embed.add_field(name="Environment", value=env, inline=True)
        embed.add_field(name="Account age", value=f"{age_days}d", inline=True)
        embed.add_field(name="Connection", value=f"{res.get('country', '?')} · {res.get('isp', '?')}", inline=True)
        embed.add_field(name="Why", value=reasons, inline=False)
        embed.add_field(name="🖥️ Device", value=_device_profile(res.get("attrs") or {})[:1024], inline=False)
        if banned:
            embed.add_field(name="🚨 Matches BANNED accounts", value=", ".join(f"<@{u}> (`{u}`)" for u in banned)[:1024], inline=False)
        if left:
            embed.add_field(name="Matches accounts that left", value=", ".join(f"<@{u}> (`{u}`)" for u in left)[:1024], inline=False)
        embed.add_field(name="Alts in-server also quarantined", value=cascade_txt, inline=False)
        if not evaded:
            embed.add_field(name="Roles removed (stored for restore)", value=removed_txt, inline=False)
        embed.set_footer(text="False positive? /altguard-release @user restores their exact roles")
        await ch.send(embed=embed)

    # ------------------------------------------------------------------ commands
    @app_commands.command(name="verify", description="Get your verification link")
    @app_commands.default_permissions(administrator=True)
    async def verify(self, interaction: discord.Interaction):
        url = _verify_link(interaction.user.id, interaction.guild_id)
        qstore.record_issue(interaction.user.id, interaction.guild_id, True)
        await interaction.response.send_message(
            "Click below to verify. The page does a quick automated check — that's it.",
            view=VerifyView(url), ephemeral=True,
        )

    @app_commands.command(
        name="altguard-verify-panel",
        description="Post the click-to-verify button in this channel (admin)",
    )
    @app_commands.default_permissions(administrator=True)
    async def verify_panel(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="🔒 Verification required",
            description=(
                "Your access is temporarily restricted as an anti-raid measure. "
                "Press **Verify** below to get **your** personal link and unlock the server — "
                "only you can see it. Takes a few seconds."
            ),
            color=0x5B8CFF,
        )
        await interaction.channel.send(embed=embed, view=VerifyPanel())
        await interaction.response.send_message("✅ Verify panel posted here.", ephemeral=True)

    @app_commands.command(
        name="altguard-sweep",
        description="DM every human member a verification link (failures get quarantined)",
    )
    @app_commands.describe(dry_run="just count, don't DM anyone")
    @app_commands.default_permissions(administrator=True)
    async def sweep(self, interaction: discord.Interaction, dry_run: bool = False):
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        targeted = dmed = skipped = 0
        for member in guild.members:
            if member.bot or member.guild_permissions.administrator:
                skipped += 1
                continue
            targeted += 1
            if dry_run:
                continue
            st = await self._issue(member)  # skips anyone already issued
            if "DMed" in st:
                dmed += 1
            elif "already issued" in st:
                skipped += 1
        if dry_run:
            msg = f"Dry run: **{targeted}** members would be considered ({skipped} bots/admins skipped)."
        else:
            msg = (
                f"DMed a verify link to **{dmed}** members "
                f"(skipped {skipped} bots/admins/already-issued; closed-DM members can `/verify`). "
                f"Failures are auto-quarantined and logged to <#{MODLOG_CHANNEL_ID}>."
            )
        await interaction.followup.send(msg, ephemeral=True)

    @app_commands.command(
        name="altguard-gate",
        description="Forced quarantine-on-join: turn ON/OFF live (persists), or omit to check status",
    )
    @app_commands.describe(
        enabled="On = every new joiner is quarantined until they verify. Omit to just see the current state.",
    )
    @app_commands.default_permissions(administrator=True)
    async def gate(self, interaction: discord.Interaction, enabled: bool = None):
        if enabled is None:
            state = "🔒 **ON**" if self.quarantine_on_join else "🔓 **OFF**"
            await interaction.response.send_message(
                f"Forced quarantine-on-join is currently {state}.\n"
                f"-# Change it with `/altguard-gate enabled:True` or `enabled:False` — the setting persists across restarts.",
                ephemeral=True,
            )
            return
        self.quarantine_on_join = enabled
        qstore.set_setting("quarantine_on_join", "1" if enabled else "0")
        if enabled:
            msg = (
                "🔒 Forced gate **ON**. Every new human is now quarantined the moment they join and DMed a "
                "verify link; they're auto-released the instant they pass. Existing members are untouched.\n"
                "-# Make sure a `#verify` channel is visible to the quarantine role for closed-DM joiners."
            )
        else:
            msg = (
                "🔓 Forced gate **OFF** (detect-only). New joiners keep normal access; only flagged/failed "
                "verifications get quarantined.\n"
                "-# Members already quarantined stay that way until they pass or you `/altguard-release` them."
            )
        await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(
        name="altguard-check",
        description="Verify a member OR an ex-user (by ID) — DM them, or just generate a link",
    )
    @app_commands.describe(
        user="the member to verify (in-server)",
        user_id="raw Discord ID — for an ex-user who left or was banned",
        dm="True (default) = try to DM them; False = just give YOU the link",
    )
    @app_commands.default_permissions(administrator=True)
    async def check(self, interaction: discord.Interaction,
                    user: discord.User = None, user_id: str = None, dm: bool = True):
        await interaction.response.defer(ephemeral=True, thinking=True)
        uid = str(user.id) if user else (user_id or "").strip()
        if not uid.isdigit():
            await interaction.followup.send("Give me a member, or a numeric `user_id` for an ex-user.", ephemeral=True)
            return
        if user and user.bot:
            await interaction.followup.send("That's a bot — nothing to check.", ephemeral=True)
            return

        in_server = interaction.guild.get_member(int(uid)) is not None
        target = user
        if target is None:
            try:
                target = await self.bot.fetch_user(int(uid))
            except discord.HTTPException:
                target = None

        url = _verify_link(int(uid), interaction.guild_id)
        dmed = False
        if dm and target is not None:
            dmed = await self._dm_user(target, interaction.guild)
        if dmed:
            status = "📨 Verify link DMed to them."
        elif not dm:
            status = "🔗 Link generated (not DMed) — deliver it however you like:"
        elif not in_server:
            status = "⚠️ Ex-user — the bot can't DM someone it shares no server with. Deliver this link yourself:"
        else:
            status = "⚠️ Their DMs are closed — deliver this link yourself:"

        qstore.record_issue(int(uid), interaction.guild_id, dmed)
        name = target.display_name if target else uid
        tag = " — not in server" if not in_server else ""
        await interaction.followup.send(
            f"Verification requested for <@{uid}> (`{uid}`){tag}.\n{status}\n`{url}`\n"
            f"-# Scored **as {name}**; OAuth makes them log in as that exact account, so it can't be misattributed. "
            f"Link stays valid until they use it. Result lands in <#{MODLOG_CHANNEL_ID}> and `altguard-records`.",
            ephemeral=True,
        )
        ch = interaction.guild.get_channel(MODLOG_CHANNEL_ID)
        if ch:
            delivery = "📨 DMed" if dmed else (
                "🔗 link handed to mod" if not dm else
                ("⚠️ ex-user, manual delivery" if not in_server else "⚠️ DMs closed"))
            audit = discord.Embed(
                title="🔍 Verification check issued",
                color=0x5B8CFF,
                description=f"{interaction.user.mention} requested verification from <@{uid}> (`{uid}`)"
                            f"{' *(ex-user)*' if not in_server else ''}.",
            )
            audit.add_field(name="Delivery", value=delivery, inline=True)
            audit.set_footer(text="Verdict will post here when they complete it.")
            await ch.send(embed=audit)

    async def _annotate(self, guild, uids):
        """Tag each uid with in-server / left / BANNED for the lookup view."""
        out = []
        for u in uids:
            aid = int(u)
            if guild.get_member(aid):
                out.append(f"<@{u}> `in-server`")
            else:
                s = await self._ban_status(guild, aid)
                tag = "🚨BANNED" if s == "banned" else ("left" if s == "left" else "?")
                out.append(f"<@{u}> (`{u}`) {tag}")
        return out

    @app_commands.command(
        name="altguard-lookup",
        description="Inspect a user's fingerprint/verdict history and who they link to",
    )
    @app_commands.describe(
        user="member (or use user_id for someone not in the server)",
        user_id="raw Discord ID — for banned/left accounts not in the server",
    )
    @app_commands.default_permissions(administrator=True)
    async def lookup(self, interaction: discord.Interaction, user: discord.User = None, user_id: str = None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        uid = str(user.id) if user else (user_id or "").strip()
        if not uid.isdigit():
            await interaction.followup.send("Give me a member or a numeric `user_id`.", ephemeral=True)
            return

        try:
            async with self.session.get(
                f"{GATE_URL}/api/lookup", params={"uid": uid},
                headers=_hmac_headers(), timeout=10,
            ) as r:
                data = await r.json()
        except Exception as e:
            await interaction.followup.send(f"Lookup failed: {e}", ephemeral=True)
            return

        guild = interaction.guild
        v = qstore.verification(uid)
        issued = "never issued a link" if not v else f"link **{v['status']}**"
        embed = discord.Embed(title=f"🔎 AltGuard lookup — {uid}", color=0x5B8CFF)
        embed.add_field(name="Verification", value=issued, inline=True)

        if not data.get("found"):
            embed.description = "No verification on file (this account never completed the gate)."
        else:
            res = data["result"]
            embed.add_field(name="Last verdict", value=f"**{res['verdict']}** · top match {res.get('match_pct',0)}%", inline=True)
            embed.add_field(name="Environment", value=f"{res.get('environment','?')} · conf {res.get('confidence','?')}", inline=True)
            embed.add_field(name="Connection", value=f"{res.get('country','?')} · {res.get('isp','?')} · `{res.get('ip','?')}`", inline=False)
            embed.add_field(name="🖥️ Device", value=_device_profile(res.get("attrs") or {})[:1024], inline=False)

        # device-similarity matches with %, and what they matched on
        matches = data.get("matches", [])
        if matches:
            lines = []
            for m in matches[:12]:
                tag = (await self._annotate(guild, [m["uid"]]))[0]
                lines.append(f"**{m['pct']}%** {tag} · on: {', '.join(m.get('matched', [])) or '—'}")
            embed.add_field(name="Device-similarity matches", value="\n".join(lines)[:1024], inline=False)
        else:
            embed.set_footer(text="No similar devices on file — stands alone.")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="altguard-release", description="Clear a quarantine and restore removed roles")
    @app_commands.default_permissions(administrator=True)
    async def release(self, interaction: discord.Interaction, member: discord.Member):
        ok, restored = await self._release(member)
        if ok:
            roles = ", ".join(r.mention for r in restored) if restored else "no stored roles"
            await interaction.response.send_message(
                f"✅ Cleared quarantine on {member.mention}. Restored: {roles}.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"⚠️ Couldn't fully restore {member.mention} — check my permissions/role hierarchy.",
                ephemeral=True,
            )

    @app_commands.command(
        name="altguard-watch",
        description="Watchlist a (banned) account — loud alert if they ever verify or an alt matches them",
    )
    @app_commands.describe(user_id="raw Discord ID to watch", reason="why (e.g. 'banned raider')")
    @app_commands.default_permissions(administrator=True)
    async def watch(self, interaction: discord.Interaction, user_id: str, reason: str = ""):
        uid = user_id.strip()
        if not uid.isdigit():
            await interaction.response.send_message("Give me a numeric `user_id`.", ephemeral=True)
            return
        qstore.watch(uid, reason)
        await interaction.response.send_message(
            f"👁️ Watchlisted <@{uid}> (`{uid}`)" + (f" — *{reason}*" if reason else "") +
            f".\nIf they verify (their link still works) or a device matches them, "
            f"a 🚨 alert fires in <#{MODLOG_CHANNEL_ID}>.",
            ephemeral=True,
        )

    @app_commands.command(name="altguard-unwatch", description="Remove an account from the watchlist")
    @app_commands.describe(user_id="raw Discord ID to stop watching")
    @app_commands.default_permissions(administrator=True)
    async def unwatch(self, interaction: discord.Interaction, user_id: str):
        ok = qstore.unwatch(user_id.strip())
        await interaction.response.send_message(
            f"{'✅ Removed' if ok else '⚠️ Not on'} the watchlist: `{user_id}`.", ephemeral=True
        )


async def setup(bot: commands.Bot):
    missing = [n for n, v in (
        ("ALTGUARD_SECRET", SECRET), ("ALTGUARD_GATE_URL", GATE_URL),
        ("ALTGUARD_GUILD_ID", GUILD_ID), ("ALTGUARD_QUARANTINE_ROLE_ID", QUARANTINE_ROLE_ID),
    ) if not v]
    if missing:
        raise RuntimeError("AltGuard env not configured: " + ", ".join(missing))
    await bot.add_cog(AltGuard(bot))
