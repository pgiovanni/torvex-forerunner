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
import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import time
from collections import deque

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

import quarantine_store as qstore
import rejoin_roles
from tokens import make_token, pack
from utils.security_config import get_config
from utils import altguard_mode as agmode
from utils import security_ai as sai

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
# Observe mode's raid heuristic: joins per guild inside this window (seconds).
OBSERVE_BURST_SECS = _env_int("ALTGUARD_OBSERVE_BURST_SECS", 60)
# "Almost Verified": a member who opened their verify link and scored a CLEAN,
# HIGH-TIMING-CONFIDENCE pass, but stopped at the Discord authorize screen. The
# role carries ZERO server permissions — its only effect is an allow-send
# overwrite on #verify, so they can ask a human why the login step looks like
# phishing instead of sitting mute until the 72h prune. Everything else stays
# locked: they keep the quarantine role and every anti-nuke / LinkGuard rule
# applies to them unchanged (antinuke._exempt is uid-based — owner, bot and the
# guild whitelist — so no role can buy an exemption). 0 = feature off.
ALMOST_ROLE_ID = _env_int("ALTGUARD_ALMOST_ROLE_ID")
ALMOST_SYNC_MIN = _env_int("ALTGUARD_ALMOST_SYNC_MIN", 5)
PRUNE_HOURS_HINT = _env_int("PRUNE_HOURS", 72)  # display only; verify_prune owns the clock
# opt-out default roles auto-granted when a member gains access (at join if not
# gating, or on release after they verify). Replaces MEE6 autorole. Members can
# remove any they don't want.
DEFAULT_ROLE_IDS = [int(x) for x in os.environ.get("ALTGUARD_DEFAULT_ROLES", "").replace(",", " ").split() if x.strip().isdigit()]
# Age roles granted from the verify page's age-group selection (results.age =
# "18+" | "under18"). Picking one always clears the other. 0 = feature off.
AGE_ROLE_18PLUS = _env_int("ALTGUARD_AGE_ROLE_18PLUS", 0)
AGE_ROLE_UNDER18 = _env_int("ALTGUARD_AGE_ROLE_UNDER18", 0)

# Age-group bands the verify page + reaction panel offer: {pick string -> role id}.
# From ALTGUARD_AGE_ROLES (json). The legacy 18+/under18 keys stay mapped for
# in-flight results and existing members (go-forward migration). Picking any band
# grants it and drops every OTHER age role (mutual exclusivity, done server-side).
def _age_role_map():
    m = {}
    raw = os.environ.get("ALTGUARD_AGE_ROLES", "")
    if raw:
        try:
            m = {str(k): int(v) for k, v in json.loads(raw).items() if str(v).strip().isdigit()}
        except (ValueError, TypeError):
            m = {}
    if AGE_ROLE_18PLUS:
        m.setdefault("18+", AGE_ROLE_18PLUS)
    if AGE_ROLE_UNDER18:
        m.setdefault("under18", AGE_ROLE_UNDER18)
    return m


AGE_ROLE_MAP = _age_role_map()
ALL_AGE_ROLE_IDS = set(AGE_ROLE_MAP.values())

# Band stamped on ANY release where the member ends up with no age band — a manual
# /altguard-release, a pass that carried no pick, or the prune's auto-approve. A key
# from ALTGUARD_AGE_ROLES ("13-15" … "28+"); empty = leave them bandless. Defaulting
# down is the safe direction: an adult mislabelled has a badge they can fix in
# #reaction-roles, the reverse leaves a minor labelled as an adult. Never overwrites
# a real pick or a restored band.
DEFAULT_AGE = os.environ.get("ALTGUARD_DEFAULT_AGE", "").strip()

# Returning-member role restore NEVER hands back these permissionless-but-sensitive
# roles (staff/access/quarantine). Permission-bearing roles are already excluded by
# rejoin_roles' permission filter; this is the belt-and-suspenders list for the ones
# that carry no perms. Extendable via ALTGUARD_NO_RESTORE_ROLES (space/comma ids).
NO_RESTORE_ROLE_IDS = {
    1526741916979298316,  # 18+ Staff (adult-staff access gate)
    1215150451234963466,  # Admin
    1514724003992961165,  # Senior Admin
    1500624978129719437,  # Senior Moderator
    1215294456199254147,  # Mod
    1466063845352017971,  # Retired Super Admin
    1466063596445503499,  # Retired Admin
    1466063286352089303,  # Retired Staff
    1377788209488068689,  # Minecraft Staff
    QUARANTINE_ROLE_ID,
} | {int(x) for x in os.environ.get("ALTGUARD_NO_RESTORE_ROLES", "").replace(",", " ").split() if x.strip().isdigit()}
DM_ON_JOIN = os.environ.get("ALTGUARD_DM_ON_JOIN", "1") != "0"
# Forced-gate mode: quarantine EVERY human the moment they join (strip access),
# DM them the link, and auto-release them on a PASS verdict. Off = detect-only.
QUARANTINE_ON_JOIN = os.environ.get("ALTGUARD_QUARANTINE_ON_JOIN", "0") != "0"
# When a verifier's device/GPU matches a BANNED account, auto-ban the new one too.
AUTOBAN_EVASION = os.environ.get("ALTGUARD_AUTOBAN_EVASION", "0") != "0"
# Spoof score (0-100) at/above which a member is auto-BANNED. 0 disables.
SPOOF_BAN_THRESHOLD = _env_int("ALTGUARD_SPOOF_BAN", 60)
# Device-match % at/above which accounts count as one confirmed-alt GROUP for
# /altguard-release cascading. Mirror of the gate's MATCH_THRESHOLD verdict bar.
RELEASE_MATCH_PCT = _env_int("ALTGUARD_RELEASE_MATCH_PCT", 85)


def _verify_link(uid: int, gid: int) -> str:
    return f"{GATE_URL}/v/{pack(make_token(SECRET, uid, gid))}"


def _verify_ping_wanted(guild_id: int, dm_delivered: bool) -> bool:
    """Should we @ the held member in the verify channel? Per-guild
    (security_config `verify_ping`, dashboard-settable):

      always     — greet every held member (the original behaviour)
      dm_failed  — only when their DM never landed
      never      — never ping; the Verify panel button in that channel is the
                   only prompt they get

    Turning it off is safe on its own — the panel button is always there — but
    it does mean a closed-DM joiner sees no prompt at all, which is exactly the
    case `dm_failed` covers.
    """
    mode = str(get_config(guild_id).get("verify_ping") or "always")
    if mode == "never":
        return False
    if mode == "dm_failed":
        return not dm_delivered
    return True


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


def _conn_line(res: dict, ip: bool = False) -> str:
    """Connection summary with city-level geolocation when the gate has it.
    Geo comes from the results row (gate stores IPQS city/region since the
    geo-corroboration update); older rows just show country · ISP."""
    loc = ", ".join(v for v in (res.get("city"), res.get("region")) if v)
    if loc and not res.get("geo_trust", True):
        loc += " (exit)"   # anonymizer exit — locates the tunnel, not the person
    parts = [p for p in (f"📍 {loc}" if loc else None,
                         res.get("country", "?"), res.get("isp", "?")) if p]
    line = " · ".join(parts)
    if ip:
        line += f" · `{res.get('ip', '?')}`"
    return line


_CONN_CLASS = {
    "mobile": "📶 mobile carrier",
    "residential": "🏠 residential ISP",
    "business": "🏢 business/organisation line",
    "satellite": "🛰️ satellite ISP",
    "hosting": "🖥️ datacenter / hosting",
    "relay": "🍏 iCloud Private Relay",
    "none": "no rDNS published",
    "unknown": "unclassified network",
}


