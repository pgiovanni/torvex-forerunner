"""link_guard — detects canary-token & IP-grabber links in chat.

The threat: a link that logs the clicker's IP (Grabify/iplogger vanity domains)
or fires a Thinkst canary token. The nasty variant we purple-team against is the
"hidden embed" — the link is posted so Discord UNFURLS it (an image embed) while
the tracker domain never appears as clickable text: a markdown-masked link with
blank link text `[⠀](https://grabify.link/x.jpg)`, or an innocent-looking link
that Discord proxies an image from the tracker for. The image renders, the URL
hides.

Three surfaces are covered that Discord AutoMod (keyword-only) can't:
  1. raw content (incl. scheme-less `grabify.link/abc`)   — content vector
  2. markdown-masked links `[text](url)`                  — masked vector
  3. embed fields added when Discord unfurls a link, incl. the proxied image URL
     (which encodes the origin domain in its path)         — embed vector

Detection runs on BOTH on_message (immediate) and on_raw_message_edit (Discord
adds the unfurl embed via a later MESSAGE_UPDATE — no message cache or API fetch
needed; the new embeds ride in the raw payload). A hit found only in the embed
and NOT in the message text is the exact "hidden behind another domain" attack —
flagged specially in the alert.

Matching mirrors the operator's AutoMod wildcards: a hitlist entry WITH a dot
matches by hostname suffix AND raw substring; an entry WITHOUT a dot (e.g.
`canarytokens`, `shorturl`) is a pure substring rule. All text is URL-unquoted
first, so a proxied `https%3A%2F%2Fgrabify.link...` still matches.

Per-guild + opt-in + shadow-first, exactly like anti-nuke: runs only where
`linkguard_enabled`; SHADOW alerts only; ENFORCE deletes the message + times out
the poster. Never acts on: guild owner, the bot, or the guild whitelist. Webhook/
bot posts ARE scanned and (enforce) deleted, but can't be timed out.

Base hitlist: data/link_hitlist.json. Verification of new domains is DNS-only —
the cog NEVER makes an HTTP request to a suspected tracker (that would fire it).
"""
import asyncio
import datetime
import json
import logging
import os
import re
import sys
import time
from urllib.parse import unquote, urlparse

import discord
from discord import app_commands
from discord.ext import commands

import quarantine_store as qstore  # shared with AntiNuke/AltGuard — /altguard-release restores

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from utils.security_config import get_config, set_config, is_enabled

log = logging.getLogger("link_guard")

_DATA = os.path.join(os.path.dirname(__file__), "..", "data", "link_hitlist.json")

# URL-ish token (with or without scheme, or bare www.) — greedy up to whitespace
# or a delimiter. Used to pull hostnames for suffix matching + vector labelling.
_URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>()\[\]{}\"'`|\\]+", re.I)
# markdown masked link: [visible text](url) — capture the url target.
_MASK_RE = re.compile(r"\[[^\]]*\]\(\s*<?\s*((?:https?://|www\.)[^)\s>]+)", re.I)
# whole masked construct — stripped from "visible content" so a masked-only link
# doesn't count as a visible (content) hit (its URL isn't rendered to readers).
_MASK_STRIP_RE = re.compile(r"\[[^\]]*\]\([^)]*\)")


# ----------------------------------------------------------------- pure logic
def normalize_rule(d):
    """Lowercase, strip a leading wildcard/dot and a leading www."""
    d = (d or "").strip().lower().strip("*").strip(".")
    if d.startswith("www."):
        d = d[4:]
    return d


def hostname_of(token):
    """Best-effort hostname for a URL-ish token (handles scheme-less)."""
    t = (token or "").strip().strip("<>").rstrip(".,);]}'\"")
    if "://" not in t:
        t = "http://" + t
    try:
        return (urlparse(t).hostname or "").lower()
    except ValueError:
        return ""


