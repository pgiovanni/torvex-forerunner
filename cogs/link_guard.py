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

INVITE CAPTURE (2026-08-31): every Discord invite link posted (discord.gg /
discord.com/invite, in text, masked links, or embeds) is resolved via the API
(metadata only — never a join) and recorded durably to linkguard.db
`invite_posts`. Invites to THIS server or an allow-listed friendly server are
captured quietly; a FOREIGN invite from a non-staff member raises a mod-log
embed naming the server it points at, and a burst of them — counted ACROSS
channels, the split that keeps invite-spam selfbots under the per-channel flood
rule — trips a spam response: delete + timeout (enforce mode; shadow alerts).
Configure with /hitlist invites.
"""
import asyncio
import datetime
import ipaddress
import json
import logging
import os
import re
import sqlite3
import sys
import time
from urllib.parse import unquote, urlparse

import socket

import discord
from discord import app_commands
from discord.ext import commands, tasks

import quarantine_store as qstore  # shared with AntiNuke/AltGuard — /altguard-release restores

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from utils.security_config import get_config, set_config, is_enabled

log = logging.getLogger("link_guard")

_DATA = os.path.join(os.path.dirname(__file__), "..", "data", "link_hitlist.json")
# Durable catch log — survives restarts; keyed to the offender's user id so a hit
# ties back to their AltGuard verification record (a known-offender trail).
_TRIPDB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "linkguard.db")

# Known dedicated tracker origin IPs (grabify/iplogger shared hosts). Seeded from
# DNS recon; the cog AUTO-LEARNS more by resolving the grabify corpus and keeping
# only IPs that >= _IP_CONSENSUS known grabber domains share — so a shared-hosting
# coincidence can't become a block rule. DNS resolution only, never HTTP, so a
# lookup never fires the tracker.
_SEED_TRACKER_IPS = {"52.173.151.229", "104.247.81.99"}
_IP_CONSENSUS = 2

# Shared reverse-proxy CDN ranges (Cloudflare et al.) — these front MILLIONS of
# unrelated sites, so an IP here can NEVER be a dedicated tracker origin even if
# several grabber domains resolve to it (grabify.link itself sits behind
# Cloudflare). Auto-learned IPs in these ranges are dropped, or we'd flag half the
# web. Dedicated cloud-VM ranges (Azure/AWS/GCP) are NOT excluded — a grabber's
# own box lives there (52.173.151.229 is Azure).
_CDN_EXCLUDE = [ipaddress.ip_network(c) for c in (
    "173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22",
    "141.101.64.0/18", "108.162.192.0/18", "190.93.240.0/20", "188.114.96.0/20",
    "197.234.240.0/22", "198.41.128.0/17", "162.158.0.0/15", "104.16.0.0/13",
    "104.24.0.0/14", "172.64.0.0/13", "131.0.72.0/22",       # Cloudflare
    "151.101.0.0/16",                                         # Fastly
)]


def _is_shared_cdn(ip):
    """True if an IP is a shared CDN / private / reserved address — never trust it
    as a dedicated tracker origin."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    if addr.is_private or addr.is_loopback or addr.is_reserved or addr.is_multicast:
        return True
    return any(addr in net for net in _CDN_EXCLUDE)
# hosts we never bother resolving — common infra that could never be a tracker IP.
_SKIP_RESOLVE = {
    "discord.com", "discordapp.com", "cdn.discordapp.com", "media.discordapp.net",
    "tenor.com", "youtube.com", "youtu.be", "google.com", "github.com",
    "twitter.com", "x.com", "reddit.com", "imgur.com", "twitch.tv", "spotify.com",
}

# Discord invite link — any of the real invite URL shapes, scheme optional.
# The code keeps its case (vanity codes are case-sensitive).
_INVITE_RE = re.compile(
    r"(?:https?://)?(?:(?:www|ptb|canary)\.)?(?:discord\.gg|discord(?:app)?\.com/invite)/"
    r"([A-Za-z0-9][A-Za-z0-9-]{1,31})", re.I)

# Perms that mean "trusted enough to share an invite deliberately" — staff posts
# (partnerships etc.) are captured in the table but never alerted or punished.
_STAFF_PERMS = ("administrator", "manage_guild", "manage_channels", "manage_messages",
                "kick_members", "ban_members", "moderate_members", "manage_roles")