def _precap_conn(p: dict, ip: bool = True) -> str:
    """Connection + network summary for a PRECAPTURE row (a link-open with no
    OAuth behind it), off the `scored_*` context the gate persists at score time.

    Two lines: where the exit says they are, then who actually owns the address.
    The second line is the one that settles arguments — `AS328309 Globacom · no
    rDNS` explains a lost geo-trust that `fraud 82` on its own just looks
    arbitrary about.

    Rows scored before those columns existed have none of this; they fall back
    to the plain IP so the field never renders empty.
    """
    loc = ", ".join(v for v in (p.get("scored_city"), p.get("scored_region")) if v)
    trusted = bool(p.get("scored_geo_trust"))
    if loc and not trusted:
        loc += " (exit)"
    # Fall back to the CAPTURE-TIME facts (local GeoLite2, written the instant
    # the page posted) for anything the drain hasn't filled in yet. Alerts fire
    # ~2 minutes ahead of scoring, and a card that just says the IP tells an
    # operator nothing about who owns it. The raw IP always stays on the line.
    head = [x for x in (f"📍 {loc}" if loc else None,
                        p.get("scored_country") or p.get("cap_country"),
                        p.get("scored_isp") or p.get("cap_org")) if x]
    if ip:
        head.append(f"`{p.get('ip', '?')}`")
    lines = [" · ".join(head) if head else f"`{p.get('ip', '?')}`"]

    asn = p.get("scored_asn") or p.get("cap_asn")
    org = p.get("scored_org") or p.get("cap_org")
    cls = p.get("scored_conn_class") or p.get("cap_conn_class")
    # iCloud Private Relay egresses via Fastly/Cloudflare and publishes no PTR,
    # so it lands on conn_class 'none' and used to render as "no rDNS published"
    # — which reads like a gap in our data when it is actually a known, handled
    # case. Name it. It also keeps the unknown-network tripwire honest: relay
    # rows are excluded there, so the label and the alert agree.
    if p.get("scored_relay"):
        cls = "relay"
        # the org string IS "iCloud Private Relay", so keeping it alongside the
        # label prints the same words twice on one line
        if (org or "").strip().lower() == "icloud private relay":
            org = None
    if asn or org or cls:
        # IPQS echoes the bare IP in `host` when the range publishes no PTR —
        # printing that back as rDNS would read like a real hostname. Parsed, not
        # pattern-matched: an IPv6 address has hex letters and slips a [\d.:] test.
        host = (p.get("scored_host") or "").strip()
        try:
            ipaddress.ip_address(host)
            host = None
        except ValueError:
            host = host or None
        net = [x for x in (f"AS{asn}" if asn else None, org or None, host,
                           _CONN_CLASS.get(cls, cls)) if x]
        fraud = p.get("scored_fraud")
        if fraud is not None:
            net.append(f"fraud {fraud}")
        lines.append(" · ".join(net))
        flags = (p.get("scored_flags") or "").strip()
        if flags:
            lines.append(f"flags: {flags}")
        # The one case worth calling out: geo was discarded ONLY because nothing
        # identifies the owner as an eyeball network — no PTR to read, and the AS
        # isn't in MOBILE_ASNS. That's a coverage gap (Glo, Jio, MTN all land
        # here), not evidence of a tunnel, and it's the difference between "add
        # the ASN" and "they really are behind something". Deliberately silent
        # for a relay, a datacenter, or any range that DOES publish rDNS —
        # there the lost trust is the system working.
        # scored_relay only fills when the gate's own Private-Relay downgrade ran,
        # and that lives on the OAuth path (app.py), not in the precapture replay
        # — so recognise a relay egress here too rather than mislabel one as a
        # coverage gap. ASN 54113 = Apple, 13335 = the Cloudflare egress partner.
        relay = (bool(p.get("scored_relay"))
                 or p.get("scored_asn") in (54113, 13335)
                 or "icloud private relay" in (org or "").lower()
                 or "icloud private relay" in (p.get("scored_isp") or "").lower())
        if (not trusted and cls == "none" and not relay
                and "datacenter" not in flags and "Tor" not in flags):
            lines.append("-# geo dropped only because nothing identifies this AS as a "
                         "consumer/carrier network — an unlisted carrier looks the same "
                         "as a tunnel here")
    return "\n".join(lines)


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


class HoldReplyView(discord.ui.View):
    """Buttons on the mod-log card that carries a held member's own answer.

    The custom_id is STATIC so the view survives restarts; the target uid is read
    back out of the embed footer (`uid:<id>`), which keeps one registered view
    serving every card instead of one per member.
    """

    def __init__(self, cog=None):
        super().__init__(timeout=None)
        self.cog = cog

    @staticmethod
    def _uid_from(message: discord.Message):
        for e in message.embeds:
            m = re.search(r"uid:(\d+)", (e.footer.text or "") if e.footer else "")
            if m:
                return int(m.group(1))
        return None

    async def _guard(self, interaction: discord.Interaction):
        """Admin-only, and only usable while the cog is live."""
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "That's an admin action.", ephemeral=True)
            return None
        uid = self._uid_from(interaction.message)
        member = interaction.guild.get_member(uid) if uid else None
        if member is None:
            await interaction.response.send_message(
                "That member isn't in the server any more.", ephemeral=True)
            return None
        return member

    @discord.ui.button(label="Release + restore roles", style=discord.ButtonStyle.success,
                       emoji="✅", custom_id="altguard:holdreply_release")
    async def release_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        member = await self._guard(interaction)
        if member is None or not self.cog:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, restored, _aged, also, _failed = await self.cog._do_group_release(
            interaction.guild, interaction.user, member
        )
        roles = ", ".join(r.mention for r in restored) if restored else "no stored roles"
        extra = (" · also cleared " + ", ".join(a.mention for a in also)) if also else ""
        await interaction.followup.send(
            (f"✅ Released {member.mention}. Restored: {roles}.{extra}" if ok else
             f"⚠️ Couldn't fully restore {member.mention} — check permissions/hierarchy.{extra}"),
            ephemeral=True,
        )

    @discord.ui.button(label="Keep held", style=discord.ButtonStyle.secondary,
                       emoji="🔒", custom_id="altguard:holdreply_keep")
    async def keep_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        member = await self._guard(interaction)
        if member is None:
            return
        # No state change — this is an acknowledgement so the next mod knows the
        # card was read. The quarantine is already in place.
        await interaction.response.send_message(
            f"🔒 Left {member.mention} held. Nothing changed.", ephemeral=True)