def load_base_domains(path=_DATA):
    """Flatten the categorised JSON corpus into a de-duped rule list."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        log.warning("link_guard: could not load base hitlist (%s) — running empty", e)
        return []
    out, seen = [], set()
    for key, vals in data.items():
        if key.startswith("_") or not isinstance(vals, list):
            continue
        for d in vals:
            r = normalize_rule(d)
            if r and r not in seen:
                seen.add(r)
                out.append(r)
    return out


def load_shortener_rules(path=_DATA):
    """The 'shorteners' category — these have legit uses, so a hit on ONLY these
    (bit.ly/tinyurl/shorturl) is treated as LOW severity (gentle response)."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return set()
    return {normalize_rule(d) for d in data.get("shorteners", []) if normalize_rule(d)}


def classify_severity(findings, shortener_rules):
    """HIGH if any hit is a real tracker/canary domain OR used the hidden-embed
    trick; LOW if the ONLY hits are URL shorteners (possible legit member).
    Returns "high" or "low"."""
    shortener_rules = set(shortener_rules or ())
    for rule, meta in findings.items():
        if meta.get("hidden"):
            return "high"
        if rule not in shortener_rules:
            return "high"
    return "low"


def _flatten(obj, acc):
    """Collect every string value from a nested embed dict/list into acc."""
    if isinstance(obj, str):
        acc.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            _flatten(v, acc)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _flatten(v, acc)


def scan(content, embed_dicts, domains, allow=()):
    """Core detector. Returns {rule: {"vectors": set(...), "hidden": bool}}.

    content       raw message content (may be "")
    embed_dicts   list of embed dicts (discord.Embed.to_dict() or raw payload embeds)
    domains       iterable of hitlist rules (already or not yet normalized)
    allow         iterable of domains to suppress (per-guild allowlist)
    """
    content = content or ""
    allow = {normalize_rule(a) for a in allow if normalize_rule(a)}
    rules = []
    for d in domains:
        r = normalize_rule(d)
        if r and r not in allow:
            rules.append(r)

    embed_strings = []
    for e in (embed_dicts or []):
        _flatten(e, embed_strings)
    embed_joined = " \n ".join(embed_strings)

    masked = _MASK_RE.findall(content)
    masked_joined = " \n ".join(masked)

    # hostnames (for precise suffix matching) from every URL-ish token we can see
    tokens = set(_URL_RE.findall(content))
    tokens.update(_URL_RE.findall(embed_joined))
    tokens.update(masked)
    hostnames = {h for h in (hostname_of(t) for t in tokens) if h}

    # unquoted lowered blobs for substring matching (covers scheme-less, proxied,
    # percent-encoded origins, and bare-token rules). The content blob strips the
    # masked-link targets so a masked-only link registers as "masked" (hidden),
    # not "content" (visible).
    blob_content = unquote(_MASK_STRIP_RE.sub(" ", content)).lower()
    blob_embed = unquote(embed_joined).lower()
    blob_masked = unquote(masked_joined).lower()

    def host_allowed(h):
        return any(h == a or h.endswith("." + a) for a in allow)

    findings = {}
    for rule in rules:
        vectors = set()
        has_dot = "." in rule
        # precise: hostname suffix (only for dotted rules)
        if has_dot:
            for h in hostnames:
                if (h == rule or h.endswith("." + rule)) and not host_allowed(h):
                    vectors.add("link")
        # substring across each source blob
        if rule in blob_masked:
            vectors.add("masked")
        if rule in blob_embed:
            vectors.add("embed")
        if rule in blob_content:
            vectors.add("content")
        if vectors:
            # "hidden": present in the unfurled embed but NOT in the visible text
            # or a masked-link target with the domain absent from plain content.
            hidden = (("embed" in vectors or "masked" in vectors)
                      and "content" not in vectors)
            findings[rule] = {"vectors": vectors, "hidden": hidden}
    return findings


def defang(s):
    """Render a URL/domain un-clickable for the mod-log."""
    return (s or "").replace("http", "hxxp").replace(".", "[.]")