def _is_staff(member):
    p = member.guild_permissions
    return any(getattr(p, name, False) for name in _STAFF_PERMS)


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


def load_category(cat, path=_DATA):
    """Normalized rule list for a single category (e.g. 'grabify'). Used to learn
    tracker origin IPs from just the dedicated-grabber category."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    return [normalize_rule(d) for d in data.get(cat, []) if normalize_rule(d)]


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


def _embed_url_strings(embed_dicts):
    """Only the URL-bearing fields of an embed (any *url key: the embed url, image/
    thumbnail/video url + proxy_url, icon urls) — where a hidden-image grabber
    actually lives. DELIBERATELY EXCLUDES prose fields (description, title, author/
    provider name, fields, footer text): a domain merely *mentioned* in a legit
    unfurl (e.g. a bit.ly inside a YouTube video's description) is third-party
    content the poster didn't control, and blaming them for it is a false positive."""
    out = []

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if isinstance(v, str):
                    if k.endswith("url"):
                        out.append(v)
                elif isinstance(v, (dict, list)):
                    walk(v)
        elif isinstance(o, (list, tuple)):
            for v in o:
                walk(v)

    for e in (embed_dicts or []):
        walk(e)
    return out


def _flatten(obj, acc):
    """Collect EVERY string value from a nested embed (prose included). Used ONLY
    by the retroactive audit (scan(..., embed_prose=True)) to catch a grabber link
    QUOTED in a mod-log entry's description/field — the live detector never uses it
    (prose matching is the legit-unfurl false-positive source)."""
    if isinstance(obj, str):
        acc.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            _flatten(v, acc)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _flatten(v, acc)


def _bmatch(rule, blob):
    """Substring match anchored to domain-label boundaries, so a short rule like
    `x.co` can't match inside a longer label (`relax.com`, `x.com`) and spam false
    positives. Dots count as boundaries, so subdomains and a domain at the end of a
    sentence still match."""
    return re.search(r"(?<![a-z0-9-])" + re.escape(rule) + r"(?![a-z0-9-])", blob) is not None


def scan(content, embed_dicts, domains, allow=(), embed_prose=False):
    """Core detector. Returns {rule: {"vectors": set(...), "hidden": bool}}.

    content       raw message content (may be "")
    embed_dicts   list of embed dicts (discord.Embed.to_dict() or raw payload embeds)
    domains       iterable of hitlist rules (already or not yet normalized)
    allow         iterable of domains to suppress (per-guild allowlist)
    embed_prose   LIVE detector leaves this False: match only URL/image embed fields,
                  never the prose (description/title) that carries third-party unfurl
                  content — a bit.ly in a YouTube video's description is not the
                  poster's doing. The retroactive AUDIT passes True to also scan prose
                  (to catch a grabber link quoted inside a mod-log entry).
    """
    content = content or ""
    allow = {normalize_rule(a) for a in allow if normalize_rule(a)}
    rules = []
    for d in domains:
        r = normalize_rule(d)
        if r and r not in allow:
            rules.append(r)

    if embed_prose:
        embed_strings = []
        for e in (embed_dicts or []):
            _flatten(e, embed_strings)
    else:
        embed_strings = _embed_url_strings(embed_dicts)
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
        if _bmatch(rule, blob_masked):
            vectors.add("masked")
        if _bmatch(rule, blob_embed):
            vectors.add("embed")
        if _bmatch(rule, blob_content):
            vectors.add("content")
        if vectors:
            # "hidden": present in the unfurled embed but NOT in the visible text
            # or a masked-link target with the domain absent from plain content.
            hidden = (("embed" in vectors or "masked" in vectors)
                      and "content" not in vectors)
            findings[rule] = {"vectors": vectors, "hidden": hidden}
    return findings


def candidate_hostnames(content, embed_dicts):
    """Every hostname referenced in the text, masked links, or embeds — the set
    the IP-origin check resolves (minus the ones a domain rule already caught)."""
    content = content or ""
    tokens = set(_URL_RE.findall(content))
    tokens.update(_URL_RE.findall(" \n ".join(_embed_url_strings(embed_dicts))))
    tokens.update(_MASK_RE.findall(content))
    return {h for h in (hostname_of(t) for t in tokens) if h}


def match_tracker_ip(host, resolved_ips, tracker_ips):
    """If a host resolves onto a known tracker origin IP, return an IP-vector
    finding for it, else None. Pure — unit-testable without live DNS."""
    hit = set(resolved_ips) & set(tracker_ips)
    if hit:
        return {"vectors": {"ip"}, "hidden": False, "resolved_ip": sorted(hit)[0]}
    return None


def defang(s):
    """Render a URL/domain un-clickable for the mod-log."""
    return (s or "").replace("http", "hxxp").replace(".", "[.]")


def extract_invite_codes(content, embed_dicts):
    """Every Discord invite code visible in a message — raw text (which includes
    masked-link targets) plus unfurled-embed URL fields. De-duped, first-seen
    order, case preserved (vanity codes are case-sensitive)."""
    blobs = [unquote(content or ""),
             unquote(" \n ".join(_embed_url_strings(embed_dicts)))]
    out, seen = [], set()
    for blob in blobs:
        for m in _INVITE_RE.finditer(blob):
            code = m.group(1)
            if code not in seen:
                seen.add(code)
                out.append(code)
    return out


def invite_spam_hit(times, now, count, window):
    """Sliding-window spam check for one member. `times` is their mutable list of
    prior post stamps: `now` is appended, stale stamps pruned, and True returned
    once `count` foreign-invite posts landed inside `window` seconds. Deliberately
    NOT keyed per channel — splitting a blast across channels is exactly how the
    8/31 selfbot stayed under anti-nuke's per-channel flood counter."""
    times.append(now)
    while times and now - times[0] > window:
        times.pop(0)
    return len(times) >= count


def format_age(delta):
    """Human age of an account from a timedelta — '7h' / '12 days' / '2.4 years'."""
    days = delta.days + delta.seconds / 86400.0
    if days < 1:
        return f"{max(0, int(delta.total_seconds() // 3600))}h"
    if days < 60:
        return f"{days:.0f} days"
    return f"{days / 365.25:.1f} years"


# ------------------------------------------------------- durable trip log
def _tripdb():
    c = sqlite3.connect(_TRIPDB, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def _init_tripdb():
    with _tripdb() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS trips (
                   id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL,
                   guild_id TEXT, user_id TEXT, username TEXT,
                   channel_id TEXT, message_id TEXT,
                   rules TEXT, vectors TEXT, severity TEXT,
                   hidden INTEGER, is_webhook INTEGER, enforce INTEGER, actions TEXT)"""
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_trips_user ON trips(guild_id, user_id, ts)")
        # invite capture — one row per invite code per message, own-guild included
        # (quiet rows are still the record "who shared what, when"). NB the column
        # is is_foreign because FOREIGN is an SQL keyword.
        c.execute(
            """CREATE TABLE IF NOT EXISTS invite_posts (
                   id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL,
                   guild_id TEXT, user_id TEXT, username TEXT,
                   channel_id TEXT, message_id TEXT, code TEXT,
                   target_guild_id TEXT, target_guild_name TEXT,
                   member_count INTEGER, is_foreign INTEGER)"""
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_invite_user ON invite_posts(guild_id, user_id, ts)")


def _persist_invite_post(row):
    try:
        with _tripdb() as c:
            c.execute(
                "INSERT INTO invite_posts(ts,guild_id,user_id,username,channel_id,message_id,"
                "code,target_guild_id,target_guild_name,member_count,is_foreign) VALUES "
                "(:ts,:guild_id,:user_id,:username,:channel_id,:message_id,"
                ":code,:target_guild_id,:target_guild_name,:member_count,:is_foreign)", row)
    except Exception:
        log.exception("link_guard: persist invite post failed")


def _count_user_invite_posts(guild_id, user_id):
    """How many invite posts this member already has on record in this guild."""
    try:
        with _tripdb() as c:
            return c.execute("SELECT COUNT(*) FROM invite_posts WHERE guild_id=? AND user_id=?",
                             (str(guild_id), str(user_id))).fetchone()[0]
    except Exception:
        return 0


def _guild_invite_stats(guild_id):
    """(total, foreign) invite posts captured for a guild — for /hitlist invites."""
    try:
        with _tripdb() as c:
            row = c.execute("SELECT COUNT(*), COALESCE(SUM(is_foreign),0) FROM invite_posts "
                            "WHERE guild_id=?", (str(guild_id),)).fetchone()
            return int(row[0]), int(row[1])
    except Exception:
        return 0, 0


def _persist_trip(row):
    try:
        with _tripdb() as c:
            c.execute(
                "INSERT INTO trips(ts,guild_id,user_id,username,channel_id,message_id,"
                "rules,vectors,severity,hidden,is_webhook,enforce,actions) VALUES "
                "(:ts,:guild_id,:user_id,:username,:channel_id,:message_id,"
                ":rules,:vectors,:severity,:hidden,:is_webhook,:enforce,:actions)", row)
    except Exception:
        log.exception("link_guard: persist trip failed")


def _count_user_trips(guild_id, user_id):
    """Prior catches on record for this user in this guild — their offender count."""
    try:
        with _tripdb() as c:
            return c.execute("SELECT COUNT(*) FROM trips WHERE guild_id=? AND user_id=?",
                             (str(guild_id), str(user_id))).fetchone()[0]
    except Exception:
        return 0


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
        # message ids already written to the durable trip log (one row per catch
        # even though we scan twice). {message_id: (expiry_ts, offense_count)}
        self._logged = {}
        # DNS-origin detection: known tracker IPs (seed + auto-learned) + resolver
        # cache so a repeated host isn't looked up twice. {host: (expiry_ts, {ips})}
        self._tracker_ips = set(_SEED_TRACKER_IPS)
        self._dns_cache = {}
        # invite capture: resolved-code cache {code: (expiry, info|None)}, per-member
        # sliding windows {(gid,uid): [ts,...]}, spam-trip suppression
        # {(gid,uid): expiry} and per-message dedupe {message_id: expiry}.
        self._invite_cache = {}
        self._invite_times = {}
        self._invite_tripped = {}
        self._invite_msgs = {}
        _init_tripdb()
        log.info("link_guard: loaded %d base domains (%d shorteners)",
                 len(self.base), len(self.shortener_rules))

    async def cog_load(self):
        self.refresh_tracker_ips.start()

    async def cog_unload(self):
        self.refresh_tracker_ips.cancel()

    # --------------------------------------------------- DNS-origin detection
    @staticmethod
    def _resolve_blocking(host):
        try:
            return {info[4][0] for info in socket.getaddrinfo(host, None) if info[4]}
        except OSError:
            return set()

    async def _resolve(self, host, fresh=False):
        """A-record set for a host (cached ~5 min). Runs in an executor with a
        hard timeout so a slow resolver can't stall the event loop. DNS only."""
        now = time.time()
        if not fresh:
            hit = self._dns_cache.get(host)
            if hit and hit[0] > now:
                return hit[1]
        try:
            loop = asyncio.get_event_loop()
            ips = await asyncio.wait_for(
                loop.run_in_executor(None, self._resolve_blocking, host), timeout=3)
        except Exception:
            ips = set()
        if len(self._dns_cache) > 4096:
            self._dns_cache = {k: v for k, v in self._dns_cache.items() if v[0] > now}
        self._dns_cache[host] = (now + 300, ips)
        return ips

    @tasks.loop(hours=6)
    async def refresh_tracker_ips(self):
        await self._rebuild_tracker_ips()

    @refresh_tracker_ips.before_loop
    async def _before_refresh(self):
        await self.bot.wait_until_ready()

    async def _rebuild_tracker_ips(self):
        """Resolve the grabify corpus and keep IPs shared by >= _IP_CONSENSUS known
        grabber domains — dedicated tracker origins, safe to block by IP. Catches
        grabify's rotating vanities pointed at a known origin the domain list misses."""
        grabify = [d for d in load_category("grabify") if "." in d]
        try:
            resolved = await asyncio.gather(*(self._resolve(d, fresh=True) for d in grabify))
        except Exception:
            return
        counts = {}
        for ips in resolved:
            for ip in ips:
                counts[ip] = counts.get(ip, 0) + 1
        learned = {ip for ip, n in counts.items()
                   if n >= _IP_CONSENSUS and not _is_shared_cdn(ip)}
        self._tracker_ips = set(_SEED_TRACKER_IPS) | learned
        log.info("link_guard: tracker-IP set = %d (%d auto-learned from grabify, CDN-filtered)",
                 len(self._tracker_ips), len(learned))

    async def _augment_ip(self, findings, content, embed_dicts, cfg):
        """Add HIGH findings for unknown hosts that resolve onto a known tracker
        origin IP — the durable catch for grabify's rotating vanity domains."""
        trackers = self._tracker_ips | {str(x) for x in (cfg.get("linkguard_tracker_ips") or [])}
        if not trackers:
            return
        allow = {normalize_rule(a) for a in (cfg.get("linkguard_allow_domains") or []) if normalize_rule(a)}
        covered = set(findings)
        for host in list(candidate_hostnames(content, embed_dicts))[:8]:
            if host in _SKIP_RESOLVE or host in findings:
                continue
            if any(host == r or host.endswith("." + r) for r in covered):
                continue  # already caught by a domain rule
            if any(host == a or host.endswith("." + a) for a in allow):
                continue
            finding = match_tracker_ip(host, await self._resolve(host), trackers)
            if finding:
                findings[host] = finding

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
            author_name=(str(message.author) if message.author else None),
            content=message.content or "",
            embed_dicts=[e.to_dict() for e in message.embeds],
            is_webhook=bool(message.webhook_id),
            message=message,
        )
        try:
            await self._process_invites(message)
        except Exception:
            log.exception("link_guard: invite capture failed")

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
        author_name = author.get("username")
        channel = guild.get_channel(payload.channel_id)
        if channel is None:
            return
        await self._process(
            guild=guild,
            channel=channel,
            message_id=payload.message_id,
            author_id=author_id,
            author_name=author_name,
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

    def _log_trip(self, *, guild_id, user_id, username, channel_id, message_id,
                  findings, severity, is_webhook, enforce, acts):
        """Persist the catch durably (one row per message, survives restarts) and
        return how many PRIOR catches this user already has in this guild. The
        user_id is the join key back to their AltGuard verification record."""
        now = time.time()
        prior = self._logged.get(message_id)
        if prior is not None:
            return prior[1]  # already logged this message (second scan pass)
        offense = _count_user_trips(guild_id, user_id) if user_id else 0
        vectors = sorted({v for f in findings.values() for v in f["vectors"]})
        _persist_trip({
            "ts": now, "guild_id": str(guild_id),
            "user_id": str(user_id) if user_id else None, "username": username,
            "channel_id": str(channel_id) if channel_id else None,
            "message_id": str(message_id),
            "rules": json.dumps(sorted(findings)), "vectors": json.dumps(vectors),
            "severity": severity,
            "hidden": 1 if any(f["hidden"] for f in findings.values()) else 0,
            "is_webhook": 1 if is_webhook else 0, "enforce": 1 if enforce else 0,
            "actions": json.dumps(acts)})
        if len(self._logged) > 4096:
            self._logged = {k: v for k, v in self._logged.items() if v[0] > now}
        self._logged[message_id] = (now + 900, offense)
        return offense

    # ------------------------------------------------------------- core
    async def _process(self, *, guild, channel, message_id, author_id, author_name,
                        content, embed_dicts, is_webhook, message):
        if not is_enabled(guild.id, "linkguard"):
            return
        cfg = get_config(guild.id)
        # exempt trusted humans (but always scan webhook/bot posts)
        if not is_webhook and self._exempt(guild, author_id, cfg):
            return
        findings = scan(content, embed_dicts, self._domains_for(cfg),
                        cfg.get("linkguard_allow_domains") or [])
        if cfg.get("linkguard_resolve_ips", 1):
            await self._augment_ip(findings, content, embed_dicts, cfg)
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
        # durable record (survives restarts) + this user's prior-catch count
        offense = self._log_trip(
            guild_id=guild.id, user_id=author_id, username=author_name,
            channel_id=getattr(channel, "id", None), message_id=message_id,
            findings=findings, severity=severity, is_webhook=is_webhook,
            enforce=enforce, acts=acts)
        await self._alert(guild, cfg, channel, author_id, is_webhook,
                          findings, enforce, severity, acts, offense)

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
                     findings, enforce, severity, acts, offense=0):
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
            meta = findings[rule]
            vecs = meta["vectors"]
            if meta.get("resolved_ip"):
                tag = f"🛰️ resolved to known tracker IP `{meta['resolved_ip']}`"
            elif meta["hidden"]:
                tag = "🎭 hidden in embed"
            else:
                tag = ", ".join(sorted(v for v in vecs if v != "link"))
            lines.append(f"• `{defang(rule)}` — {tag or 'link'}")
        embed.add_field(name="Matched", value="\n".join(lines)[:1024], inline=False)
        if offense:
            embed.add_field(
                name="🔁 Known offender",
                value=f"**{offense}** prior LinkGuard catch(es) on record for this user "
                      f"— logged and tied to their verification.", inline=False)
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

    # ------------------------------------------------------- invite capture
    async def _resolve_invite(self, code):
        """Invite metadata via the API — a read, never a join. Returns
        {guild_id, guild_name, members} or None for an invalid/expired/
        unresolvable code. Cached ~10 min either way, so a spammer repeating one
        code costs a single API call."""
        now = time.time()
        hit = self._invite_cache.get(code)
        if hit and hit[0] > now:
            return hit[1]
        info = None
        try:
            inv = await asyncio.wait_for(
                self.bot.fetch_invite(code, with_counts=True), timeout=5)
            g = inv.guild
            info = {"guild_id": str(g.id) if g else None,
                    "guild_name": getattr(g, "name", None),
                    "members": getattr(inv, "approximate_member_count", None)}
        except Exception:
            info = None
        if len(self._invite_cache) > 2048:
            self._invite_cache = {k: v for k, v in self._invite_cache.items() if v[0] > now}
        self._invite_cache[code] = (now + 600, info)
        return info

    async def _process_invites(self, message):
        """Capture every Discord invite in a member's message; alert on foreign
        ones and trip the cross-channel spam response on a burst. Runs from
        on_message only (an unfurl edit adds no new invite codes — the URL was
        already in the text)."""
        guild, author = message.guild, message.author
        if author is None or author.bot or message.webhook_id:
            return
        if not is_enabled(guild.id, "linkguard"):
            return
        cfg = get_config(guild.id)
        if not cfg.get("linkguard_invites", 1):
            return
        if str(message.channel.id) in {str(c) for c in (cfg.get("linkguard_invite_exempt_channels") or [])}:
            return
        if self._exempt(guild, author.id, cfg):
            return
        codes = extract_invite_codes(message.content or "",
                                     [e.to_dict() for e in message.embeds])
        if not codes:
            return
        now = time.time()
        if len(self._invite_msgs) > 4096:
            self._invite_msgs = {k: v for k, v in self._invite_msgs.items() if v > now}
        if self._invite_msgs.get(message.id, 0) > now:
            return
        self._invite_msgs[message.id] = now + 900

        allow_guilds = {str(x) for x in (cfg.get("linkguard_invite_allow_guilds") or [])}
        allow_guilds.add(str(guild.id))
        foreign = []
        for code in codes[:3]:   # resolve at most 3 codes per message
            info = await self._resolve_invite(code)
            is_foreign = info is None or info.get("guild_id") not in allow_guilds
            _persist_invite_post({
                "ts": now, "guild_id": str(guild.id),
                "user_id": str(author.id), "username": str(author),
                "channel_id": str(message.channel.id), "message_id": str(message.id),
                "code": code,
                "target_guild_id": (info or {}).get("guild_id"),
                "target_guild_name": (info or {}).get("guild_name"),
                "member_count": (info or {}).get("members"),
                "is_foreign": 1 if is_foreign else 0})
            if is_foreign:
                foreign.append((code, info))
        if not foreign:
            return  # our own / friendly invites: captured quietly, nothing more
        member = guild.get_member(author.id)
        if member is None or _is_staff(member):
            return  # staff share invites deliberately (partnerships) — table only

        enforce = bool(cfg.get("linkguard_enforce"))
        key = (guild.id, author.id)
        if self._invite_tripped.get(key, 0) > now:
            # already contained this run — keep sweeping, stay quiet
            if enforce:
                await self._delete(message.channel, message, message.id)
            return
        spam = cfg.get("linkguard_invite_spam") or [3, 60]
        try:
            s_count, s_win = max(2, int(spam[0])), max(5, int(spam[1]))
        except (TypeError, ValueError, IndexError):
            s_count, s_win = 3, 60
        times = self._invite_times.setdefault(key, [])
        hit = invite_spam_hit(times, now, s_count, s_win)
        acts = {"deleted": False, "timed_out": False}
        if hit:
            # suppress further embeds/punishment for them either mode; enforce
            # additionally keeps deleting whatever else they post (branch above)
            self._invite_tripped[key] = now + 600
            if len(self._invite_tripped) > 2048:
                self._invite_tripped = {k: v for k, v in self._invite_tripped.items() if v > now}
            if enforce:
                acts["deleted"] = await self._delete(message.channel, message, message.id)
                why = f"invite spam: {len(times)} server invites in {s_win}s"
                try:
                    await member.timeout(
                        datetime.timedelta(minutes=int(cfg.get("linkguard_invite_timeout_min", 60))),
                        reason=f"LinkGuard: {why}"[:400])
                    acts["timed_out"] = True
                except (discord.Forbidden, discord.HTTPException):
                    pass
        await self._invite_alert(guild, cfg, message.channel, member, foreign,
                                 hit, enforce, acts, s_win, len(times))

    async def _invite_alert(self, guild, cfg, channel, member, foreign, hit,
                            enforce, acts, window, n):
        ch = self._modlog(guild, cfg)
        if ch is None:
            return
        if hit:
            title, color = "📨🚨 LinkGuard — invite SPAM", 0x8B0000
        else:
            title, color = "📨 Foreign server invite posted", 0xE0A23B
        lines = []
        for code, info in foreign:
            if info:
                members = info.get("members")
                lines.append(f"• `discord.gg/{code}` → **{info.get('guild_name') or 'unknown'}** "
                             f"(`{info.get('guild_id')}`"
                             + (f", ~{members:,} members)" if members else ")"))
            else:
                lines.append(f"• `discord.gg/{code}` — ⚠️ invalid, expired, or unresolvable")
        embed = discord.Embed(
            title=title, color=color,
            description=f"{member.mention} (`{member.id}`) in {channel.mention}.")
        embed.add_field(name="Invite(s)", value="\n".join(lines)[:1024], inline=False)
        embed.add_field(name="Account age",
                        value=format_age(discord.utils.utcnow() - member.created_at),
                        inline=True)
        prior = _count_user_invite_posts(guild.id, member.id)
        if prior > 1:
            embed.add_field(name="On record",
                            value=f"{prior} invite post(s) logged", inline=True)
        if hit:
            embed.add_field(
                name="Pattern",
                value=f"**{n}** foreign invites inside **{window}s** — counted across "
                      f"channels (the split that dodges the flood rule).", inline=False)
            if enforce:
                tmin = int(cfg.get("linkguard_invite_timeout_min", 60))
                action = [("🗑️ deleted" if acts["deleted"] else "⚠️ not deleted"),
                          (f"⏳ timed out {tmin}m" if acts["timed_out"] else "⚠️ timeout failed")]
                embed.set_footer(text="Their further invite posts are auto-removed for "
                                      "10 min. Review and ban if warranted.")
            else:
                action = [f"none — SHADOW (would delete + timeout "
                          f"{int(cfg.get('linkguard_invite_timeout_min', 60))}m)"]
            embed.add_field(name="Action", value=" · ".join(action), inline=False)
        ping = self._ping_prefix(cfg) if (hit and enforce) else None
        try:
            await ch.send(content=ping, embed=embed,
                          allowed_mentions=discord.AllowedMentions.all())
        except (discord.Forbidden, discord.HTTPException):
            pass

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
        embed.set_footer(text="/hitlist add · remove · test · enable · enforce · invites")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @hitlist.command(name="invites",
                     description="Invite capture: log every Discord invite posted + invite-spam response.")
    @app_commands.describe(
        enabled="Capture invite links at all (on by default while LinkGuard is enabled).",
        spam_count="Foreign invites inside the window that count as spam (default 3).",
        spam_window_sec="The spam window, in seconds (default 60). Counted across channels.",
        timeout_min="Timeout applied on an invite-spam trip, in minutes (default 60).",
        exempt_channel="Toggle a channel where invites are ignored entirely (e.g. a promotions channel).",
        allow_guild="Toggle a friendly server id whose invites are treated like our own.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def invites_cmd(self, interaction: discord.Interaction,
                          enabled: bool = None,
                          spam_count: app_commands.Range[int, 2, 20] = None,
                          spam_window_sec: app_commands.Range[int, 10, 600] = None,
                          timeout_min: app_commands.Range[int, 1, 40320] = None,
                          exempt_channel: discord.TextChannel = None,
                          allow_guild: str = None):
        cfg = get_config(interaction.guild.id)
        fields, notes = {}, []
        if enabled is not None:
            fields["linkguard_invites"] = 1 if enabled else 0
            notes.append(f"capture **{'on' if enabled else 'off'}**")
        spam = list(cfg.get("linkguard_invite_spam") or [3, 60])
        if spam_count is not None or spam_window_sec is not None:
            spam = [int(spam_count or spam[0]), int(spam_window_sec or spam[1])]
            fields["linkguard_invite_spam"] = spam
            notes.append(f"spam rule **{spam[0]} in {spam[1]}s**")
        if timeout_min is not None:
            fields["linkguard_invite_timeout_min"] = int(timeout_min)
            notes.append(f"spam timeout **{timeout_min}m**")
        if exempt_channel is not None:
            ex = [str(c) for c in (cfg.get("linkguard_invite_exempt_channels") or [])]
            cid = str(exempt_channel.id)
            if cid in ex:
                ex.remove(cid)
                notes.append(f"{exempt_channel.mention} **watched again**")
            else:
                ex.append(cid)
                notes.append(f"{exempt_channel.mention} **exempt** — invites there are ignored")
            fields["linkguard_invite_exempt_channels"] = ex
        if allow_guild is not None:
            gid = allow_guild.strip()
            if not gid.isdigit():
                await interaction.response.send_message(
                    "❌ `allow_guild` takes a numeric server id.", ephemeral=True)
                return
            al = [str(x) for x in (cfg.get("linkguard_invite_allow_guilds") or [])]
            if gid in al:
                al.remove(gid)
                notes.append(f"server `{gid}` invites are **foreign again**")
            else:
                al.append(gid)
                notes.append(f"server `{gid}` invites now count as **our own**")
            fields["linkguard_invite_allow_guilds"] = al
        if fields:
            cfg = set_config(interaction.guild.id, **fields)

        spam = list(cfg.get("linkguard_invite_spam") or [3, 60])
        total, foreign_n = _guild_invite_stats(interaction.guild.id)
        on = bool(cfg.get("linkguard_invites", 1)) and bool(cfg.get("linkguard_enabled"))
        embed = discord.Embed(
            title="📨 Invite capture",
            color=0x5B8CFF,
            description=("Changed: " + " · ".join(notes) + "\n\n" if notes else "")
            + ("🟢 **Capturing**" if on else
               "⚪ **Off**" + ("" if cfg.get("linkguard_enabled") else " — LinkGuard itself is disabled here")))
        embed.add_field(name="Spam rule",
                        value=f"{spam[0]} foreign invites / {spam[1]}s (across channels)\n"
                              f"→ delete + timeout {cfg.get('linkguard_invite_timeout_min', 60)}m "
                              f"({'🔴 ENFORCE' if cfg.get('linkguard_enforce') else '🟡 SHADOW — alert only'})",
                        inline=False)
        ex = cfg.get("linkguard_invite_exempt_channels") or []
        embed.add_field(name="Exempt channels",
                        value=", ".join(f"<#{c}>" for c in ex) or "none", inline=True)
        al = cfg.get("linkguard_invite_allow_guilds") or []
        embed.add_field(name="Friendly servers",
                        value=", ".join(f"`{g}`" for g in al) or "none", inline=True)
        embed.add_field(name="Captured so far",
                        value=f"{total} invite post(s), {foreign_n} foreign", inline=False)
        embed.set_footer(text="Own-server + friendly invites are logged quietly; staff posts never alert.")
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