class AltGuard(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.session: aiohttp.ClientSession | None = None
        # live, runtime-toggleable forced-gate flag (env is just the seed default)
        self.quarantine_on_join = QUARANTINE_ON_JOIN
        # observe mode: recent join timestamps per remote guild (raid heuristic)
        self._join_window = {}

    async def cog_load(self):
        qstore.init()
        # a persisted /altguard-gate toggle wins over the env default
        persisted = qstore.get_setting("quarantine_on_join")
        if persisted is not None:
            self.quarantine_on_join = persisted == "1"
        self.session = aiohttp.ClientSession()
        self.bot.add_view(VerifyPanel())  # persistent verify button — works after restarts
        self.bot.add_view(HoldReplyView(self))  # buttons on hold-reply cards
        self.poll_results.start()
        if ALMOST_ROLE_ID:
            self.sync_almost_verified.start()

    async def cog_unload(self):
        self.poll_results.cancel()
        if ALMOST_ROLE_ID:
            self.sync_almost_verified.cancel()
        if self.session:
            await self.session.close()

    # ------------------------------------------------------------------ quarantine
    def _removable_roles(self, member: discord.Member):
        """Roles we're allowed to strip: not @everyone, not the quarantine role,
        not managed (bot/booster/integration) roles, and below the bot's top role."""
        me = member.guild.me
        out = []
        for r in member.roles:
            # ALMOST_ROLE_ID is excluded for the same reason the quarantine role
            # is: we grant it TO quarantined members on purpose, and
            # on_member_update re-strips anything a quarantined member gains.
            # Without this it would be handed out and torn off every sync tick.
            if r.is_default() or r.id in (QUARANTINE_ROLE_ID, ALMOST_ROLE_ID) or r.managed:
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

    def _age_roles(self, guild, res):
        """(grant, [drop...]) for the age group picked on the verify page: the
        picked band's role, plus every OTHER age role to remove (mutual
        exclusivity). grant is None when the result carries no/invalid selection."""
        pick = (res or {}).get("age") or ""
        grant_id = AGE_ROLE_MAP.get(pick)
        grant = guild.get_role(grant_id) if grant_id else None
        drops = []
        for rid in ALL_AGE_ROLE_IDS:
            if rid != grant_id:
                r = guild.get_role(rid)
                if r is not None:
                    drops.append(r)
        return grant, drops

    def _has_age_role(self, member) -> bool:
        """True if the member already wears an age band — a returning member whose
        band was restored on release, or anyone who self-picked in #reaction-roles.
        Callers that apply a DEFAULT band must check this first: a default that
        stomps a real selection is worse than no default at all."""
        return any(r.id in ALL_AGE_ROLE_IDS for r in member.roles)

    async def _apply_age_role(self, guild, member, res):
        """Grant the picked age band (and drop every other age role) outside the
        release path — used for passes that lift no quarantine (re-verify,
        grandfathered)."""
        if not member:
            return
        grant, drops = self._age_roles(guild, res)
        me = guild.me
        if not grant or grant.managed or not me or grant >= me.top_role:
            return
        try:
            to_remove = [d for d in drops if d in member.roles]
            if to_remove:
                await member.remove_roles(*to_remove, reason="AltGuard: age selection (verify page)")
            if grant not in member.roles:
                await member.add_roles(grant, reason="AltGuard: age selection (verify page)")
        except discord.Forbidden:
            pass

    async def _release(self, member: discord.Member, res=None):
        """Remove quarantine role, restore the exact roles we removed, AND grant
        the opt-out default roles + the verify-page age role — in a single bulk edit."""
        qrole = member.guild.get_role(QUARANTINE_ROLE_ID)
        me = member.guild.me
        stored = qstore.pop(member.id)
        restore = []
        for rid in stored:
            r = member.guild.get_role(rid)
            if r and not r.managed and me and r < me.top_role:
                restore.append(r)
        # returning member: restore their safe pre-leave roles (self-assigned
        # reaction roles, age band, Level N+) from the departure snapshot. The
        # filter drops anything with power + the staff/access deny set, so a
        # former mod/admin never regains it, and doing it here (post-verify) means
        # a restore can't bypass the gate.
        bot_top = me.top_role if me else None
        for r in rejoin_roles.safe_restorable(
                member.guild,
                rejoin_roles.last_known_role_ids(member.id, member.guild.id),
                NO_RESTORE_ROLE_IDS, bot_top):
            if r not in restore:
                restore.append(r)
        # final set: current roles, minus quarantine, plus restored, plus defaults
        # "Almost Verified" is mid-gate access only — it must never outlive the
        # quarantine it was granted alongside, so it goes in the same bulk edit
        # rather than waiting for the next sync tick to notice.
        target = [r for r in member.roles
                  if not r.is_default() and r != qrole and r.id != ALMOST_ROLE_ID]
        for r in restore + self._default_roles(member.guild):
            if r not in target:
                target.append(r)
        # age band. A pick from the verify page is authoritative: add it and drop
        # every other age role (that's what beats a stale stored/restored band).
        # With NO pick we must not run those drops — they'd strip a band the member
        # legitimately had, which is how a manual /altguard-release used to leave
        # people with no age role at all. Instead: keep what they have, and stamp
        # ALTGUARD_DEFAULT_AGE only if they'd otherwise land bandless.
        aged = None
        grant, drops = self._age_roles(member.guild, res)
        if grant:
            target = [r for r in target if r not in drops]
            if grant not in target and not grant.managed and me and grant < me.top_role:
                target.append(grant)
        elif DEFAULT_AGE and not any(r.id in ALL_AGE_ROLE_IDS for r in target):
            dflt = member.guild.get_role(AGE_ROLE_MAP.get(DEFAULT_AGE, 0))
            if dflt and not dflt.managed and me and dflt < me.top_role:
                target.append(dflt)
                aged = DEFAULT_AGE
        try:
            await member.edit(roles=target, reason="AltGuard: quarantine cleared (restore + defaults)")
            # if the prune was holding off on them pending review, that's resolved
            qstore.unspare(member.id)
            return True, restore, aged
        except discord.Forbidden:
            return False, restore, None

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
        except discord.HTTPException as e:
            # Forbidden (closed DMs) is the usual one, but opening a DM with an
            # ex-user we share no server with comes back 400/50007, not 403 —
            # catching only Forbidden let that escape and kill the command.
            log.info("verify DM to %s failed: %s", user.id, e)
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
        if member.bot:
            return
        if member.guild.id != GUILD_ID:
            # Multi-server: everything below is the operator's own guild, wired
            # to env ids and a single-guild results poll. Other guilds get the
            # observe-only path — reporting, never a role.
            await self._observe_join(member)
            return
        # forced-gate: strip access on the way in, before anything else
        quarantined = False
        if self.quarantine_on_join:
            quarantined, _ = await self._quarantine(member, "awaiting verification (quarantine-on-join)")
            if not quarantined:
                log.warning("quarantine-on-join failed for %s — check Manage Roles + hierarchy", member)
        else:
            # detect-only mode: grant opt-out defaults right away, plus restore a
            # returning member's safe pre-leave roles. (Gated mode does both on
            # release instead, so the reconciliation listener doesn't strip them
            # while the member is held.)
            me = member.guild.me
            bot_top = me.top_role if me else None
            grants = list(self._default_roles(member.guild))
            for r in rejoin_roles.safe_restorable(
                    member.guild,
                    rejoin_roles.last_known_role_ids(member.id, member.guild.id),
                    NO_RESTORE_ROLE_IDS, bot_top):
                if r not in grants:
                    grants.append(r)
            if grants:
                try:
                    await member.add_roles(*grants, reason="AltGuard: defaults + returning-member restore")
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
        # skip while mid-onboarding: a member still in Discord's onboarding flow
        # hasn't landed in the server yet, so _on_onboarding_complete posts the
        # ping once they have — avoids a duplicate prompt.
        # NB (2026-07-25): rules screening ALSO sets `pending`, but unlike
        # onboarding it never hid anything — #verify is view=allow for
        # @everyone. Screened joiners were skipped here for no good reason.
        # Moot now that screening is off, but don't trust `pending` to mean
        # "can't see the server" if it ever comes back.
        # ...unless this guild has the greeting turned down (verify_ping). Read
        # the DELIVERY RECORD, not `status`: an already-issued rejoiner returns
        # "already issued …" here even when that original DM never landed.
        v = qstore.verification(member.id)
        dm_ok = bool(v and v.get("dm_delivered"))
        if (quarantined and VERIFY_CHANNEL_ID and not member.pending
                and _verify_ping_wanted(member.guild.id, dm_ok)):
            vch = member.guild.get_channel(VERIFY_CHANNEL_ID)
            if vch:
                try:
                    tail = ("we also DMed you the link" if dm_ok else
                            "we couldn't DM you, so this button is the way in")
                    await vch.send(
                        f"👋 {member.mention} — your access is held for a quick anti-raid check. "
                        f"Tap **🔒 Verify** above to unlock ({tail})."
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
                    panel = (f" or click the verify panel in <#{VERIFY_CHANNEL_ID}>"
                             if VERIFY_CHANNEL_ID else " or click the verify panel")
                    note.append("**DMs closed** — couldn't deliver link; "
                                f"tell them to run `/verify`{panel}")
                await ch.send(f"👀 AltGuard: {member.mention} (`{member.id}`) joined — {', '.join(note)}.")

    async def _observe_join(self, member: discord.Member):
        """Observe mode, for guilds that are not the operator's own.

        Reports join-time risk to THEIR mod-log and does nothing else: no
        quarantine, no DM, no role touched, ever. That promise is what makes it
        safe to switch on in someone else's server, and it is all we can honestly
        offer until the gate speaks per-guild (a remote member's verdict would
        currently be processed against the home guild).

        Silent on ordinary joins by design — a line on every member is noise,
        and noise is how a security log stops being read.
        """
        guild = member.guild
        cfg = get_config(guild.id)
        mode = agmode.effective_mode(guild.id, cfg, GUILD_ID)
        if mode == "off":
            return
        ids = agmode.resolve_ids(guild.id, cfg, GUILD_ID, {})
        ch = guild.get_channel(ids["modlog_channel_id"]) if ids.get("modlog_channel_id") else None
        if ch is None:
            return
        now = time.time()
        window = self._join_window.setdefault(guild.id, deque())
        window.append(now)
        while window and now - window[0] > OBSERVE_BURST_SECS:
            window.popleft()
        age_days = (discord.utils.utcnow() - member.created_at).days
        notes = agmode.join_risk_signals(
            age_days, member.avatar is not None, len(window), OBSERVE_BURST_SECS,
            min_age_days=MIN_ACCOUNT_AGE_DAYS)
        if not notes:
            return
        tail = ""
        if agmode.is_degraded(guild.id, cfg, GUILD_ID):
            tail = ("\n*Observing only — this server is configured to hold members, but "
                    "enforcement isn't switched on for it yet. Nobody has been held.*")
        try:
            await ch.send(f"👀 AltGuard (observing): {member.mention} (`{member.id}`) "
                          f"joined — {', '.join(notes)}.{tail}",
                          allowed_mentions=discord.AllowedMentions.none())
        except (discord.Forbidden, discord.HTTPException):
            pass

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
        # Partner verification roles are never re-stripped: a member who clears
        # the partner bot (carl-bot etc.) AFTER AltGuard held them would get the
        # role granted and instantly yanked — stuck in both systems forever.
        # Leaving it on is also the assist-mode signal that they passed there.
        # partner_role_ids() coerces and DROPS junk. Hand-rolling int() here (as
        # this line originally did) let one non-numeric entry — an admin typing
        # a bot's name instead of its id — raise straight out of the listener,
        # silently disabling autorole-race protection for every held member in
        # the guild until someone noticed. Third instance of that bug in a day.
        partner = agmode.partner_role_ids(get_config(after.guild.id))
        kept_partner = [r for r in strip if r.id in partner]
        strip = [r for r in strip if r.id not in partner]
        if kept_partner and not strip:
            ch0 = after.guild.get_channel(MODLOG_CHANNEL_ID)
            if ch0:
                await ch0.send(
                    f"🤝 {after.mention} (`{after.id}`) gained partner role "
                    f"{', '.join(r.mention for r in kept_partner)} while held — left in place.")
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
        # (subject to the same per-guild verify_ping setting as the join ping)
        if VERIFY_CHANNEL_ID and _verify_ping_wanted(
                member.guild.id, bool(v and v.get("dm_delivered"))):
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
        retried = None
        if not (v and v.get("dm_delivered")):
            retried = await self._dm_link(member, locked=True)
            qstore.record_issue(member.id, member.guild.id, retried)
        # The join line named them in the mod log seconds ago; a second note
        # saying the same member is still held is pure duplication. Only speak
        # up when this pass actually DID something a mod hasn't seen — i.e. we
        # retried a DM that never landed the first time.
        if retried is None:
            return
        ch = member.guild.get_channel(MODLOG_CHANNEL_ID)
        if ch:
            outcome = ("re-sent the verify DM." if retried else
                       "DM retry failed (DMs still closed) — pointed at the verify channel instead.")
            try:
                await ch.send(
                    f"🎬 AltGuard: {member.mention} (`{member.id}`) finished onboarding "
                    f"while held — {outcome}"
                )
            except discord.Forbidden:
                pass

    # ------------------------------------------------------------------ poller
    @tasks.loop(minutes=ALMOST_SYNC_MIN)
    async def sync_almost_verified(self):
        """Hand the 'Almost Verified' role to quarantined members whose link-open
        scored a clean, high-timing-confidence pass — and take it back when they
        stop being quarantined.

        The bar is deliberately the SAME one auto-approve uses: the gate's
        /api/clean-passes list, i.e. score_precapture.is_clean_pass — verdict
        pass, not via the operator downgrade, clean environment, no spoof, full
        confidence, corroborated geo, device match under the alt threshold,
        fraud under the review trigger, IPQS-sourced intel, and HIGH timing
        confidence. A merely-opened link earns nothing.

        Watchlisted uids are skipped, matching auto-approve. Chat in one channel
        is far weaker than a release, but a precapture has no OAuth binding, and
        the watchlist is where the motivated accounts are.

        Fails closed: gate unreachable -> nobody is granted anything.
        """
        if not (ALMOST_ROLE_ID and self.session and GATE_URL and SECRET):
            return
        guild = self.bot.get_guild(GUILD_ID)
        role = guild.get_role(ALMOST_ROLE_ID) if guild else None
        qrole = guild.get_role(QUARANTINE_ROLE_ID) if guild else None
        if not role or not qrole:
            return
        try:
            async with self.session.get(f"{GATE_URL}/api/clean-passes",
                                        headers=_hmac_headers(), timeout=10) as r:
                if r.status != 200:
                    log.warning("almost-verified sync: gate HTTP %s", r.status)
                    return
                rows = (await r.json()).get("candidates", [])
        except Exception as e:
            log.debug("almost-verified sync failed: %s", e)
            return
        clean = {str(x["target_uid"]) for x in rows}

        granted = 0
        for m in qrole.members:
            if m.bot or role in m.roles or str(m.id) not in clean:
                continue
            if qstore.is_watched(m.id):
                continue
            try:
                await m.add_roles(role, reason="AltGuard: clean high-confidence link-open — #verify chat access")
            except discord.HTTPException as e:
                log.warning("almost-verified: couldn't grant to %s: %s", m.id, e)
                continue
            granted += 1
            await self._almost_alert(guild, m, next((x for x in rows if str(x["target_uid"]) == str(m.id)), {}))
            try:
                await m.send(
                    f"You opened your verify link for **{guild.name}** and everything we could see "
                    f"looked clean — you just didn't finish the Discord login step.\n\n"
                    f"You can now **talk in <#{VERIFY_CHANNEL_ID}>**. Ask us anything there, "
                    f"including why that login screen looks the way it does — it's the normal "
                    f"Discord one, and we only ever see your username.\n\n"
                    f"**You won't be removed for taking your time.** Finishing verification is "
                    f"what unlocks the rest of the server, whenever you're ready."
                )
            except discord.HTTPException:
                pass
            await asyncio.sleep(2)

        # take it back the moment they stop being quarantined (verified, released
        # or re-quarantined elsewhere) — the role only ever means "mid-gate"
        revoked = 0
        for m in list(role.members):
            if qrole in m.roles:
                continue
            try:
                await m.remove_roles(role, reason="AltGuard: no longer mid-verification")
                revoked += 1
            except discord.HTTPException:
                pass
            await asyncio.sleep(2)
        # print, not log.info: cog loggers have no handler attached in this bot,
        # so log.info goes nowhere — which is why this loop looked dead while it
        # was in fact working. Only speaks when something actually changed.
        if granted or revoked:
            print("[almost-verified] granted %d, revoked %d (of %d clean-pass uids)"
                  % (granted, revoked, len(clean)), flush=True)

    @sync_almost_verified.before_loop
    async def _before_almost(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(60)  # let the member cache chunk first

    async def _almost_alert(self, guild, member, row):
        ch = self.bot.get_channel(MODLOG_CHANNEL_ID)
        if not ch:
            return
        e = discord.Embed(
            title="🗣️ Almost Verified — chat unlocked in #verify",
            color=0xE0A23B,
            description=(
                f"{member.mention} (`{member.id}`) opened their verify link, let the page "
                f"fingerprint them, and stopped at the Discord authorize screen. The gate scored "
                f"that open **clean** at **high timing confidence**, so they can now talk in "
                f"<#{VERIFY_CHANNEL_ID}> — and nowhere else.\n\n"
                f"They are **still quarantined**, and they are **off the {PRUNE_HOURS_HINT}h prune "
                f"clock** for as long as they hold this role — nudge them in <#{VERIFY_CHANNEL_ID}> "
                f"rather than waiting for a kick. Revoking the role puts them back on the clock."
            ),
        )
        e.add_field(name="🕒 Timing", value=(row.get("timing") or "—")[:256], inline=False)
        e.add_field(name="🌐 Connection", value=_precap_conn(row)[:1024], inline=False)
        e.set_footer(text="No OAuth binding — anti-nuke, LinkGuard and every other rule still apply")
        try:
            await ch.send(embed=e)
        except discord.HTTPException:
            pass

    async def _fetch_results(self, guild_id: str):
        """One guild's pending verdicts — ALWAYS scoped. An unscoped call
        returns every guild's rows, and acking those consumes results whose
        rightful reader is another server's poll (the pre-2b bug: a remote
        member's verdict was processed against the home guild)."""
        try:
            async with self.session.get(
                f"{GATE_URL}/api/results", params={"guild_id": guild_id},
                headers=_hmac_headers(), timeout=10
            ) as r:
                if r.status != 200:
                    return []
                data = await r.json()
                return data.get("results", [])
        except Exception as e:
            log.debug("poll failed for %s: %s", guild_id, e)
            return []

    async def _ack_results(self, uids, guild_id: str):
        """Ack one guild's copies only — the results PK is (uid, guild_id)."""
        if not uids:
            return
        try:
            async with self.session.post(
                f"{GATE_URL}/api/ack", headers=_hmac_headers(),
                json={"uids": uids, "guild_id": guild_id}, timeout=10
            ):
                pass
        except Exception as e:
            log.debug("ack failed for %s: %s", guild_id, e)

    def _security_ai_review(self, guild, res, facts, channel=None):
        """Paid sealed second opinion, when this guild holds a tier. Never
        blocks the poll — the SecurityAI cog runs the model in its own task
        and posts to the same log channel that carries the case."""
        cog = self.bot.get_cog("SecurityAI")
        if cog is None or guild is None:
            return
        ch = channel if channel is not None else guild.get_channel(MODLOG_CHANNEL_ID)
        cog.schedule_review(guild, res.get("uid"), res.get("signals") or [],
                            res.get("verdict"), facts, ch)

    @tasks.loop(seconds=10)
    async def poll_results(self):
        if not self.session or not GATE_URL:
            return
        guild = self.bot.get_guild(GUILD_ID)
        results = await self._fetch_results(str(GUILD_ID))
        if not results:
            await self._poll_remote_guilds()
            await self._poll_shares(guild)  # still surface link-sharing
            await self._poll_precaptures(guild)
            await self._refresh_precap_cards(guild)
            await self._poll_unknown_networks(guild)
            await self._poll_hold_replies(guild)
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
                    ok, restored, _aged = await self._release(member, res)
                    await self._released_alert(guild, member, res, restored, ok)
                else:
                    # no lock to lift, but the age pick still applies
                    await self._apply_age_role(guild, member, res)
                    # pass with NO join-lock to lift — grandfathered member (joined
                    # before the gate), a re-verify, or an already-cleared account.
                    # This path was previously SILENT in the mod log; record it so
                    # every pass leaves a trace.
                    await self._verified_note(guild, member, res)
                continue
            if member is None:
                continue
            _, removed = await self._quarantine(member, "verification flagged")

            # classify every fingerprint-matched account: in-server / banned / left / cleared
            cascaded, banned, left, cleared = [], [], [], []
            for alt_uid in res.get("alt_uids", []):
                aid = int(alt_uid)
                alt = guild.get_member(aid)
                if alt:
                    if qstore.is_cleared(aid):
                        # mod-trusted (released) — keep as a detector: the NEW account
                        # is still quarantined for review above, but never re-quarantine
                        # the cleared member. Just name them as the match.
                        cleared.append(alt)
                    elif not qstore.is_quarantined(alt.id):
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

            await self._alert(guild, member, res, removed, cascaded, banned, left, evaded, cleared)
            self._security_ai_review(guild, res, {
                "matched_account_banned_here": bool(banned),
                "matched_account_member_here": bool(cascaded or cleared),
                "matched_account_left": bool(left),
                "matched_account_cleared": bool(cleared),
            })

        await self._ack_results(acked, str(GUILD_ID))
        await self._poll_remote_guilds()
        await self._poll_shares(guild)
        await self._poll_precaptures(guild)
        await self._refresh_precap_cards(guild)
        await self._poll_unknown_networks(guild)
        await self._poll_hold_replies(guild)

    async def _poll_remote_guilds(self):
        """Phase 2b, bot half: each non-home guild with AltGuard switched on
        gets its own scoped poll, and its results are routed to IT — reported
        in its mod-log, quarantined (if enforcing) with its own role, acked as
        its own copy. The gate already redacts the payload server-side and
        recomputes the verdict from what the guild may see, so a foreign match
        can hold nobody here."""
        for g in self.bot.guilds:
            if g.id == GUILD_ID:
                continue
            cfg = get_config(g.id)
            if agmode.effective_mode(g.id, cfg, GUILD_ID) == "off":
                continue
            remote = await self._fetch_results(str(g.id))
            if not remote:
                continue
            acked = await self._process_remote_results(g, cfg, remote)
            await self._ack_results(acked, str(g.id))

    async def _process_remote_results(self, guild, cfg, results):
        """Handle one remote guild's verdicts under its own mode.

        observe (and any degraded enforcing mode): report only — no role is
        ever touched, that promise is what makes observe safe to run in a
        stranger's server. assist/gate with remote enforcement on: quarantine
        holds with THEIR configured role. Local facts for the reviewer come
        from rows this guild owns (its members, its ban list) — never from
        another server's history."""
        mode = agmode.effective_mode(guild.id, cfg, GUILD_ID)
        ids = agmode.resolve_ids(guild.id, cfg, GUILD_ID, {})
        ch = guild.get_channel(ids["modlog_channel_id"]) if ids.get("modlog_channel_id") else None
        acked = []
        for res in results:
            uid = str(res.get("uid") or "")
            if not uid:
                continue
            acked.append(uid)
            verdict = res.get("verdict")
            signals = res.get("signals") or []
            member = guild.get_member(int(uid)) if uid.isdigit() else None
            facts = {}
            for a in res.get("alt_uids") or []:
                try:
                    aid = int(a)
                except (TypeError, ValueError):
                    continue
                if guild.get_member(aid):
                    facts["matched_account_member_here"] = True
                else:
                    status = await self._ban_status(guild, aid)
                    if status == "banned":
                        facts["matched_account_banned_here"] = True
                    else:
                        facts["matched_account_left"] = True
            held = False
            if verdict != "pass" and member is not None and agmode.acts_on_roles(mode):
                role = guild.get_role(ids.get("quarantine_role_id") or 0)
                if role is not None:
                    try:
                        await member.add_roles(role, reason="AltGuard: verification flagged")
                        held = True
                    except (discord.Forbidden, discord.HTTPException):
                        pass
            if ch is not None:
                await self._remote_result_card(ch, uid, verdict, signals, res, held, mode)
            if verdict != "pass":
                self._security_ai_review(guild, res, facts, channel=ch)
        return acked

    async def _remote_result_card(self, ch, uid, verdict, signals, res, held, mode):
        passed = verdict == "pass"
        e = discord.Embed(
            title="✅ AltGuard: verification passed" if passed
            else "🚩 AltGuard: verification flagged",
            color=0x2ECC71 if passed else 0xE67E22,
            description=f"<@{uid}> (`{uid}`)",
        )
        lines = sai.signal_lines(signals)
        if lines:
            e.add_field(name="Signals", value="\n".join(lines)[:1024], inline=False)
        if res.get("redacted"):
            e.add_field(name="Scope", value=(
                "Only signals involving your own server are disclosable — "
                "nothing further exists for this case here."), inline=False)
        if not passed:
            action = ("Quarantined with your configured role — your mods decide the release."
                      if held else
                      "Observing only — nobody's roles were touched.")
            e.add_field(name="Action", value=action, inline=False)
        try:
            await ch.send(embed=e, allowed_mentions=discord.AllowedMentions.none())
        except (discord.Forbidden, discord.HTTPException):
            pass

    async def _poll_hold_replies(self, guild):
        """Surface what a HELD member said about their own device match.

        The gate asks this only on device-match holds; the answer is self-declared
        context for a human and never touched the verdict. Read it that way: a
        'yes' is worth a lot (someone volunteered a checkable explanation), a 'no'
        is worth almost nothing (innocent and evader both say no), and saying
        NOTHING is not evidence of anything.
        """
        if not self.session or not GATE_URL:
            return
        try:
            async with self.session.get(
                f"{GATE_URL}/api/hold-replies", headers=_hmac_headers(), timeout=10
            ) as r:
                if r.status != 200:
                    return
                data = await r.json()
        except Exception as e:
            log.debug("hold-reply poll failed: %s", e)
            return

        replies = data.get("replies", [])
        if not replies:
            return

        ids = []
        for rep in replies:
            ids.append(rep["id"])
            if guild:
                await self._hold_reply_alert(guild, rep)

        try:
            async with self.session.post(
                f"{GATE_URL}/api/hold-replies/ack", headers=_hmac_headers(),
                json={"ids": ids}, timeout=10,
            ):
                pass
        except Exception as e:
            log.debug("hold-reply ack failed: %s", e)

    async def _hold_reply_alert(self, guild, rep):
        ch = guild.get_channel(MODLOG_CHANNEL_ID)
        if not ch:
            return
        uid = int(rep["uid"])
        member = guild.get_member(uid)
        label = {
            "mine":   ("✅ “Yes — the other account is mine”", 0x57F287),
            "shared": ("👨‍👩‍👧 “It's a shared device — family, partner, roommate”", 0xFEE75C),
            "no":     ("❔ “No / not that I know of”", 0x95A5A6),
        }.get(rep.get("choice") or "", ("(unknown answer)", 0x95A5A6))

        embed = discord.Embed(
            title="💬 Held member replied",
            color=label[1],
            description=(f"{member.mention if member else f'<@{uid}>'} `{uid}` answered the "
                         f"device-match question on the verify page."),
        )
        embed.add_field(name="Answer", value=label[0], inline=False)
        note = (rep.get("note") or "").strip()
        if note:
            embed.add_field(name="In their words", value=f">>> {note[:1000]}", inline=False)
        embed.add_field(
            name="How to weigh it",
            value=("-# Self-declared and unscored. A **yes** is a checkable explanation; "
                   "a **no** rules nothing out either way. Verify against the match, don't "
                   "substitute this for it."),
            inline=False,
        )
        embed.set_footer(text=f"uid:{uid} · AltGuard hold reply")
        await ch.send(embed=embed, view=HoldReplyView(self))

    async def _poll_precaptures(self, guild):
        """Surface pre-auth landing-page captures whose DEVICE matched a known
        account — fires even for visitors who bail before completing OAuth. Loud
        @here when the matched account is on the watchlist."""
        try:
            async with self.session.get(
                f"{GATE_URL}/api/precaptures", headers=_hmac_headers(), timeout=10
            ) as r:
                if r.status != 200:
                    return
                rows = (await r.json()).get("precaptures", [])
        except Exception:
            return
        if not rows:
            return
        ch = guild.get_channel(MODLOG_CHANNEL_ID) if guild else None
        ids = []
        for p in rows:
            ids.append(p["id"])
            if not ch:
                continue
            embed, loud = self._precap_embed(p)
            msg = await ch.send(content="@here" if loud else None, embed=embed,
                                allowed_mentions=discord.AllowedMentions(everyone=True))
            # remember the card so it can be corrected once the drain scores the
            # row — the first render only ever knows what the local mmdb knew
            try:
                if msg is not None:
                    qstore.remember_precap_card(p["id"], ch.id, msg.id)
            except Exception:
                log.exception("could not track precapture card %s", p.get("id"))
        try:
            async with self.session.post(
                f"{GATE_URL}/api/precaptures/ack", headers=_hmac_headers(), json={"ids": ids}, timeout=10
            ):
                pass
        except Exception:
            pass

    def _precap_embed(self, p):
        """Build the mod-log card for one precapture row -> (embed, loud).

        Pure rendering, no I/O, deliberately: it is called once when the alert
        fires and AGAIN when the intel drain has scored the row, so a corrected
        card is the same code path rather than a second, subtly different one.
        """
        try:
            matches = json.loads(p.get("matches") or "[]")
        except (TypeError, ValueError):
            matches = []
        try:
            attrs = json.loads(p.get("attrs") or "{}")
        except (TypeError, ValueError):
            attrs = {}
        watched = [m for m in matches if qstore.is_watched(m["uid"])]
        top = p.get("top_pct", 0)
        match_txt = ", ".join(f"<@{m['uid']}> (`{m['uid']}` · {m['pct']}%)" for m in matches[:6]) or "—"
        loud = bool(watched)
        target = p.get("target_uid")
        # A row can reach us two ways: it matched a known device (the original
        # alert), or timing says the clicker IS the target and the gate replayed
        # a verdict for them (the "bailed at the Discord login" review queue).
        scored = p.get("scored_verdict")
        review_only = bool(scored) and not matches
        if loud:
            title, color = "🚨 WATCHED device opened a verify link", 0x8B0000
        elif review_only:
            title = "🕵️ Opened the link but never finished — scored for review"
            color = 0x3BA55D if scored == "pass" else 0xE0A23B
        else:
            title, color = "👁️ Link-open device matched a known account", 0xE0A23B
        if review_only:
            desc = (
                f"<@{target}> (`{target}`) opened their verify link and let the page finish, "
                f"then stopped at the Discord login. Timing says the clicker **is** them, so the "
                f"gate replayed their signals through the normal scorer."
            )
        else:
            desc = (
                f"A verify link **issued for** <@{target}> (`{target}`) was opened by a device that "
                f"matches **{len(matches)}** known account(s), up to **{top}%**. Captured on the trust "
                f"page **before** the Discord login — so this fires even if they never finish verifying."
            )
        embed = discord.Embed(title=title, color=color, description=desc)
        if scored:
            risk = p.get("scored_risk", 0)
            try:
                why = json.loads(p.get("scored_reasons") or "[]")
            except (TypeError, ValueError):
                why = []
            verdict_line = ("✅ **PASS**" if scored == "pass" else f"⚠️ **{str(scored).upper()}**")
            embed.add_field(
                name="⚖️ Replayed verdict (review only — nobody was released)",
                value=(f"{verdict_line} · risk **{risk}**\n"
                       + "\n".join(f"• {w}" for w in why[:4]))[:1024],
                inline=False)
        if matches or not review_only:
            embed.add_field(name="Device matches", value=match_txt[:1024], inline=False)
        if watched:
            embed.add_field(name="🚨 On your watchlist",
                            value=", ".join(f"<@{m['uid']}>" for m in watched)[:1024], inline=False)
        embed.add_field(name="🖥️ Device", value=_device_profile(attrs)[:1024], inline=False)
        conn = _precap_conn(p) + (f"\nJA4 `{p['ja4']}`" if p.get("ja4") else "")
        embed.add_field(name="🌐 Connection", value=conn[:1024], inline=False)
        if p.get("timing"):
            embed.add_field(name="🕒 Timing confidence", value=p["timing"][:1024], inline=False)
        embed.set_footer(
            text=("Replayed from stored signals · no OAuth binding, no velocity — "
                  "release with /altguard-release if you're satisfied"
                  if review_only else
                  "Pre-auth capture · unattributed — the opener may not be the link's target"))
        return embed, loud

    async def _refresh_precap_cards(self, guild):
        """Correct cards that were posted before the gate had scored the row.

        The alert fires within a second of the link-open; the intel drain scores
        on a timer (109s on the 2026-08-09 alert whose Connection line was a
        bare IPv6 while the gate went on to learn `AS7552 Viettel Group,
        residential`). We hold the message id, ask the gate for the row's
        CURRENT state, and re-render in place once `scored_ts` is set. Editing
        rather than re-posting is deliberate: a second card for one link-open
        reads like a second event.
        """
        cards = {c["precap_id"]: c for c in qstore.precap_cards_to_refresh()}
        if not cards:
            return
        try:
            async with self.session.post(
                f"{GATE_URL}/api/precaptures/refresh", headers=_hmac_headers(),
                json={"ids": list(cards)}, timeout=10
            ) as r:
                if r.status != 200:
                    return
                rows = (await r.json()).get("precaptures", [])
        except Exception:
            return
        for p in rows:
            card = cards.get(p.get("id"))
            if not card:
                continue
            if not p.get("scored_ts"):
                continue                      # not scored yet — try again later
            try:
                ch = guild.get_channel(int(card["channel_id"])) if guild else None
                if ch is None:
                    qstore.mark_precap_refreshed(p["id"])   # channel gone; stop retrying
                    continue
                msg = await ch.fetch_message(int(card["message_id"]))
                embed, _ = self._precap_embed(p)
                await msg.edit(embed=embed)
                qstore.mark_precap_refreshed(p["id"])
            except discord.NotFound:
                qstore.mark_precap_refreshed(p["id"])       # card deleted by a mod
            except Exception:
                log.exception("could not refresh precapture card %s", p.get("id"))

    async def _poll_unknown_networks(self, guild):
        """Surface connections the gate could NOT identify.

        Every conn_class condition in the gate is `== "hosting"`, so a network
        it cannot name is scored exactly like an ordinary home line — that is
        how furkankgzz's Oracle VPS passed on 2026-07-28. This does not hold
        anyone; it asks a human to name the network once, after which the ASN
        lists or the vendor vocabulary answer it permanently.

        Deliberately quiet: no @here, no ping. Its value is that it is normally
        silent — 8/9, the first full day on the consensus classifier, was 3 for
        3 classified — so when it does speak it means a network appeared that
        six vendors and every list have never seen. iCloud Private Relay is
        excluded gate-side; it is a deliberate exemption, not a blind spot.
        """
        try:
            async with self.session.get(
                f"{GATE_URL}/api/unknown-networks", headers=_hmac_headers(), timeout=10
            ) as r:
                if r.status != 200:
                    return
                rows = (await r.json()).get("unknown", [])
        except Exception:
            return
        if not rows:
            return
        ch = guild.get_channel(MODLOG_CHANNEL_ID) if guild else None
        acked = []
        for u in rows:
            acked.append({"kind": u["kind"], "row_id": u["row_id"]})
            if not ch:
                continue
            try:
                where = "verified" if u["kind"] == "result" else "opened a link"
                embed = discord.Embed(
                    title="❓ Unidentified network",
                    color=0x5865F2,
                    description=(
                        f"<@{u['subject']}> (`{u['subject']}`) {where} from a connection "
                        f"no ASN list, no rDNS pattern and no intel vendor could classify. "
                        f"**Nobody was held for this** — it is scored exactly like a home "
                        f"connection, which is the blind spot worth knowing about."))
                net = [x for x in (f"AS{u['asn']}" if u.get("asn") else None,
                                   u.get("isp") or None,
                                   (u.get("host") or None),
                                   f"class `{u.get('conn_class') or 'empty'}`") if x]
                embed.add_field(name="🌐 Connection",
                                value=" · ".join(net) + f"\n`{u.get('ip','?')}`", inline=False)
                if u.get("environment") and u["environment"] != "clean":
                    embed.add_field(name="🖥️ Environment", value=u["environment"], inline=False)
                embed.set_footer(text="Name it once and the lists answer it forever — "
                                      "/altguard-lookup for the full record")
                await ch.send(embed=embed)
            except Exception:
                log.exception("could not post unknown-network card")
        try:
            async with self.session.post(
                f"{GATE_URL}/api/unknown-networks/ack", headers=_hmac_headers(),
                json={"items": acked}, timeout=10
            ):
                pass
        except Exception:
            pass

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
        embed.add_field(name="Connection", value=_conn_line(res), inline=True)
        if res.get("age"):
            embed.add_field(name="Age group", value=res["age"], inline=True)
        if ok:
            embed.add_field(name="Roles restored", value=roles, inline=False)
        await ch.send(embed=embed)

    async def _verified_note(self, guild, member, res):
        """A pass that lifted no quarantine-on-join lock — grandfathered member,
        re-verify, or already-cleared account. Previously unlogged; recorded here
        so the mod log captures EVERY pass, not just forced-gate releases."""
        ch = guild.get_channel(MODLOG_CHANNEL_ID) if guild else None
        if not ch:
            return
        who = member.mention if member else f"<@{res['uid']}>"
        joined = getattr(member, "joined_at", None) if member else None
        since = f" · member since <t:{int(joined.timestamp())}:D>" if joined else ""
        embed = discord.Embed(
            title="✅ Verified — no lock to lift",
            color=0x3BA55D,
            description=(
                f"{who} `{res['uid']}` passed verification, but no quarantine-on-join "
                f"lock was held, so nothing was released. **Possibly grandfathered** "
                f"(joined before the gate) — could also be a re-verify or already "
                f"cleared.{since}"
            ),
        )
        embed.add_field(name="Top device match", value=f"{res.get('risk', 0)}%", inline=True)
        embed.add_field(name="Connection", value=_conn_line(res), inline=True)
        if res.get("age"):
            embed.add_field(name="Age group", value=res["age"], inline=True)
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
        embed.add_field(name="Connection", value=_conn_line(res, ip=True), inline=False)
        await ch.send(content="@here", embed=embed)

    @poll_results.before_loop
    async def _before_poll(self):
        await self.bot.wait_until_ready()

    async def _alert(self, guild, member, res, removed, cascaded, banned, left, evaded, cleared=None):
        ch = guild.get_channel(MODLOG_CHANNEL_ID)
        if not ch:
            return
        cleared = cleared or []
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
        embed.add_field(name="Connection", value=_conn_line(res), inline=True)
        embed.add_field(name="Why", value=reasons, inline=False)
        embed.add_field(name="🖥️ Device", value=_device_profile(res.get("attrs") or {})[:1024], inline=False)
        if banned:
            embed.add_field(name="🚨 Matches BANNED accounts", value=", ".join(f"<@{u}> (`{u}`)" for u in banned)[:1024], inline=False)
        if left:
            embed.add_field(name="Matches accounts that left", value=", ".join(f"<@{u}> (`{u}`)" for u in left)[:1024], inline=False)
        embed.add_field(name="Alts in-server also quarantined", value=cascade_txt, inline=False)
        if cleared:
            embed.add_field(
                name="✅ Matches a CLEARED member (not re-quarantined)",
                value=", ".join(f"{m.mention} (`{m.id}`)" for m in cleared)[:1024] +
                      "\n-# this new account was held for review *because* it matches a trusted member's device",
                inline=False,
            )
        if not evaded:
            embed.add_field(name="Roles removed (stored for restore)", value=removed_txt, inline=False)
        embed.set_footer(text="False positive? /altguard-release @user restores their exact roles")
        await ch.send(embed=embed)

    # ------------------------------------------------------------------ commands
    @app_commands.command(name="verify", description="Get your own verification link")
    async def verify(self, interaction: discord.Interaction):
        url = _verify_link(interaction.user.id, interaction.guild_id)
        qstore.record_issue(interaction.user.id, interaction.guild_id, True)
        await interaction.response.send_message(
            "👋 Tap below to verify yourself — it's a quick automated check, "
            "nothing needed from you. You're unlocked the moment it passes.",
            view=VerifyView(url), ephemeral=True,
        )

    @app_commands.command(
        name="altguard-verify-panel",
        description="Post the click-to-verify button in this channel (admin)",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
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
    @app_commands.checks.has_permissions(administrator=True)
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
    @app_commands.checks.has_permissions(administrator=True)
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
        quarantine="True = strip their roles + hold them until they pass (in-server only)",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def check(self, interaction: discord.Interaction,
                    user: discord.User = None, user_id: str = None, dm: bool = True,
                    quarantine: bool = False):
        await interaction.response.defer(ephemeral=True, thinking=True)
        uid = str(user.id) if user else (user_id or "").strip()
        if not uid.isdigit():
            await interaction.followup.send("Give me a member, or a numeric `user_id` for an ex-user.", ephemeral=True)
            return
        if user and user.bot:
            await interaction.followup.send("That's a bot — nothing to check.", ephemeral=True)
            return

        member_obj = interaction.guild.get_member(int(uid))
        in_server = member_obj is not None
        target = user
        if target is None:
            try:
                target = await self.bot.fetch_user(int(uid))
            except discord.HTTPException:
                target = None

        url = _verify_link(int(uid), interaction.guild_id)
        # optionally hold them until they pass — strip + store roles, apply quarantine
        q_status = ""
        if quarantine:
            if member_obj is None:
                q_status = "\n⚠️ Can't quarantine — they're not in the server."
            else:
                ok, removed = await self._quarantine(member_obj, "manual /altguard-check quarantine")
                q_status = (
                    f"\n🔒 Quarantined — stripped {len(removed)} role(s); `/altguard-release` to undo."
                    if ok else "\n⚠️ Quarantine failed — check my Manage Roles permission / role order."
                )
        dmed = False
        if dm and target is not None:
            dmed = await self._dm_user(target, interaction.guild, locked=quarantine)
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
            f"Verification requested for <@{uid}> (`{uid}`){tag}.\n{status}\n`{url}`{q_status}\n"
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
            if quarantine:
                audit.add_field(name="Quarantine", value="🔒 held until they pass" if in_server else "—", inline=True)
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
    @app_commands.checks.has_permissions(administrator=True)
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
            embed.add_field(name="Connection", value=_conn_line(res, ip=True), inline=False)
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

    async def _alt_group(self, uid: int, cap: int = 12) -> set:
        """The confirmed-alt GROUP for uid: gate device matches at/above the
        detection bar (RELEASE_MATCH_PCT), followed transitively so A-B-C twins
        resolve even when A only matched B. Plus reason-links from the store
        ("alt of X" in either direction) as a net for fingerprint drift. Never
        raises — a dead gate just shrinks the group to the reason-links."""
        seen, frontier, looked = {uid}, [uid], 0
        while frontier and looked < cap:
            cur = frontier.pop(0)
            looked += 1
            try:
                async with self.session.get(
                    f"{GATE_URL}/api/lookup", params={"uid": str(cur)},
                    headers=_hmac_headers(), timeout=10,
                ) as r:
                    data = await r.json()
            except Exception as e:
                log.warning("alt-group lookup failed for %s: %s", cur, e)
                continue
            for m in data.get("matches", []):
                try:
                    aid, pct = int(m["uid"]), int(m.get("pct", 0))
                except (KeyError, TypeError, ValueError):
                    continue
                if pct >= RELEASE_MATCH_PCT and aid not in seen:
                    seen.add(aid)
                    frontier.append(aid)
        # reason-links: members cascade-quarantined FROM anyone in the group,
        # and whoever a group member was itself cascaded from.
        for gid_ in list(seen):
            for aid in qstore.quarantined_alts_of(gid_):
                seen.add(int(aid))
            mt = re.search(r"alt of (\d+)", qstore.quarantine_reason(gid_) or "")
            if mt:
                seen.add(int(mt.group(1)))
        return seen

    async def _do_group_release(self, guild, actor, member: discord.Member):
        """Release one member + every still-held account fingerprint-linked to them.

        Extracted from /altguard-release so the hold-reply mod-log card can offer
        the exact same action from a button — one release path, not two.
        Returns (ok, restored, aged, also, failed).
        """
        qrole = guild.get_role(QUARANTINE_ROLE_ID)

        # Releasing one confirmed alt vouches for the whole group: find every
        # fingerprint-linked account BEFORE releasing (release pops stored rows).
        group = await self._alt_group(member.id)

        ok, restored, aged = await self._release(member)
        # Mark trusted: their device (if on file) stays a live detector — a NEW
        # account matching it is still quarantined for review — but the alt-cascade
        # will never re-quarantine this member again.
        qstore.clear(member.id, f"released by {actor}")

        # Cascade-release: every in-server member of the group still held goes
        # out with them. This does NOT whitelist the device — a future alt still
        # gets quarantined on join and stays held on a fail verdict.
        also, failed = [], []
        for aid in group:
            if aid == member.id:
                continue
            alt = guild.get_member(aid)
            if not alt:
                continue
            held = qstore.is_quarantined(aid) or (qrole and qrole in alt.roles)
            if not held:
                continue
            a_ok, _, _ = await self._release(alt)
            qstore.clear(aid, f"released by {actor} (group release with {member.id})")
            (also if a_ok else failed).append(alt)

        if also or failed:
            await self._group_release_note(guild, actor, member, also, failed)
        return ok, restored, aged, also, failed

    @app_commands.command(
        name="altguard-release",
        description="Clear a quarantine and restore removed roles — releases the whole matched-alt group",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def release(self, interaction: discord.Interaction, member: discord.Member):
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, restored, aged, also, failed = await self._do_group_release(
            interaction.guild, interaction.user, member
        )

        roles = ", ".join(r.mention for r in restored) if restored else "no stored roles"
        age_note = (f"\n-# No age band on file — defaulted to **{aged}**; they can change it themselves."
                    if aged else "")
        group_note = ""
        if also:
            group_note = ("\n🔗 Group release — also cleared: "
                          + ", ".join(a.mention for a in also))
        if failed:
            group_note += ("\n⚠️ Couldn't restore (permissions/hierarchy): "
                           + ", ".join(a.mention for a in failed))
        if ok:
            await interaction.followup.send(
                f"✅ Cleared quarantine on {member.mention}. Restored: {roles}.{age_note}{group_note}\n"
                f"-# Marked trusted — they won't be re-flagged, but any NEW account matching their device is still held for review.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"⚠️ Couldn't fully restore {member.mention} — check my permissions/role hierarchy.{group_note}",
                ephemeral=True,
            )

    async def _group_release_note(self, guild, mod, member, also, failed):
        """Durable mod-log record of a cascade release — who vouched, for whom."""
        ch = guild.get_channel(MODLOG_CHANNEL_ID)
        if not ch:
            return
        embed = discord.Embed(
            title="🔗 Alt-group released",
            description=(
                f"{mod.mention} released {member.mention} — the linked accounts below "
                f"were cleared with them as one confirmed-alt group."
            ),
            color=0x57F287,
        )
        if also:
            embed.add_field(
                name="Also released",
                value=", ".join(f"{a.mention} (`{a.id}`)" for a in also)[:1024],
                inline=False,
            )
        if failed:
            embed.add_field(
                name="⚠️ Release failed (permissions/hierarchy)",
                value=", ".join(f"{a.mention} (`{a.id}`)" for a in failed)[:1024],
                inline=False,
            )
        embed.set_footer(text="Device stays a live detector — new matching accounts are still held on join.")
        try:
            await ch.send(embed=embed)
        except discord.Forbidden:
            pass

    @app_commands.command(
        name="altguard-watch",
        description="Watchlist a (banned) account — loud alert if they ever verify or an alt matches them",
    )
    @app_commands.describe(user_id="raw Discord ID to watch", reason="why (e.g. 'banned raider')")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
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
    @app_commands.checks.has_permissions(administrator=True)
    async def unwatch(self, interaction: discord.Interaction, user_id: str):
        ok = qstore.unwatch(user_id.strip())
        await interaction.response.send_message(
            f"{'✅ Removed' if ok else '⚠️ Not on'} the watchlist: `{user_id}`.", ephemeral=True
        )

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            msg = "❌ You need the **Administrator** permission to use this."
        else:
            # Defining this handler suppresses discord.py's own logging
            # (tree.on_error stays quiet when a command has any handler), so an
            # unhandled error used to vanish twice over: no reply and no
            # traceback — a deferred command just hung as "app didn't respond".
            cmd = getattr(interaction.command, "name", "?")
            log.error("unhandled error in /%s", cmd, exc_info=error)
            msg = f"⚠️ `/{cmd}` errored: `{type(error).__name__}: {error}` — traceback is in the bot log."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass


async def setup(bot: commands.Bot):
    missing = [n for n, v in (
        ("ALTGUARD_SECRET", SECRET), ("ALTGUARD_GATE_URL", GATE_URL),
        ("ALTGUARD_GUILD_ID", GUILD_ID), ("ALTGUARD_QUARANTINE_ROLE_ID", QUARANTINE_ROLE_ID),
    ) if not v]
    if missing:
        raise RuntimeError("AltGuard env not configured: " + ", ".join(missing))
    await bot.add_cog(AltGuard(bot))