# The public "gotcha" — laughing gifs + taunt line dropped in-channel on a
# confirmed catch (HIGH severity, enforce only). Tenor URLs autoplay inline in
# Discord; klipy page links don't. Override per-guild via linkguard_taunt_gifs /
# linkguard_taunt_text. These two match the memes Paul picked (boo-boo-this-man +
# dedsec logo).
DEFAULT_TAUNT_GIFS = [
    "https://tenor.com/view/boo-boo-this-man-boohoo-tongue-out-tongue-sticking-out-gif-10617493753048617662",
    "https://tenor.com/view/dedsec-dedsec-logo-watchdogs-2-watch-dogs-watchdogs-gif-16403421894979992946",
]
DEFAULT_TAUNT_TEXT = "we caught you 😈"


# --------------------------------------------------------------------- the cog
class LinkGuard(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.base = load_base_domains()
        self.shortener_rules = load_shortener_rules()
        # dedupe so on_message + the later unfurl edit don't double-alert the same
        # domain on the same message. {message_id: (expiry_ts, set(rules))}
        self._seen = {}
        # message ids we've already punished, so the on_message + unfurl-edit
        # passes don't double-punish (dedupe of ACTIONS, separate from alerts).
        self._punished = {}
        log.info("link_guard: loaded %d base domains (%d shorteners)",
                 len(self.base), len(self.shortener_rules))

    # ------------------------------------------------------------- helpers
    def _domains_for(self, cfg):
        return list(self.base) + list(cfg.get("linkguard_extra_domains") or [])

    def _exempt(self, guild, user_id, cfg):
        if user_id is None:
            return False
        wl = set(cfg.get("whitelist") or [])
        return user_id == self.bot.user.id or user_id == guild.owner_id or user_id in wl

    def _modlog(self, guild, cfg):
        mid = cfg.get("modlog_channel_id")
        return guild.get_channel(int(mid)) if mid else None

    def _fresh(self, message_id, rules):
        """Return only rules not already alerted for this message (TTL dedupe)."""
        now = time.time()
        # prune
        if len(self._seen) > 4096:
            self._seen = {k: v for k, v in self._seen.items() if v[0] > now}
        exp, seen = self._seen.get(message_id, (now + 900, set()))
        new = {r for r in rules if r not in seen}
        seen.update(new)
        self._seen[message_id] = (now + 900, seen)
        return new

    # ------------------------------------------------------------- listeners
    @commands.Cog.listener()
    async def on_message(self, message):
        if message.guild is None:
            return
        await self._process(
            guild=message.guild,
            channel=message.channel,
            message_id=message.id,
            author_id=(message.author.id if message.author else None),
            content=message.content or "",
            embed_dicts=[e.to_dict() for e in message.embeds],
            is_webhook=bool(message.webhook_id),
            message=message,
        )

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload):
        # The unfurl embed Discord adds after the fact arrives here. The new
        # content/embeds ride in payload.data — no cache or API fetch required.
        if payload.guild_id is None:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        data = payload.data or {}
        author = data.get("author") or {}
        author_id = int(author["id"]) if author.get("id") else None
        channel = guild.get_channel(payload.channel_id)
        if channel is None:
            return
        await self._process(
            guild=guild,
            channel=channel,
            message_id=payload.message_id,
            author_id=author_id,
            content=data.get("content") or "",
            embed_dicts=data.get("embeds") or [],
            is_webhook=bool(data.get("webhook_id")),
            message=None,  # act via a partial message
        )

    def _already_punished(self, message_id):
        """True if this message already triggered enforcement (across the
        on_message + unfurl-edit passes). Records + prunes on first call."""
        now = time.time()
        if len(self._punished) > 4096:
            self._punished = {k: v for k, v in self._punished.items() if v > now}
        if self._punished.get(message_id, 0) > now:
            return True
        self._punished[message_id] = now + 900
        return False

    # ------------------------------------------------------------- core
    async def _process(self, *, guild, channel, message_id, author_id, content,
                        embed_dicts, is_webhook, message):
        if not is_enabled(guild.id, "linkguard"):
            return
        cfg = get_config(guild.id)
        # exempt trusted humans (but always scan webhook/bot posts)
        if not is_webhook and self._exempt(guild, author_id, cfg):
            return
        findings = scan(content, embed_dicts, self._domains_for(cfg),
                        cfg.get("linkguard_allow_domains") or [])
        if not findings:
            return
        fresh = self._fresh(message_id, findings.keys())
        if not fresh:
            return
        findings = {r: findings[r] for r in fresh}
        severity = classify_severity(findings, self.shortener_rules)
        enforce = bool(cfg.get("linkguard_enforce"))

        acts = {"deleted": False, "timed_out": False, "taunted": False,
                "quarantine_scheduled": False}
        # only ACT once per message even though we scan it twice (post + unfurl)
        if enforce and not self._already_punished(message_id):
            acts["deleted"] = (await self._delete(channel, message, message_id)
                               if cfg.get("linkguard_delete") else False)
            member = guild.get_member(author_id) if author_id else None
            actionable = member is not None and not self._exempt(guild, author_id, cfg)
            if severity == "high":
                if actionable:
                    acts["timed_out"] = await self._timeout(
                        member, cfg.get("linkguard_catch_timeout_min", 60), findings)
                if cfg.get("linkguard_taunt") and not is_webhook:
                    acts["taunted"] = await self._taunt(channel, author_id, cfg)
                if cfg.get("linkguard_quarantine") and actionable:
                    delay = int(cfg.get("linkguard_quarantine_delay_sec", 600))
                    reason = "canary/IP-grabber link: " + ", ".join(sorted(findings))
                    asyncio.create_task(
                        self._delayed_quarantine(guild.id, author_id, delay, reason))
                    acts["quarantine_scheduled"] = True
            else:  # low severity (shortener-only) — gentle
                if actionable:
                    acts["timed_out"] = await self._timeout(
                        member, cfg.get("linkguard_timeout_min", 10), findings)
        await self._alert(guild, cfg, channel, author_id, is_webhook,
                          findings, enforce, severity, acts)

    async def _delete(self, channel, message, message_id):
        try:
            if message is not None:
                await message.delete()
            else:
                await channel.get_partial_message(message_id).delete()
            return True
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            return False

    async def _timeout(self, member, minutes, findings):
        why = "canary/IP-grabber link: " + ", ".join(sorted(findings))
        try:
            await member.timeout(datetime.timedelta(minutes=int(minutes)),
                                 reason=f"LinkGuard: {why}"[:400])
            return True
        except (discord.Forbidden, discord.HTTPException):
            return False

    async def _taunt(self, channel, author_id, cfg):
        """Public gotcha in the channel it happened: the taunt line + laughing
        gifs (Tenor links autoplay inline)."""
        gifs = cfg.get("linkguard_taunt_gifs") or DEFAULT_TAUNT_GIFS
        text = cfg.get("linkguard_taunt_text") or DEFAULT_TAUNT_TEXT
        ping = f"<@{author_id}> " if author_id else ""
        try:
            await channel.send(
                f"🚨 {ping}**{text}** — that was an IP-grabber / canary link. Nice try.",
                allowed_mentions=discord.AllowedMentions(users=True))
            for url in gifs:      # separate messages so each gif autoplays
                await channel.send(url)
            return True
        except (discord.Forbidden, discord.HTTPException):
            return False

    async def _quarantine(self, guild, member, reason, cfg):
        """Strip removable roles (saved for restore) + apply the quarantine role.
        Reversible via /altguard-release. Mirrors AntiNuke's mechanism."""
        qid = cfg.get("quarantine_role_id")
        qrole = guild.get_role(int(qid)) if qid else None
        me = guild.me
        removable = [r for r in member.roles
                     if not (r.is_default() or r.managed or (qid and r.id == int(qid)))
                     and not (me and r >= me.top_role)]
        try:
            qstore.save(member.id, guild.id, [r.id for r in removable], f"link-guard: {reason}")
        except Exception:
            pass
        target = [r for r in member.roles if r not in set(removable)]
        if qrole and qrole not in target:
            target.append(qrole)
        try:
            await member.edit(roles=target, reason=f"LinkGuard: {reason} — quarantined"[:400])
            return True
        except (discord.Forbidden, discord.HTTPException):
            return False

    async def _delayed_quarantine(self, guild_id, member_id, delay, reason):
        """Quarantine `delay` seconds after the catch (theatrics — timeout first,
        then lock them out anyway). In-memory: a bot restart cancels a pending
        quarantine (the 1h timeout already contains them meanwhile)."""
        try:
            await asyncio.sleep(max(0, delay))
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                return
            cfg = get_config(guild_id)
            member = guild.get_member(member_id)
            if member is None or self._exempt(guild, member_id, cfg):
                return
            ok = await self._quarantine(guild, member, reason, cfg)
            ch = self._modlog(guild, cfg)
            if ch:
                await ch.send(
                    content="@here",
                    embed=discord.Embed(
                        color=0x8B0000 if ok else 0xE0A23B,
                        title="🔒 LinkGuard — offender quarantined" if ok
                        else "⚠️ LinkGuard — quarantine FAILED",
                        description=(f"<@{member_id}> (`{member_id}`) is now locked out "
                                     f"(roles stripped, saved for restore). **Review and "
                                     f"ban manually** if warranted — reverse with "
                                     f"`/altguard-release`." if ok else
                                     f"Couldn't quarantine <@{member_id}> — check my "
                                     f"perms/role hierarchy.")))
        except Exception as e:
            log.warning("link_guard delayed quarantine failed: %s", e)

    def _ping_prefix(self, cfg):
        val = str(cfg.get("linkguard_ping", "here")).strip().lower()
        if val == "everyone":
            return "@everyone"
        if val == "none" or not val:
            return None
        if val == "here":
            return "@here"
        if val.isdigit():
            return f"<@&{val}>"
        return "@here"

    async def _alert(self, guild, cfg, channel, author_id, is_webhook,
                     findings, enforce, severity, acts):
        ch = self._modlog(guild, cfg)
        if ch is None:
            return
        hidden = any(f["hidden"] for f in findings.values())
        high = severity == "high"
        if not enforce:
            head, color = "🎣 LinkGuard would trip (shadow)", 0xE0A23B
        elif high:
            head = "🎣 LinkGuard — CAUGHT a grabber/canary link" + (" 🎭" if hidden else "")
            color = 0x8B0000
        else:
            head, color = "🎣 LinkGuard — URL shortener removed (low severity)", 0xE0A23B
        who = f"webhook/bot in {channel.mention}" if is_webhook \
            else (f"<@{author_id}> (`{author_id}`)" if author_id else "unknown")
        embed = discord.Embed(title=head, color=color,
                              description=f"Posted by {who}.")
        lines = []
        for rule in sorted(findings):
            vecs = findings[rule]["vectors"]
            tag = "🎭 hidden in embed" if findings[rule]["hidden"] else ", ".join(
                sorted(v for v in vecs if v != "link"))
            lines.append(f"• `{defang(rule)}` — {tag or 'link'}")
        embed.add_field(name="Matched", value="\n".join(lines)[:1024], inline=False)
        if hidden:
            embed.add_field(
                name="⚠️ Hidden-embed trick",
                value="Tracker domain was in the unfurled **embed**, not the visible "
                      "message text — a masked/proxied link. The hide-behind-another-"
                      "domain attack.", inline=False)
        embed.add_field(name="Severity", value="🔴 HIGH" if high else "🟡 LOW (shortener)",
                        inline=True)
        embed.add_field(name="Mode", value="ENFORCE" if enforce else "SHADOW (alert-only)",
                        inline=True)
        if enforce:
            action = []
            action.append("🗑️ deleted" if acts["deleted"] else "⚠️ not deleted")
            if acts["timed_out"]:
                mins = cfg.get("linkguard_catch_timeout_min", 60) if high \
                    else cfg.get("linkguard_timeout_min", 10)
                action.append(f"⏳ timed out {mins}m")
            if acts["taunted"]:
                action.append("😈 publicly called out")
            if acts["quarantine_scheduled"]:
                action.append(f"🔒 quarantine in "
                              f"{int(cfg.get('linkguard_quarantine_delay_sec',600))//60}m")
            if is_webhook:
                action.append("(webhook — no timeout/quarantine)")
        else:
            preview = "would delete + " + (
                f"timeout {cfg.get('linkguard_catch_timeout_min',60)}m + 😈 taunt + 🔒 quarantine"
                if high else f"timeout {cfg.get('linkguard_timeout_min',10)}m")
            action = [f"none — SHADOW ({preview})"]
        embed.add_field(name="Action", value=" · ".join(action)[:1024], inline=False)
        if not enforce:
            embed.set_footer(text="Shadow mode — alert only. /hitlist enforce on:True to act.")
        ping = self._ping_prefix(cfg) if (enforce and high) or hidden else None
        await ch.send(content=ping,
                      embed=embed,
                      allowed_mentions=discord.AllowedMentions.all())

    # ------------------------------------------------------------- commands
    hitlist = app_commands.Group(
        name="hitlist", description="Canary-token / IP-grabber link detection (Manage Server)",
        guild_only=True, default_permissions=discord.Permissions(manage_guild=True))

    @hitlist.command(name="enable", description="Turn on LinkGuard for this server (shadow mode).")
    @app_commands.describe(modlog="Channel for LinkGuard alerts (reuses the security mod-log if set).")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def enable_cmd(self, interaction: discord.Interaction, modlog: discord.TextChannel = None):
        cfg = get_config(interaction.guild.id)
        fields = {"linkguard_enabled": 1}
        if modlog is not None:
            fields["modlog_channel_id"] = str(modlog.id)
        set_config(interaction.guild.id, **fields)
        ml = modlog.mention if modlog else (
            f"<#{cfg.get('modlog_channel_id')}>" if cfg.get("modlog_channel_id")
            else "⚠️ none set — pass `modlog:` or run `/security setup`")
        await interaction.response.send_message(
            f"✅ **LinkGuard enabled** in **🟡 shadow mode** (alerts only).\n"
            f"• Mod-log: {ml}\n"
            f"• Watching {len(self.base)} base domains + "
            f"{len(cfg.get('linkguard_extra_domains') or [])} guild extras.\n"
            f"• Run `/hitlist enforce on:True` once you've watched it to delete + timeout.",
            ephemeral=True)

    @hitlist.command(name="enforce", description="Toggle acting (delete + timeout) vs shadow (alert-only).")
    @app_commands.describe(on="True = delete the message + timeout the poster · False = alert only")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def enforce_cmd(self, interaction: discord.Interaction, on: bool):
        if not get_config(interaction.guild.id).get("linkguard_enabled"):
            await interaction.response.send_message(
                "⚠️ LinkGuard isn't enabled here — run `/hitlist enable` first.", ephemeral=True)
            return
        set_config(interaction.guild.id, linkguard_enforce=1 if on else 0)
        await interaction.response.send_message(
            "🔴 **Enforce ON** — grabber/canary links get deleted and the poster timed out."
            if on else "🟡 **Shadow ON** — LinkGuard will only alert, not act.", ephemeral=True)

    @hitlist.command(name="disable", description="Turn off LinkGuard for this server.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def disable_cmd(self, interaction: discord.Interaction):
        set_config(interaction.guild.id, linkguard_enabled=0, linkguard_enforce=0)
        await interaction.response.send_message("⚪ LinkGuard **disabled** for this server.", ephemeral=True)

    @hitlist.command(name="add", description="Add a domain to this server's grabber/canary hitlist.")
    @app_commands.describe(domain="e.g. grabify.link  (a bare word like 'canarytokens' matches as a substring)")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def add_cmd(self, interaction: discord.Interaction, domain: str):
        rule = normalize_rule(domain)
        if not rule:
            await interaction.response.send_message("❌ Give me a domain.", ephemeral=True)
            return
        cfg = get_config(interaction.guild.id)
        extra = list(cfg.get("linkguard_extra_domains") or [])
        if rule in self.base or rule in extra:
            await interaction.response.send_message(f"ℹ️ `{defang(rule)}` is already on the list.", ephemeral=True)
            return
        extra.append(rule)
        # if it was previously allow-listed, un-allow it
        allow = [a for a in (cfg.get("linkguard_allow_domains") or []) if normalize_rule(a) != rule]
        set_config(interaction.guild.id, linkguard_extra_domains=extra, linkguard_allow_domains=allow)
        await interaction.response.send_message(
            f"✅ Added `{defang(rule)}` to the hitlist ({len(self.base)+len(extra)} total).", ephemeral=True)

    @hitlist.command(name="remove", description="Remove a domain: drop a guild-added one, or allow-list a base one.")
    @app_commands.describe(domain="Domain to stop matching in this server.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def remove_cmd(self, interaction: discord.Interaction, domain: str):
        rule = normalize_rule(domain)
        cfg = get_config(interaction.guild.id)
        extra = list(cfg.get("linkguard_extra_domains") or [])
        if rule in extra:
            extra.remove(rule)
            set_config(interaction.guild.id, linkguard_extra_domains=extra)
            await interaction.response.send_message(f"🗑️ Removed guild domain `{defang(rule)}`.", ephemeral=True)
            return
        if rule in self.base:
            allow = list(cfg.get("linkguard_allow_domains") or [])
            if rule not in (normalize_rule(a) for a in allow):
                allow.append(rule)
                set_config(interaction.guild.id, linkguard_allow_domains=allow)
            await interaction.response.send_message(
                f"🔕 `{defang(rule)}` is a **base** domain — allow-listed for this server so it no longer matches.",
                ephemeral=True)
            return
        await interaction.response.send_message(f"ℹ️ `{defang(rule)}` isn't on this server's list.", ephemeral=True)

    @hitlist.command(name="test", description="Dry-run: would this text/URL trip LinkGuard?")
    @app_commands.describe(text="Paste a message or URL — nothing is fetched, matching only.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def test_cmd(self, interaction: discord.Interaction, text: str):
        cfg = get_config(interaction.guild.id)
        findings = scan(text, [], self._domains_for(cfg), cfg.get("linkguard_allow_domains") or [])
        if not findings:
            await interaction.response.send_message("✅ No match — this wouldn't trip LinkGuard.", ephemeral=True)
            return
        lines = [f"• `{defang(r)}` — {', '.join(sorted(findings[r]['vectors']))}"
                 f"{'  🎭 hidden' if findings[r]['hidden'] else ''}" for r in sorted(findings)]
        await interaction.response.send_message("🎣 **Would trip:**\n" + "\n".join(lines)[:1900], ephemeral=True)

    @hitlist.command(name="list", description="Show LinkGuard status + this server's domain counts.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def list_cmd(self, interaction: discord.Interaction):
        cfg = get_config(interaction.guild.id)
        on = bool(cfg.get("linkguard_enabled"))
        mode = ("🔴 ENFORCE" if cfg.get("linkguard_enforce") else "🟡 SHADOW") if on else "⚪ DISABLED"
        extra = cfg.get("linkguard_extra_domains") or []
        allow = cfg.get("linkguard_allow_domains") or []
        embed = discord.Embed(title="🎣 LinkGuard", color=0x5B8CFF,
                              description=f"**Mode:** {mode}")
        embed.add_field(name="Base domains", value=str(len(self.base)), inline=True)
        embed.add_field(name="Guild-added", value=", ".join(f"`{defang(d)}`" for d in extra) or "none",
                        inline=True)
        embed.add_field(name="Allow-listed", value=", ".join(f"`{defang(d)}`" for d in allow) or "none",
                        inline=True)
        embed.add_field(name="Covers", value=(
            "• raw content (incl. scheme-less)\n• markdown-masked links `[x](url)`\n"
            "• unfurled embeds + proxied image URLs (the hidden-embed trick)"), inline=False)
        embed.set_footer(text="/hitlist add · remove · test · enable · enforce")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def cog_app_command_error(self, interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            msg = "❌ Manage Server permission required."
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)


async def setup(bot):
    await bot.add_cog(LinkGuard(bot))
