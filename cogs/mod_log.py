"""Message archive + mod-log — the message layer of the MEE6/Quark/Carl-bot
log replacement.

Two jobs, deliberately in one cog because the second depends on the first:

1. ARCHIVE — every guild message is persisted to messages.db (content, author,
   channel, attachments metadata, reply ref) the moment it arrives, and small
   attachments are cached to media_cache/ on disk. Discord's gateway tells you a
   message was deleted but not what it said; the archive is what lets us log
   content, and the media cache is what lets us RE-POST deleted images/videos
   (a deleted attachment's CDN URL dies with the message).

2. MOD-LOG — deletes, edits and bulk deletes are posted to the log channel as
   embeds. Single deletes are attributed via the audit log the way Quark does
   it: Discord AGGREGATES message_delete audit entries (a mod deleting a second
   message by the same author in the same channel bumps `count` on the existing
   entry instead of writing a new one), so we keep a {entry_id: count} cache
   and treat either a fresh entry or a count increase as evidence. No matching
   entry = the author deleted it themselves (self-deletes never hit the audit
   log). Bulk deletes get a chronological transcript .txt built from the
   archive plus the same attribution against message_bulk_delete entries.

Storage: ~1.25M messages ≈ 300-600 MB SQLite for the operator guild; every
other guild lives under a retention window (see RETENTION TIERS below) swept
every SWEEP_MINUTES. Re-posts made to the log channel persist in Discord, so a
cached file is deleted the moment it has been re-posted — the cache holds
pending files, not history.
Per-guild opt-in via security_config (msglog_* keys), same as antinuke/linkguard.
Backfill of pre-cog history = backfill_history.py (REST, writes the same DB).
"""
import asyncio
import glob
import hashlib
import io
import json
import os
import re
import sqlite3
import sys
import time
from collections import OrderedDict

import discord
from discord import app_commands
from discord.ext import commands, tasks

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from utils.security_config import get_config, set_config, is_enabled, all_enabled  # noqa: E402
from utils.quiet_removals import is_quiet  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
DB_PATH = os.path.join(ROOT, "messages.db")
MEDIA_DIR = os.path.join(ROOT, "media_cache")

# ── RETENTION TIERS ─────────────────────────────────────────────────────────
# This bot is public: anyone can add it. A message log that forgets is barely a
# log, but indefinite retention must never happen to a server by accident. The
# shape is the one Quark uses (2026-08-23): a SHORT rolling window for everyone,
# a LONG archive only where somebody took responsibility for it. Three tiers:
#
#   0. RECENT WINDOW (default, every msglog guild, no agreement needed)
#      Messages AND attachments are kept for MSGLOG_RECENT_HOURS (24h) and then
#      swept. That is enough to show what a deleted message said and re-post the
#      deleted image — the thing a mod log is for — without anyone's history
#      accumulating on our disk. Files are capped per guild (MSGLOG_RECENT_MEDIA_MB)
#      so a GIF-heavy stranger's server can't evict the operator's evidence.
#      Quark free: 12h + a per-channel message cap. We give 24h and no cap.
#
#   1. PRO ARCHIVE (paid entitlement + Manage Server accepts the retention terms)
#      Text for MSGLOG_PRO_TEXT_DAYS (90), files for MSGLOG_PRO_MEDIA_DAYS (30),
#      the identity ledger, `/msglog deleted` / `/msglog history`. The entitlement
#      is granted by the operator (`/msglog pro-grant`, fed by the web checkout);
#      the terms acceptance is what makes the server's admin — not us — the one
#      who decided to keep their members' history. Both are required: a paid
#      guild that never accepts stays on the 24h window. Quark Pro: 4 weeks.
#
#   2. OPERATOR ARCHIVE (MSGLOG_ARCHIVE_GUILDS — the operator's own servers)
#      Unlimited, nothing swept, full disk budget. Unchanged from before.
#
# Every row is keyed by guild_id and every read is filtered by it, so a guild is
# a lookup away and revoking the terms purges exactly that guild.
ARCHIVE_GUILDS = {g.strip() for g in os.environ.get(
    "MSGLOG_ARCHIVE_GUILDS",
    os.environ.get("ALTGUARD_GUILD_ID", "")).split(",") if g.strip()}

# Tier 0/1 windows. Operator-controlled ONLY (environment) — a remote admin must
# not be able to turn the free window into indefinite storage on our disk.
RECENT_HOURS = max(1, int(os.environ.get("MSGLOG_RECENT_HOURS") or 24))
RECENT_MEDIA_MB = max(0, int(os.environ.get("MSGLOG_RECENT_MEDIA_MB") or 200))   # per guild
PRO_TEXT_DAYS = max(1, int(os.environ.get("MSGLOG_PRO_TEXT_DAYS") or 90))
PRO_MEDIA_DAYS = max(1, int(os.environ.get("MSGLOG_PRO_MEDIA_DAYS") or 30))
PRO_MEDIA_MB = max(0, int(os.environ.get("MSGLOG_PRO_MEDIA_MB") or 1024))        # per guild
PRO_GRACE_DAYS = max(0, int(os.environ.get("MSGLOG_PRO_GRACE_DAYS") or 3))       # sweep waits this long after expiry
PRO_URL = os.environ.get("MSGLOG_PRO_URL") or "https://torvex.app/Packages#discord-logging-pro"
SWEEP_MINUTES = max(5, int(os.environ.get("MSGLOG_SWEEP_MINUTES") or 30))

# guild_id -> entitlement row (expires_ts None = no expiry). Refilled from the
# archive_pro table at startup and after every grant; consulted per message.
_PRO = {}

# The operator's OWN guilds — the only ones whose settings page may influence
# shared disk policy (retention window, cache cap). A guild can be granted the
# media tier without being handed the operator's disk budget.
OPERATOR_GUILDS = {g.strip() for g in os.environ.get(
    "ALTGUARD_GUILD_ID", "").split(",") if g.strip()} or set(ARCHIVE_GUILDS)

# Bump when the terms text changes materially — acceptance records store the
# version they agreed to, so an old acceptance can be re-prompted rather than
# silently treated as consent to something they never read.
TERMS_VERSION = 2   # v2 2026-08-23: free 24h window + Logging Pro windows

# guild_id -> acceptance row. Cached because on_message consults it per message;
# the DB table is the source of truth and this is refilled at startup.
_CONSENT = {}


# DRAFT — operator should review the wording before this ships publicly.
TERMS_TEXT = (
    "📜 **Message archive — data retention terms (v2)**\n"
    "**Every server already gets the free window:** messages and attachments are held for "
    f"{RECENT_HOURS} hours so deletions and edits can be logged with their content and images, "
    "then swept automatically. Nothing older is kept and nothing needs accepting for that.\n\n"
    "**Logging Pro** keeps a real archive. Turning it on means the person who runs this bot "
    "stores your server's data on their machine for longer. Read this before you accept.\n"
    "**Stored:** message text, author, channel and timestamps · edit history · deletions and who "
    f"performed them · attachments, stickers and avatars (files for {PRO_MEDIA_DAYS} days) · "
    "member events (joins, leaves, nickname/username changes, timeouts, kicks, bans). "
    "Storage begins when you accept — there is no history from before that.\n"
    f"**How long:** text for {PRO_TEXT_DAYS} days, files for {PRO_MEDIA_DAYS} days, rolling, "
    "while the Pro entitlement is active. `/msglog revoke-terms` stops storage and permanently "
    "deletes everything held for this server; when Pro lapses the archive falls back to the "
    "free window.\n"
    "**Who can read it:** people with Manage Server *in this server* (via the bot's commands), "
    "and the bot operator, who maintains the database and cannot be locked out of it.\n"
    "**Your side of it:** you tell your members what is logged, and you remain responsible for "
    "your community's data. The operator supplies the tooling, not the policy.\n"
    "**No guarantee of completeness:** outages, restarts, rate limits, size caps and Discord's "
    "own blind spots all lose records. Treat the archive as helpful, never as proof."
)


def consent_ok(guild_id) -> bool:
    """Current, un-revoked acceptance of the retention terms for this guild."""
    row = _CONSENT.get(str(guild_id))
    return bool(row) and int(row.get("version") or 0) >= TERMS_VERSION


def pro_active(guild_id, now=None, grace_days=0) -> bool:
    """Live Pro entitlement. `grace_days` lets the sweeper keep a lapsed guild's
    archive a few days past expiry so a late renewal doesn't lose history."""
    row = _PRO.get(str(guild_id))
    if not row:
        return False
    exp = row.get("expires_ts")
    if exp is None:
        return True
    try:
        exp = float(exp)
    except (TypeError, ValueError):
        # SQLite's dynamic typing accepts text in a REAL column, so a webhook
        # writing an ISO date instead of an epoch lands here. Treat an
        # unreadable expiry as EXPIRED (fail closed — a guild does not get a
        # paid archive because its row is malformed) rather than raising:
        # pro_active runs from _load_pro in __init__, and an exception there
        # takes the whole mod_log cog down with it.
        return False
    return exp + grace_days * 86400 > (now if now is not None else time.time())


def retention_tier(guild_id) -> str:
    """'operator' | 'pro' | 'recent' — which window this guild's data lives under."""
    gid = str(guild_id)
    if gid in ARCHIVE_GUILDS:
        return "operator"
    if consent_ok(gid) and pro_active(gid):
        return "pro"
    return "recent"


def guild_media_cap_bytes(guild_id):
    """Per-guild ceiling for cached files; None = operator guild, no ceiling."""
    tier = retention_tier(guild_id)
    if tier == "operator":
        return None
    return (PRO_MEDIA_MB if tier == "pro" else RECENT_MEDIA_MB) * 1024 * 1024

# Whole-cache disk limits come from the OPERATOR's environment only. They used
# to be max()'d across every msglog-enabled guild, which let any remote admin
# raise a shared global cap — and, because the cache is one flat LRU directory,
# let a busy remote server evict the operator's own moderation evidence.
MEDIA_CAP_GB = max(1, int(os.environ.get("MSGLOG_MEDIA_CAP_GB") or 5))
MEDIA_DAYS = max(1, int(os.environ.get("MSGLOG_MEDIA_DAYS") or 30))


def archives_messages(guild_id) -> bool:
    """LONG archive (Pro or operator): rows outlive the recent window, the text
    identity ledger is kept, `/msglog deleted` + `/msglog history` are meaningful.
    Every msglog guild stores rows for the recent window regardless — this
    governs how long they live, not whether they exist."""
    return retention_tier(guild_id) != "recent"


def archives_media(guild_id) -> bool:
    """Files beyond the recent window (Pro or operator). Avatar bytes ride on
    this too — those are members' FACES, so the free window records which
    picture it was (hash) and nothing more."""
    return retention_tier(guild_id) != "recent"


def unstored_attachments(atts, cached_paths, cap_mb):
    """Attachments named in the archive that have NO file on disk, with why.

    Feeds the honesty line on delete embeds: the log must never imply it kept
    something it didn't. Reasons are 'too large' (over the per-file cache cap)
    or 'not archived' (cache off / not granted / CDN fetch failed / pruned).
    """
    have = set()
    for p in cached_paths:
        parts = os.path.basename(p).split("_", 2)
        tok = parts[1] if len(parts) == 3 else ""
        if tok.isdigit():
            have.add(int(tok))
    cap = max(0, int(cap_mb or 0)) * 1024 * 1024
    missing = []
    for i, a in enumerate(atts or []):
        if i in have:
            continue
        size = a.get("size") or 0
        missing.append((a.get("filename") or "?",
                        "too large" if cap and size > cap else "not archived",
                        size))
    return missing
FLUSH_SECONDS = 30
RECENT_CAP = 4000           # in-memory rows for instant delete/edit lookups
AUDIT_WAIT = 1.3            # audit entries lag the gateway event slightly
AUDIT_FRESH_WINDOW = 120.0  # unseen audit entry counts as evidence only if this recent
ROLELOG_WINDOW = 20.0       # per-member role-log rate-limit window
ROLELOG_LIMIT = 6           # role changes per window before we pause this member's role logs
ROLELOG_COOLDOWN = 10.0     # after tripping the limit, suppress this member's role logs this long (no punishment)
ROLELOG_NUKE = 25           # role changes/window this high = griefing → quarantine as a nuke
SELFDEL_WINDOW = 300.0      # rolling window for the mass-self-delete detector
SELFDEL_THRESHOLD = 8       # self-deletes in window → alert (Yousef/apple.231 scrubbed
                            # their history before we caught them; self-deletes never
                            # hit the audit log, so anti-nuke is blind here BY DESIGN —
                            # this archive-side detector is the only tripwire possible)
SELFDEL_QUIET = 120.0       # episode ends after this long with no further deletes
MENTION_RE = re.compile(r"<@[!&]?\d+>|@everyone|@here")


def extract_mentions(content):
    """content -> (user_ids, role_ids, everyone, here), order-preserving dedupe.
    Ghost-ping forensics need WHO was pinged: raw <@id> markup in an embed
    renders as @unknown-user whenever the client can't resolve it."""
    c = content or ""
    users = list(dict.fromkeys(re.findall(r"<@!?(\d+)>", c)))
    roles = list(dict.fromkeys(re.findall(r"<@&(\d+)>", c)))
    return users, roles, "@everyone" in c, "@here" in c


def mentions_removed(old, new):
    """Mentions present in old content but gone from new — the edit-away ghost ping."""
    ou, orl, oe, oh = extract_mentions(old)
    nu, nrl, ne, nh = extract_mentions(new)
    return ([u for u in ou if u not in nu], [r for r in orl if r not in nrl],
            oe and not ne, oh and not nh)

COLOR_SELF_DELETE = 0xE67E22
COLOR_MOD_DELETE = 0xC0392B
COLOR_BULK = 0x8B0000
COLOR_EDIT = 0x3498DB
COLOR_JOIN = 0x3BA55D
COLOR_LEAVE = 0x95A5A6
COLOR_KICK = 0xE8A33D
COLOR_BAN = 0x992D22
COLOR_VOICE = 0x9B59B6
COLOR_ROLE = 0x1ABC9C
COLOR_CHANNEL = 0x5865F2
INVITES_DB = os.path.join(ROOT, "invites.db")  # invites cog's attribution store (read-only here)


# --------------------------------------------------------------------------- pure helpers
def _trunc(s, n=1024):
    s = s or ""
    return s if len(s) <= n else s[: n - 1] + "…"


def _plain(s):
    """Render a user-chosen name safely inside an embed field.

    Names are attacker-controlled — a nickname of `@everyone` or one stuffed
    with markdown must not render as a ping or reformat the log entry. Mentions
    are defanged and markdown is escaped; the stored ledger keeps the raw value.
    """
    if not s:
        return ""
    s = str(s).replace("@", "@​")
    return discord.utils.escape_markdown(s)


def safe_filename(name, maxlen=80):
    """Attachment filenames go into filesystem paths — neutralize separators etc."""
    name = os.path.basename(name or "file")
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    return name[:maxlen] or "file"


def sticker_meta(stickers):
    """Rich archive metadata for a message's stickers. Name alone is useless
    for recovery — the id/url is what lets us grab the image later."""
    return [{"id": str(s.id), "name": s.name,
             "format": getattr(getattr(s, "format", None), "name", None),
             "url": getattr(s, "url", None)} for s in stickers]


def parse_stickers(raw):
    """Archived sticker column -> [{'id','name','format','url'}]. Rows written
    before 2026-07-17 stored a bare name list — normalize both shapes."""
    try:
        data = json.loads(raw or "[]")
    except (ValueError, TypeError):
        return []
    out = []
    for s in data:
        if isinstance(s, str):
            out.append({"id": None, "name": s, "format": None, "url": None})
        elif isinstance(s, dict):
            out.append({"id": s.get("id"), "name": s.get("name") or "?",
                        "format": s.get("format"), "url": s.get("url")})
    return out


def poll_meta(poll):
    """Archive metadata for a Discord poll. Polls carry no message.content, no
    attachments and no stickers, so without this a deleted poll archives as a
    fully EMPTY row and its delete log shows nothing at all (same failure
    shape as the sticker bug, hit live 2026-07-20)."""
    return {"question": poll.question or "",
            "answers": [{"text": a.text or "",
                         "emoji": str(a.emoji) if a.emoji else None}
                        for a in poll.answers],
            "multiple": bool(getattr(poll, "multiple", False)),
            "expires_ts": poll.expires_at.timestamp()
            if getattr(poll, "expires_at", None) else None}


def forward_meta(message):
    """Metadata for a forwarded message (message_snapshots) — the other
    message type whose own content is empty. The forwarded text lives in the
    snapshot; the origin channel/message id in message.reference."""
    snaps = list(getattr(message, "message_snapshots", None) or [])
    if not snaps:
        return None
    s = snaps[0]
    ref = getattr(message, "reference", None)
    return {"content": s.content or "",
            "attachments": [a.filename for a in s.attachments],
            "stickers": [st.name for st in s.stickers],
            "origin_channel_id": str(ref.channel_id) if ref else None,
            "origin_message_id": str(ref.message_id)
            if ref and ref.message_id else None}


def parse_json_obj(raw):
    """Archived json-dict column (poll/forward) -> dict or None. NULL for all
    rows written before 2026-07-20; tolerates garbage."""
    try:
        data = json.loads(raw or "null")
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def format_poll(meta, joiner="\n"):
    """Human-readable poll rendering for embeds (newline) and transcripts."""
    if not meta:
        return ""
    parts = [meta.get("question") or "?"]
    for a in meta.get("answers") or []:
        e = f"{a.get('emoji')} " if a.get("emoji") else ""
        parts.append(f"• {e}{a.get('text') or ''}".rstrip())
    if meta.get("multiple"):
        parts.append("(multiple answers allowed)")
    return joiner.join(parts)


def format_forward(meta):
    """Human-readable forwarded-snapshot rendering for the delete embed."""
    if not meta:
        return ""
    parts = []
    if meta.get("origin_channel_id"):
        parts.append(f"from <#{meta['origin_channel_id']}>")
    if meta.get("content"):
        parts.append(_trunc(meta["content"], 800))
    if meta.get("attachments"):
        parts.append("[attachments: " + ", ".join(meta["attachments"]) + "]")
    if meta.get("stickers"):
        parts.append("[stickers: " + ", ".join(meta["stickers"]) + "]")
    return "\n".join(parts) or "(empty snapshot)"


def flood_update(times, now, window=SELFDEL_WINDOW, limit=SELFDEL_THRESHOLD):
    """Rolling self-delete counter for one member. Returns (pruned times incl.
    now, crossed) — crossed is True exactly once, on the delete that reaches
    the limit; the episode machinery owns everything after that."""
    times = [t for t in times if now - t <= window]
    times.append(now)
    return times, len(times) == limit


def media_display_name(path, atts):
    """Original filename for a cached media file. Cache names are
    '{message_id}_{i}_{fname}' for attachments and '{message_id}_s{i}_{name}.{ext}'
    for stickers; a digit token maps back into the attachment list (positional
    zip would drift when an oversized attachment was skipped)."""
    parts = os.path.basename(path).split("_", 2)
    tok = parts[1] if len(parts) == 3 else ""
    if tok.isdigit() and int(tok) < len(atts):
        return atts[int(tok)].get("filename") or parts[-1]
    return parts[-1]


# Only renderable media is ever re-uploaded to a log channel. Anything else
# (exe, zip, apk, pdf, scripts…) is summarized name+size+sha256 instead — the
# bot must never re-distribute a potentially hostile file under its own name
# to staff. The disk copy stays in media_cache for forensics until pruned.
REPOST_MEDIA_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".bmp",
                     ".mp4", ".mov", ".webm",
                     ".mp3", ".ogg", ".wav", ".m4a", ".flac"}


def is_repostable(path, atts):
    """True if this cached file is image/video/audio and safe to re-upload.
    Checks the original filename's extension first, then the content_type
    Discord recorded at post time (covers extensionless downloads)."""
    ext = os.path.splitext(media_display_name(path, atts).lower())[1]
    if ext in REPOST_MEDIA_EXTS:
        return True
    parts = os.path.basename(path).split("_", 2)
    tok = parts[1] if len(parts) == 3 else ""
    if tok.isdigit() and int(tok) < len(atts):
        ct = (atts[int(tok)].get("content_type") or "").lower()
        return ct.split("/")[0] in ("image", "video", "audio")
    return False


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def match_delete_entry(entries, cache, channel_id, author_id, now_ts,
                       fresh_window=AUDIT_FRESH_WINDOW):
    """Attribute a deletion from audit-log entries (newest first, as dicts:
    id/user_id/user_name/target_id/channel_id/count/created_ts).

    `cache` maps entry_id -> last seen count and is UPDATED with every entry we
    see (that update is the whole trick — it's how a count bump on an old
    aggregated entry becomes visible). Evidence = an entry matching the deleted
    message's channel + author that is either brand new AND fresh, or whose
    count grew since we last saw it. author_id=None (bulk) skips the author
    check. Returns the matching entry dict or None (= self-delete)."""
    hit = None
    for en in entries:
        prev = cache.get(en["id"])
        grew = prev is not None and en["count"] > prev
        fresh = prev is None and (now_ts - en["created_ts"]) <= fresh_window
        cache[en["id"]] = en["count"]
        matches = (en["channel_id"] == channel_id
                   and (author_id is None or en["target_id"] == author_id))
        if hit is None and matches and (grew or fresh):
            hit = en
    return hit


def build_transcript(rows, guild_name=""):
    """Chronological plain-text transcript of bulk-deleted messages, from
    archive rows (dicts). Uncached ids are listed at the end."""
    lines = [f"Bulk-deleted messages — {guild_name}", "=" * 60]
    for r in sorted(rows, key=lambda r: int(r["message_id"])):
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(r["created_ts"] or 0))
        atts = ""
        try:
            names = [a.get("filename", "?") for a in json.loads(r["attachments"] or "[]")]
            if names:
                atts = f"  [attachments: {', '.join(names)}]"
        except (ValueError, TypeError):
            pass
        st = parse_stickers(r.get("stickers") if isinstance(r, dict) else None)
        if st:
            atts += f"  [stickers: {', '.join(s['name'] for s in st)}]"
        pm = parse_json_obj(r.get("poll") if isinstance(r, dict) else None)
        if pm:
            atts += f"  [poll: {format_poll(pm, joiner=' | ')}]"
        fw = parse_json_obj(r.get("forward") if isinstance(r, dict) else None)
        if fw:
            atts += f"  [forwarded: {_trunc(fw.get('content') or '', 200)}]"
        lines.append(f"[{ts}] {r['author_name'] or r['author_id']} ({r['author_id']}): "
                     f"{r['content'] or ''}{atts}")
    return "\n".join(lines) + "\n"


def files_to_prune(entries, cutoff_ts):
    """entries = [(path, mtime)]; return paths older than cutoff. Pure for tests."""
    return [p for p, m in entries if m < cutoff_ts]


def files_to_evict(entries, max_bytes):
    """entries = [(path, mtime, size)]; if the total exceeds max_bytes, return
    oldest-first paths to delete until back under the cap. Pure for tests."""
    total = sum(s for _, _, s in entries)
    evict = []
    for path, _, size in sorted(entries, key=lambda e: e[1]):
        if total <= max_bytes:
            break
        evict.append(path)
        total -= size
    return evict


def perm_diff_lines(b_allow, b_deny, a_allow, a_deny):
    """Human-readable lines for a permission-overwrite change. Inputs are
    {perm_name: bool} dicts (dict(discord.Permissions)). A perm can move
    between three states — allowed / denied / inherit — so the diff is three
    buckets: newly allowed, newly denied, and reset-to-inherit (cleared from
    allow or deny without landing in the other). Empty list = nothing changed."""
    def on(d):
        return {p for p, v in (d or {}).items() if v}
    ba, bd, aa, ad = on(b_allow), on(b_deny), on(a_allow), on(a_deny)
    lines = []
    if aa - ba:
        lines.append("✅ allow: " + ", ".join(f"`{p}`" for p in sorted(aa - ba)))
    if ad - bd:
        lines.append("⛔ deny: " + ", ".join(f"`{p}`" for p in sorted(ad - bd)))
    inherit = ((ba - aa) | (bd - ad)) - aa - ad
    if inherit:
        lines.append("↔️ inherit: " + ", ".join(f"`{p}`" for p in sorted(inherit)))
    return lines


# When OUR bot bulk-deletes on a mod's behalf (/prune-messages), the audit log
# names the bot and Discord drops the X-Audit-Log-Reason on bulk deletes
# (verified live) — so the moderation cog registers the invoker here instead.
_bot_purges = {}  # channel_id(str) -> (mod_id, mod_name, ts)


def note_bot_purge(channel_id, mod_id, mod_name, now=None):
    _bot_purges[str(channel_id)] = (int(mod_id), mod_name, now or time.time())


def purge_invoker(purges, channel_id, now, window=60.0):
    """The mod who asked the bot to purge this channel just now, or None."""
    rec = purges.get(str(channel_id))
    if rec and now - rec[2] <= window:
        return rec[0], rec[1]
    return None


def _deleter_name(hit):
    """Deleter for the archive row. Audit reasons matter when a BOT performed
    the deletion on a human's behalf (/prune-messages stamps
    '/prune-messages by mod (id)' — the reason is the real WHO)."""
    return hit["user_name"] + (f" — {hit['reason']}" if hit.get("reason") else "")


def _deleter_line(hit):
    """Deleter field for log embeds, reason included when present."""
    line = f"<@{hit['user_id']}> (`{hit['user_id']}`)"
    if hit.get("reason"):
        line += f"\n📝 {_trunc(hit['reason'], 480)}"
    return line


class ModLog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._pending = OrderedDict()   # message_id -> row dict, awaiting flush
        self._recent = OrderedDict()    # message_id -> row dict (same objects)
        self._audit_cache = {}          # audit entry_id -> last seen count
        self._audit_lock = asyncio.Lock()
        self._primed = set()            # guild ids whose audit cache is primed
        self._removals = {}             # user_id -> kick/ban audit record (classifies member_remove)
        self._removal_ids_seen = set()  # audit entry ids consumed by the fallback poll
        self._role_changes = {}         # user_id -> member_role_update audit record (attributes role diffs)
        self._member_updates = {}       # user_id -> member_update audit record (attributes nick + timeout)
        self._rolelog_hits = {}         # user_id -> [timestamps] for role-log rate limiting
        self._rolelog_cd = {}           # user_id -> cooldown-until ts (logs suppressed while spamming)
        self._bytes_since_cap_check = 0  # fresh cache writes since the last size-cap enforcement
        self._selfdel = {}              # (guild_id, author_id) -> {"times": [...], "episode": {...}|None}
        os.makedirs(MEDIA_DIR, exist_ok=True)
        with self._conn() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute(
                """CREATE TABLE IF NOT EXISTS messages (
                       message_id  TEXT PRIMARY KEY,
                       guild_id    TEXT, channel_id TEXT,
                       author_id   TEXT, author_name TEXT,
                       bot         INTEGER, webhook INTEGER,
                       created_ts  REAL,
                       content     TEXT,
                       reply_to    TEXT,
                       attachments TEXT,           -- json [{filename,url,size,content_type}]
                       stickers    TEXT,
                       deleted_ts  REAL,
                       deleted_by  TEXT, deleted_by_name TEXT,
                       delete_kind TEXT            -- 'self' | 'mod' | 'bulk' | 'unknown'
                   )""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_msgs_chan ON messages(guild_id, channel_id, created_ts)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_msgs_author ON messages(guild_id, author_id, created_ts)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_msgs_deleted ON messages(guild_id, deleted_ts)")
            # 2026-07-20: polls + forwarded messages archive as empty rows without these
            cols = {r[1] for r in c.execute("PRAGMA table_info(messages)")}
            for col in ("poll", "forward"):
                if col not in cols:
                    c.execute(f"ALTER TABLE messages ADD COLUMN {col} TEXT")
            c.execute(
                """CREATE TABLE IF NOT EXISTS edits (
                       message_id TEXT, guild_id TEXT,
                       edited_ts REAL, old_content TEXT, new_content TEXT
                   )""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_edits_msg ON edits(message_id)")
            # 2026-07-28: identity/lifecycle ledger. Embeds in a log channel are
            # NOT searchable — a 2025 investigation stalled because the only
            # record of a deleted account's names and numeric id lived inside
            # Carl-bot embeds, which no SQL query can see. Everything this cog
            # logs about a PERSON is mirrored here in plain text, keyed by uid,
            # so a deleted account's history stays greppable forever.
            c.execute(
                """CREATE TABLE IF NOT EXISTS identity_events (
                       ts        REAL,
                       guild_id  TEXT,
                       uid       TEXT,
                       username  TEXT,     -- account name as seen at event time
                       kind      TEXT,     -- nick|username|global_name|avatar|timeout|
                                           -- untimeout|join|leave|kick|ban|unban|roles
                       before    TEXT,
                       after     TEXT,
                       by_uid    TEXT,
                       by_name   TEXT,
                       reason    TEXT
                   )""")
            # Avatar bytes, deduped by Discord's asset hash. Stored as a BLOB
            # rather than base64 — SQLite handles small blobs faster than the
            # filesystem and base64 would inflate every row by a third for
            # nothing. The point is that a CDN avatar URL dies with the account:
            # if we only kept the hash, a deleted adversary's picture is gone.
            c.execute(
                """CREATE TABLE IF NOT EXISTS avatar_blobs (
                       hash        TEXT PRIMARY KEY,
                       uid         TEXT,
                       first_seen  REAL,
                       content_type TEXT,
                       size        INTEGER,
                       data        BLOB
                   )""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_avatar_uid ON avatar_blobs(uid)")
            # Who agreed to us keeping their server's data, when, and to which
            # version of the terms. Kept in the same DB as the data it licenses,
            # so a copy of the archive always carries its own permission slip.
            c.execute(
                """CREATE TABLE IF NOT EXISTS archive_consent (
                       guild_id    TEXT PRIMARY KEY,
                       guild_name  TEXT,
                       uid         TEXT,           -- who accepted (guild owner)
                       username    TEXT,
                       accepted_ts REAL,
                       version     INTEGER,
                       revoked_ts  REAL
                   )""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_ident_uid ON identity_events(uid, ts)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_ident_kind ON identity_events(guild_id, kind, ts)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_ident_after ON identity_events(after)")
            # 2026-08-23: Logging Pro entitlement. One row per guild; the web
            # checkout (or the operator by hand) extends expires_ts. Consent is
            # a separate table on purpose — paying and agreeing are different
            # acts by possibly different people, and both are required.
            c.execute(
                """CREATE TABLE IF NOT EXISTS archive_pro (
                       guild_id    TEXT PRIMARY KEY,
                       guild_name  TEXT,
                       expires_ts  REAL,           -- NULL = no expiry
                       granted_ts  REAL,
                       granted_by  TEXT,
                       note        TEXT,
                       order_ref   TEXT            -- web order id, for idempotent grants
                   )""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_edits_guild_ts ON edits(guild_id, edited_ts)")
        self._load_consent()
        self._load_pro()
        self._guild_bytes = {}          # guild_id -> cached-file bytes on disk (lazy, kept current)

    def _load_consent(self):
        """Refill the acceptance cache from disk. Consulted per message, so it
        must never touch the DB on the hot path."""
        _CONSENT.clear()
        try:
            with self._conn() as c:
                for r in c.execute("SELECT * FROM archive_consent WHERE revoked_ts IS NULL"):
                    _CONSENT[str(r["guild_id"])] = dict(r)
        except Exception:
            pass  # a consent read that fails must fail CLOSED: no rows = no retention
        self._export_tiers()

    def _load_pro(self):
        """Refill the entitlement cache. Same fail-closed rule as consent."""
        _PRO.clear()
        try:
            with self._conn() as c:
                for r in c.execute("SELECT * FROM archive_pro"):
                    _PRO[str(r["guild_id"])] = dict(r)
        except Exception:
            pass
        self._export_tiers()

    def _export_tiers(self):
        """Publish each guild's retention tier for the dashboard's Mod Logs
        page (same no-drift pipe as ai-energy.json: written from the live
        tables, never hand-maintained). The dashboard can't read messages.db
        and shouldn't — this is the only thing it needs from it. Best-effort."""
        path = os.getenv("TORVEX_MSGLOG_TIERS_JSON", "/var/lib/torvex/msglog-tiers.json")
        try:
            guilds = {}
            for gid in set(_PRO) | set(_CONSENT) | set(ARCHIVE_GUILDS):
                row = _PRO.get(gid) or {}
                guilds[gid] = {
                    "tier": retention_tier(gid),
                    "pro_expires_ts": row.get("expires_ts"),
                    "pro_active": pro_active(gid),
                    "terms_accepted": consent_ok(gid),
                }
            payload = {
                "recent_hours": RECENT_HOURS, "recent_media_mb": RECENT_MEDIA_MB,
                "pro_text_days": PRO_TEXT_DAYS, "pro_media_days": PRO_MEDIA_DAYS,
                "pro_media_mb": PRO_MEDIA_MB, "pro_url": PRO_URL,
                "terms_version": TERMS_VERSION,
                "guilds": guilds, "generated_at": int(time.time()),
            }
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=1)
            os.replace(tmp, path)
        except Exception as e:
            # Never raise: this runs from _load_pro/_load_consent, which run
            # from __init__. A docs artifact must not be able to stop the cog
            # from loading (deletes, edits, joins would all stop being logged).
            print(f"[mod_log] tier export skipped: {e!r}")

    # ------------------------------------------------------------- media layout
    # Files live in a per-guild subdirectory (media_cache/<guild_id>/...) so a
    # guild's usage is one directory size and its purge is one rmtree. The old
    # flat layout is still READ (operator guild's pre-existing cache) and is
    # aged out by the pruner like before.
    def _media_dir(self, guild_id, create=False):
        d = os.path.join(MEDIA_DIR, str(guild_id))
        if create:
            os.makedirs(d, exist_ok=True)
        return d

    def _guild_media_bytes(self, guild_id):
        gid = str(guild_id)
        if gid not in self._guild_bytes:
            total = 0
            try:
                for e in os.scandir(self._media_dir(gid)):
                    if e.is_file():
                        total += e.stat().st_size
            except OSError:
                pass
            self._guild_bytes[gid] = total
        return self._guild_bytes[gid]

    def _note_media_bytes(self, guild_id, delta):
        gid = str(guild_id)
        self._guild_bytes[gid] = max(0, self._guild_media_bytes(gid) + delta)

    def _remove_media_file(self, path):
        """Delete a cached file and keep the per-guild byte count honest."""
        try:
            size = os.path.getsize(path)
            os.remove(path)
        except OSError:
            return False
        parent = os.path.basename(os.path.dirname(path))
        if parent.isdigit():
            self._note_media_bytes(parent, -size)
        return True

    def _conn(self):
        c = sqlite3.connect(DB_PATH, timeout=30)
        c.row_factory = sqlite3.Row
        return c

    async def cog_load(self):
        self.flusher.start()
        self.retention_sweeper.start()
        # Prime the audit cache BEFORE any delete event arrives. Priming lazily
        # at event time is a bug: the fetch would cache the very entry we're
        # trying to attribute, making it look already-seen.
        self._prime_task = asyncio.create_task(self._prime_all())

    async def _prime_all(self):
        await self.bot.wait_until_ready()
        for gid in all_enabled("msglog"):
            guild = self.bot.get_guild(gid)
            if guild is not None:
                await self._prime_audit(guild)

    async def cog_unload(self):
        self.flusher.cancel()
        self.retention_sweeper.cancel()
        self._prime_task.cancel()
        self._flush()

    # ------------------------------------------------------------- archive
    def _remember(self, row):
        mid = row["message_id"]
        self._pending[mid] = row
        self._recent[mid] = row
        while len(self._recent) > RECENT_CAP:
            self._recent.popitem(last=False)

    def _flush(self):
        if not self._pending:
            return
        items = list(self._pending.values())
        self._pending.clear()
        try:
            with self._conn() as c:
                c.executemany(
                    """INSERT OR IGNORE INTO messages
                       (message_id,guild_id,channel_id,author_id,author_name,bot,webhook,
                        created_ts,content,reply_to,attachments,stickers,poll,forward,
                        deleted_ts,deleted_by,deleted_by_name,delete_kind)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    [(r["message_id"], r["guild_id"], r["channel_id"], r["author_id"],
                      r["author_name"], r["bot"], r["webhook"], r["created_ts"],
                      r["content"], r["reply_to"], r["attachments"], r["stickers"],
                      r.get("poll"), r.get("forward"),
                      r.get("deleted_ts"), r.get("deleted_by"), r.get("deleted_by_name"),
                      r.get("delete_kind")) for r in items])
        except Exception:
            for r in items:  # retry on next flush
                self._pending.setdefault(r["message_id"], r)

    @tasks.loop(seconds=FLUSH_SECONDS)
    async def flusher(self):
        self._flush()

    @flusher.before_loop
    async def _before_flush(self):
        await self.bot.wait_until_ready()

    def _row_from_db(self, message_id):
        self._flush()
        with self._conn() as c:
            r = c.execute("SELECT * FROM messages WHERE message_id=?", (str(message_id),)).fetchone()
        return dict(r) if r else None

    def _get_row(self, message_id):
        return self._recent.get(str(message_id)) or self._row_from_db(message_id)

    def _mark_deleted(self, message_id, kind, by_id=None, by_name=None):
        mid = str(message_id)
        now = time.time()
        row = self._recent.get(mid) or self._pending.get(mid)
        if row is not None:
            row.update(deleted_ts=now, delete_kind=kind,
                       deleted_by=str(by_id) if by_id else None, deleted_by_name=by_name)
        with self._conn() as c:  # no-op if the row is still unflushed; flush writes the dict
            c.execute("UPDATE messages SET deleted_ts=?, delete_kind=?, deleted_by=?, deleted_by_name=? "
                      "WHERE message_id=?",
                      (now, kind, str(by_id) if by_id else None, by_name, mid))

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or not is_enabled(message.guild.id, "msglog"):
            return
        # Every msglog guild is remembered — for the recent window at least. The
        # sweeper, not this path, decides how long the row lives (tier 0: hours).
        atts = [{"filename": a.filename, "url": a.url, "size": a.size,
                 "content_type": a.content_type} for a in message.attachments]
        fwd = forward_meta(message)
        self._remember({
            "message_id": str(message.id), "guild_id": str(message.guild.id),
            "channel_id": str(message.channel.id),
            "author_id": str(message.author.id), "author_name": str(message.author),
            "bot": 1 if message.author.bot else 0,
            "webhook": 1 if message.webhook_id else 0,
            "created_ts": message.created_at.timestamp(),
            "content": message.content or "",
            "reply_to": str(message.reference.message_id)
            if message.reference and message.reference.message_id else None,
            "attachments": json.dumps(atts) if atts else None,
            "stickers": json.dumps(sticker_meta(message.stickers)) if message.stickers else None,
            "poll": json.dumps(poll_meta(message.poll)) if message.poll else None,
            "forward": json.dumps(fwd) if fwd else None,
        })
        if atts or message.stickers:
            await self._cache_media(message)

    async def _cache_media(self, message):
        # Every tier caches files now — the recent window is what makes "deleted
        # image re-posted" work for a server that just added the bot. What
        # differs per tier is the per-guild ceiling and how soon it's swept.
        gid = message.guild.id
        cfg = get_config(gid)
        if not cfg.get("msglog_media"):
            return
        cap = int(cfg.get("msglog_media_max_mb", 25)) * 1024 * 1024
        guild_cap = guild_media_cap_bytes(gid)
        ddir = self._media_dir(gid, create=True)

        def room_for(size):
            # Per-guild ceiling (tier 0/1). Newest-wins would let an attacker
            # churn a mod's evidence out; oldest-wins means a full cache just
            # stops taking files until the sweeper frees space. Either way the
            # delete embed says "not stored" rather than pretending.
            if guild_cap is None:
                return True
            return self._guild_media_bytes(gid) + size <= guild_cap

        for i, att in enumerate(message.attachments):
            if att.size > cap or not room_for(att.size):
                continue
            path = os.path.join(ddir, f"{message.id}_{i}_{safe_filename(att.filename)}")
            try:
                await att.save(path)
            except Exception:
                continue  # CDN hiccup — the attachment URL metadata is still archived
            self._note_media_bytes(gid, att.size)
            # burst guard: the 12h pruner can't outrun a deliberate fill run, so
            # re-check the whole-cache cap every ~512MB of fresh writes
            self._bytes_since_cap_check += att.size
            if self._bytes_since_cap_check >= 512 * 1024 * 1024:
                self._bytes_since_cap_check = 0
                self._enforce_media_cap()
        # stickers too: a sticker message has no content and no attachments, so
        # without this the delete log would come out EMPTY (they're tiny, ≤512KB)
        for i, s in enumerate(message.stickers):
            ext = getattr(s.format, "file_extension", "png")
            if ext == "json":
                continue  # Lottie — no image file to re-post
            if not room_for(512 * 1024):
                continue
            path = os.path.join(ddir, f"{message.id}_s{i}_{safe_filename(s.name)}.{ext}")
            try:
                data = await s.read()
                with open(path, "wb") as f:
                    f.write(data)
                self._note_media_bytes(gid, len(data))
            except Exception:
                pass  # the archived sticker url is still a recovery path

    def _cached_media(self, message_id, guild_id=None):
        paths = glob.glob(os.path.join(MEDIA_DIR, f"{message_id}_*"))  # legacy flat layout
        if guild_id is not None:
            paths += glob.glob(os.path.join(self._media_dir(guild_id), f"{message_id}_*"))
        else:
            paths += glob.glob(os.path.join(MEDIA_DIR, "*", f"{message_id}_*"))
        return sorted(paths)

    def _all_media_entries(self):
        """(path, mtime, size) for every cached file, flat layout + per-guild dirs."""
        out = []
        try:
            for e in os.scandir(MEDIA_DIR):
                if e.is_file():
                    st = e.stat()
                    out.append((e.path, st.st_mtime, st.st_size))
                elif e.is_dir():
                    try:
                        for f in os.scandir(e.path):
                            if f.is_file():
                                st = f.stat()
                                out.append((f.path, st.st_mtime, st.st_size))
                    except OSError:
                        pass
        except OSError:
            pass
        return out

    # ------------------------------------------------------------- audit attribution
    @staticmethod
    def _entry_dict(e, bulk=False):
        extra_ch = getattr(getattr(e, "extra", None), "channel", None)
        return {"id": e.id,
                "user_id": e.user.id if e.user else None,
                "user_name": str(e.user) if e.user else "?",
                "target_id": None if bulk else getattr(e.target, "id", None),
                "channel_id": getattr(e.target, "id", None) if bulk
                else getattr(extra_ch, "id", None),
                "count": int(getattr(getattr(e, "extra", None), "count", 1) or 1),
                "reason": getattr(e, "reason", None),
                "created_ts": e.created_at.timestamp()}

    async def _fetch_entries(self, guild, action, bulk=False, limit=12):
        out = []
        try:
            async for e in guild.audit_logs(limit=limit, action=action):
                out.append(self._entry_dict(e, bulk=bulk))
        except discord.Forbidden:
            pass
        return out

    async def _prime_audit(self, guild):
        """Baseline the aggregation counts once per guild so a count bump on a
        pre-existing entry is attributable from the first deletion we see."""
        if guild.id in self._primed:
            return
        self._primed.add(guild.id)
        async with self._audit_lock:
            for action, bulk in ((discord.AuditLogAction.message_delete, False),
                                 (discord.AuditLogAction.message_bulk_delete, True)):
                for en in await self._fetch_entries(guild, action, bulk=bulk, limit=25):
                    self._audit_cache[en["id"]] = en["count"]

    async def _attribute(self, guild, channel_id, author_id, bulk=False):
        # No lazy priming here — see cog_load. An unprimed guild still works:
        # fresh entries attribute via the fresh_window path.
        await asyncio.sleep(AUDIT_WAIT)
        action = discord.AuditLogAction.message_bulk_delete if bulk \
            else discord.AuditLogAction.message_delete
        async with self._audit_lock:
            entries = await self._fetch_entries(guild, action, bulk=bulk)
            return match_delete_entry(entries, self._audit_cache, channel_id,
                                      None if bulk else author_id, time.time())

    # ------------------------------------------------------------- logging
    # Optional per-event destinations — every event class the log produces can be
    # routed to its own channel (the Dyno / carl-bot style split). Each falls back
    # to the one msglog channel, so a single-channel setup is unchanged and nothing
    # here can ever route into the security log by default.
    KIND_KEYS = {
        "members":      "msglog_members_channel_id",       # joins, leaves, kicks, bans, unbans
        "users":        "msglog_users_channel_id",         # username / nickname / avatar changes
        "member_roles": "msglog_member_roles_channel_id",  # roles added to / removed from a member
        "voice":        "msglog_voice_channel_id",         # voice join / leave / move, server mute / deafen
        "channels":     "msglog_channels_channel_id",      # channel create / delete / edit / overwrites
        "roles":        "msglog_roles_channel_id",         # role create / delete / edit
        "expressions":  "msglog_expressions_channel_id",   # emoji + sticker create / delete
        "automod":      "msglog_automod_channel_id",       # AutoMod blocks + rule changes
        "mod":          "mod_log_channel_id",              # timeouts — beside the moderation cog's own actions
    }

    def _log_channel(self, guild, cfg, kind="messages"):
        key = self.KIND_KEYS.get(kind)
        cid = ((cfg.get(key) if key else None) or cfg.get("msglog_channel_id")
               or cfg.get("modlog_channel_id"))
        return guild.get_channel(int(cid)) if cid else None

    def _media_channel(self, guild, cfg):
        """Separate destination for deleted-media re-posts (age-restricted staff
        channel). None = media stays attached to the log embeds as before."""
        cid = cfg.get("msglog_media_channel_id")
        return guild.get_channel(int(cid)) if cid else None

    def _skip_logging(self, cfg, channel_id, log_ch):
        if log_ch is not None and int(channel_id) == log_ch.id:
            return True  # never log the log channel — feedback loop
        for key in self.KIND_KEYS.values():
            v = cfg.get(key)
            if v and int(channel_id) == int(v):
                return True  # ...nor any of the split log channels
        mcid = cfg.get("msglog_media_channel_id")
        if mcid and int(channel_id) == int(mcid):
            return True  # ...nor the media channel
        return str(channel_id) in [str(x) for x in cfg.get("msglog_ignore_channels") or []]

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        if payload.guild_id is None or not is_enabled(payload.guild_id, "msglog"):
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        row = self._get_row(payload.message_id)
        if row is None and payload.cached_message is not None:
            m = payload.cached_message
            fwd = forward_meta(m)
            row = {"message_id": str(m.id), "guild_id": str(payload.guild_id),
                   "channel_id": str(payload.channel_id),
                   "author_id": str(m.author.id), "author_name": str(m.author),
                   "bot": 1 if m.author.bot else 0, "webhook": 1 if m.webhook_id else 0,
                   "created_ts": m.created_at.timestamp(), "content": m.content or "",
                   "reply_to": str(m.reference.message_id)
                   if m.reference and m.reference.message_id else None,
                   # Names/sizes only — nothing is fetched or written to disk.
                   # Without this a non-archiving guild's delete embed showed no
                   # sign an attachment was ever there, which reads as "there
                   # wasn't one" rather than "we didn't keep it".
                   "attachments": json.dumps(
                       [{"filename": a.filename, "url": a.url, "size": a.size,
                         "content_type": a.content_type} for a in m.attachments])
                   if m.attachments else None,
                   "stickers": json.dumps(sticker_meta(m.stickers)) if m.stickers else None,
                   "poll": json.dumps(poll_meta(m.poll)) if getattr(m, "poll", None) else None,
                   "forward": json.dumps(fwd) if fwd else None}

        author_id = int(row["author_id"]) if row else None
        hit = await self._attribute(guild, payload.channel_id, author_id)
        kind = "mod" if hit else ("self" if row else "unknown")
        self._mark_deleted(payload.message_id, kind,
                           by_id=hit and hit["user_id"], by_name=hit and _deleter_name(hit))

        cfg = get_config(payload.guild_id)
        log_ch = self._log_channel(guild, cfg)
        if not cfg.get("msglog_deletes") or log_ch is None:
            return
        # mass-self-delete tripwire BEFORE the per-channel ignore check — a
        # scrub is a scrub no matter which channel it happens in
        if kind == "self" and row and not row.get("bot"):
            if await self._track_selfdel(guild, row, payload.channel_id, cfg, log_ch):
                return  # active episode: individual embeds paused, summary comes later
        if self._skip_logging(cfg, payload.channel_id, log_ch):
            return

        # A message the author nukes within seconds is the strongest cheap
        # "they knew that was bad" signal there is, and the mass-scrub tripwire
        # can't see it — that needs 8 deletes in 5 minutes, and this is one.
        # bacon hair's slur (2026-07-11) lived 5.5s: archived, but nobody was
        # told, and it surfaced only in an unrelated investigation months later.
        fast = None
        if kind == "self" and row and not row.get("bot") and row.get("created_ts"):
            alive = time.time() - row["created_ts"]
            if 0 <= alive <= float(cfg.get("msglog_fastdel_secs", 20)):
                fast = alive

        if hit:
            title, color = "🛡️ Message deleted by moderator", COLOR_MOD_DELETE
        elif fast is not None:
            title, color = f"⚡ Deleted {fast:.0f}s after posting", COLOR_MOD_DELETE
        else:
            title, color = "🗑️ Message deleted", COLOR_SELF_DELETE
        embed = discord.Embed(title=title, color=color)
        if row:
            embed.add_field(name="Author",
                            value=f"<@{row['author_id']}> (`{row['author_id']}`)"
                                  + (" 🤖" if row.get("bot") else ""), inline=True)
            embed.add_field(name="Channel", value=f"<#{payload.channel_id}>", inline=True)
            embed.add_field(name="Sent", value=f"<t:{int(row['created_ts'])}:R>", inline=True)
            if row.get("content"):
                embed.add_field(name="Content", value=_trunc(row["content"]), inline=False)
                if MENTION_RE.search(row["content"]):
                    pinged = await self._mention_lines(guild, *extract_mentions(row["content"]))
                    embed.add_field(name="📣 Pinged", value=_trunc("\n".join(pinged)),
                                    inline=False)
            if row.get("reply_to"):  # reply ghost-ping: no mention markup, still pings
                ref = self._get_row(row["reply_to"])
                if ref:
                    embed.add_field(
                        name="↩️ Reply to",
                        value=f"**{ref['author_name']}** (`{ref['author_id']}`) "
                              f"— reply-pings the author unless suppressed",
                        inline=False)
            stickers = parse_stickers(row.get("stickers"))
            if stickers:  # sticker messages have no content — this WAS the "empty log" bug
                embed.add_field(
                    name="Sticker" if len(stickers) == 1 else "Stickers",
                    value=_trunc(", ".join(f"[{s['name']}]({s['url']})" if s["url"] else s["name"]
                                           for s in stickers), 1024),
                    inline=True)
            pm = parse_json_obj(row.get("poll"))
            if pm:  # polls are the other no-content message type (the "pole" case)
                embed.add_field(name="📊 Poll", value=_trunc(format_poll(pm), 1024),
                                inline=False)
            fw = parse_json_obj(row.get("forward"))
            if fw:
                embed.add_field(name="↪️ Forwarded message",
                                value=_trunc(format_forward(fw), 1024), inline=False)
        else:
            # No upsell here: this embed is read by another server's mods all day.
            # Pro is explained where an admin asks — /msglog pro, the dashboard.
            why = (f"older than the {RECENT_HOURS}h recovery window"
                   if not archives_messages(payload.guild_id) else "predates tracking")
            embed.description = (f"Message `{payload.message_id}` in <#{payload.channel_id}> "
                                 f"— **content not recoverable** ({why}).")
        if hit:
            embed.add_field(name="Deleted by", value=_deleter_line(hit), inline=False)
        files, quarantined = [], []
        atts = json.loads(row["attachments"]) if row and row.get("attachments") else []
        cached = self._cached_media(payload.message_id, payload.guild_id)
        for path in cached:
            if not is_repostable(path, atts):
                quarantined.append(path)
                continue
            if len(files) >= 9:
                break
            try:
                if os.path.getsize(path) <= guild.filesize_limit:
                    files.append(discord.File(path, filename=media_display_name(path, atts)))
            except OSError:
                pass
        if quarantined:
            lines = []
            for p in quarantined[:5]:
                try:
                    lines.append(f"`{media_display_name(p, atts)}` · {os.path.getsize(p):,} B\n"
                                 f"sha256 `{file_sha256(p)}`")
                except OSError:
                    lines.append(f"`{media_display_name(p, atts)}` · unreadable")
            if len(quarantined) > 5:
                lines.append(f"… +{len(quarantined) - 5} more")
            embed.add_field(
                name=f"⚠️ Non-media file{'s' if len(quarantined) != 1 else ''} — NOT re-posted",
                value=_trunc("\n".join(lines) + "\nKept on disk (media_cache) for review.", 1024),
                inline=False)
        media_ch = self._media_channel(guild, cfg)
        route_media = bool(files) and media_ch is not None and media_ch.id != log_ch.id
        # Say plainly what we did NOT keep. A log that lists an attachment while
        # silently having no copy of it reads like evidence right up until the
        # moment somebody needs the file.
        cap_mb = cfg.get("msglog_media_max_mb", 25)
        missing = unstored_attachments(atts, cached, cap_mb)
        if missing:
            lines = []
            for name, why, size in missing[:5]:
                reason = f"over the {cap_mb} MB cache limit" if why == "too large" else "not archived"
                lines.append(f"`{name}` · {reason}" + (f" ({size:,} B)" if size else ""))
            if len(missing) > 5:
                lines.append(f"… +{len(missing) - 5} more")
            if not archives_media(guild.id):
                lines.append(f"*Files are kept for {RECENT_HOURS}h ({RECENT_MEDIA_MB} MB per server).*")
            embed.add_field(
                name=f"📭 Not stored — unrecoverable ({len(missing)})",
                value=_trunc("\n".join(lines), 1024), inline=False)
        if route_media:
            embed.add_field(name="🖼️ Deleted media",
                            value=f"re-posted in {media_ch.mention}", inline=False)
        elif files:
            embed.add_field(name="🖼️ Deleted media re-posted below", value="​", inline=False)
        embed.set_footer(text=f"Message ID {payload.message_id}")
        ping = None
        if fast is not None and cfg.get("msglog_fastdel_ping", 0):
            # OFF by default: the retitled embed already carries the signal, and
            # an @ on every fast delete is noise on a busy server. Turn it on
            # where the failure mode matters more than the interruptions.
            who = cfg.get("msglog_alert_ping", guild.owner_id)
            if who:
                ping = f"<@&{who}>" if str(who) in {str(r.id) for r in guild.roles} else f"<@{who}>"
        await log_ch.send(
            content=ping, embed=embed,
            files=discord.utils.MISSING if route_media else (files or discord.utils.MISSING),
            allowed_mentions=discord.AllowedMentions(users=True, roles=True))
        if route_media:
            ref = discord.Embed(title="🖼️ Deleted media", color=color)
            ref.description = (f"From message `{payload.message_id}` in <#{payload.channel_id}>"
                               + (f", author <@{row['author_id']}>" if row else ""))
            ref.set_footer(text=f"Message ID {payload.message_id}")
            await media_ch.send(embed=ref, files=files,
                                allowed_mentions=discord.AllowedMentions.none())
        # The re-post in Discord IS the evidence now — the local copy only
        # existed for this moment. Drop it so the cache holds pending files,
        # not history. Quarantined (non-media) files stay for review.
        if files:
            for f in files:
                fp = getattr(f, "fp", None)
                name = getattr(fp, "name", None)
                try:
                    f.close()
                except Exception:
                    pass
                if name:
                    self._remove_media_file(name)

    # ---------------------------------------------------- mass self-delete tripwire
    def _alert_ping(self, guild, cfg):
        """Who to @ on a mass self-delete. msglog_alert_ping = role/user id,
        '0' to disable; default = the guild owner."""
        pid = cfg.get("msglog_alert_ping")
        if str(pid) == "0":
            return None
        if pid:
            return f"<@&{pid}>" if guild.get_role(int(pid)) else f"<@{pid}>"
        return f"<@{guild.owner_id}>"

    async def _track_selfdel(self, guild, row, channel_id, cfg, log_ch):
        """Count a self-delete; open an episode + fire the alert at threshold.
        Returns True while an episode is active (individual embeds paused)."""
        key = (guild.id, row["author_id"])
        st = self._selfdel.setdefault(key, {"times": [], "episode": None})
        now = time.time()
        st["times"], crossed = flood_update(st["times"], now)
        ep = st["episode"]
        if ep:
            ep["count"] += 1
            ep["last"] = now
            ep["channels"].add(str(channel_id))
            return True
        if not crossed:
            return False
        st["episode"] = {"count": len(st["times"]), "start": st["times"][0],
                         "last": now, "channels": {str(channel_id)}}
        member = guild.get_member(int(row["author_id"]))
        who = self._member_line(member) if member else \
            f"**{row.get('author_name') or '?'}** — <@{row['author_id']}> (`{row['author_id']}`)"
        embed = discord.Embed(
            title="🚨 Mass self-delete in progress", color=COLOR_BULK,
            description=f"{who}\nhas deleted **{len(st['times'])}** of their own messages "
                        f"in the last {int(SELFDEL_WINDOW / 60)} min — and counting. "
                        f"Self-deletes never hit the audit log, so only the archive sees this.")
        created = ((int(row["author_id"]) >> 22) + 1420070400000) / 1000
        embed.add_field(name="Account created", value=f"<t:{int(created)}:R>", inline=True)
        if member and member.joined_at:
            embed.add_field(name="Joined", value=f"<t:{int(member.joined_at.timestamp())}:R>",
                            inline=True)
        embed.add_field(
            name="What happens now",
            value="Per-message delete logs for this member are paused; a summary with a "
                  "full transcript posts when the run stops. The archive keeps everything.",
            inline=False)
        embed.set_footer(text=f"User ID {row['author_id']}")
        await log_ch.send(content=self._alert_ping(guild, cfg), embed=embed,
                          allowed_mentions=discord.AllowedMentions(users=True, roles=True))
        asyncio.create_task(self._selfdel_summary(guild, row["author_id"], key))
        return True

    async def _selfdel_summary(self, guild, author_id, key):
        """Wait for the deletion run to go quiet, then post one summary embed
        with a transcript of everything that was wiped in the episode."""
        while True:
            await asyncio.sleep(15)
            st = self._selfdel.get(key)
            ep = st and st["episode"]
            if ep is None:
                return
            if time.time() - ep["last"] >= SELFDEL_QUIET:
                break
        st["episode"] = None
        st["times"] = []
        self._flush()
        with self._conn() as c:
            rows = [dict(r) for r in c.execute(
                "SELECT * FROM messages WHERE guild_id=? AND author_id=? AND delete_kind='self' "
                "AND deleted_ts BETWEEN ? AND ? ORDER BY created_ts",
                (str(guild.id), str(author_id),
                 ep["start"] - SELFDEL_WINDOW, ep["last"] + 1))]
            lifetime = c.execute(
                "SELECT COUNT(*) FROM messages WHERE guild_id=? AND author_id=? "
                "AND deleted_ts IS NOT NULL", (str(guild.id), str(author_id))).fetchone()[0]
            total = c.execute(
                "SELECT COUNT(*) FROM messages WHERE guild_id=? AND author_id=?",
                (str(guild.id), str(author_id))).fetchone()[0]
        cfg = get_config(guild.id)
        log_ch = self._log_channel(guild, cfg)
        if log_ch is None:
            return
        member = guild.get_member(int(author_id))
        who = self._member_line(member) if member else f"<@{author_id}> (`{author_id}`)"
        embed = discord.Embed(
            title=f"🧨 Mass self-delete — {ep['count']} messages", color=COLOR_BULK,
            description=f"{who}\nRun lasted {max(1, int((ep['last'] - ep['start']) / 60))} min "
                        f"across {len(ep['channels'])} channel(s): "
                        + " ".join(f"<#{c}>" for c in sorted(ep["channels"])))
        embed.add_field(
            name="Lifetime",
            value=f"{lifetime} of {total} archived messages now deleted "
                  f"({lifetime / total:.0%})" if total else "—", inline=False)
        if not member:
            embed.add_field(name="⚠️", value="No longer in the server", inline=True)
        embed.set_footer(text=f"User ID {author_id} · transcript of the wiped messages attached")
        files = discord.utils.MISSING
        if rows:
            buf = io.BytesIO(build_transcript(rows, guild.name).encode("utf-8"))
            files = [discord.File(buf, filename=f"self_delete_{author_id}.txt")]
        await log_ch.send(embed=embed, files=files,
                          allowed_mentions=discord.AllowedMentions.none())

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent):
        if payload.guild_id is None or not is_enabled(payload.guild_id, "msglog"):
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        ids = [str(i) for i in payload.message_ids]
        self._flush()
        rows = []
        with self._conn() as c:
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                q = ",".join("?" * len(chunk))
                rows += [dict(r) for r in
                         c.execute(f"SELECT * FROM messages WHERE message_id IN ({q})", chunk)]
        hit = await self._attribute(guild, payload.channel_id, None, bulk=True)
        if hit and hit["user_id"] == self.bot.user.id:
            inv = purge_invoker(_bot_purges, payload.channel_id, time.time())
            if inv:  # credit the mod who ran /prune-messages, not the executor
                hit = dict(hit, user_id=inv[0],
                           user_name=f"{inv[1]} — via /prune-messages",
                           reason="/prune-messages (executed by the bot)")
        for mid in ids:
            self._mark_deleted(mid, "bulk",
                               by_id=hit and hit["user_id"], by_name=hit and _deleter_name(hit))

        cfg = get_config(payload.guild_id)
        log_ch = self._log_channel(guild, cfg)
        if not cfg.get("msglog_bulk") or log_ch is None \
                or self._skip_logging(cfg, payload.channel_id, log_ch):
            return
        embed = discord.Embed(
            title=f"🧹 Bulk delete — {len(ids)} messages", color=COLOR_BULK,
            description=f"In <#{payload.channel_id}> · **{len(rows)}** recovered from the archive"
                        + (f", {len(ids) - len(rows)} predate tracking" if len(rows) < len(ids) else ""))
        if hit:
            embed.add_field(name="Deleted by", value=_deleter_line(hit), inline=False)
        else:
            embed.add_field(name="Deleted by",
                            value="unattributed — possibly a ban's delete-days cascade", inline=False)
        media = [p for mid in ids for p in self._cached_media(mid, payload.guild_id)]
        if media:
            keep = (f"until the {RECENT_HOURS}h window sweeps them"
                    if retention_tier(payload.guild_id) == "recent" else "on disk")
            embed.add_field(name="Media", value=f"{len(media)} cached file(s) preserved {keep}",
                            inline=False)
        embed.set_footer(text=f"Channel ID {payload.channel_id}")
        files = discord.utils.MISSING
        if rows:
            buf = io.BytesIO(build_transcript(rows, guild.name).encode("utf-8"))
            files = [discord.File(buf, filename=f"bulk_delete_{payload.channel_id}.txt")]
        await log_ch.send(embed=embed, files=files,
                          allowed_mentions=discord.AllowedMentions.none())

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent):
        if payload.guild_id is None or not is_enabled(payload.guild_id, "msglog"):
            return
        guild = self.bot.get_guild(payload.guild_id)
        data = payload.data or {}
        if guild is None or "content" not in data:
            return  # partial update with no content — nothing to compare
        # Discord now sends the FULL message object on every MESSAGE_UPDATE, so
        # "content is present" no longer means "content changed". The reliable
        # tell is edited_timestamp: null on embed-unfurl / pin / component /
        # flag updates, set on a real user edit. Without this, a guild with no
        # stored copy of the message (old is None below) logged every GIF link's
        # unfurl as "Message edited — Before: not in archive" (xottic, 8/23).
        if not data.get("edited_timestamp"):
            return
        new = data.get("content") or ""
        row = self._get_row(payload.message_id)
        old = row["content"] if row else None
        if old is not None and old == new:
            return  # content unchanged (unfurl adds an embed, fires MESSAGE_UPDATE)
        edited_ts = time.time()
        row_in_mem = self._recent.get(str(payload.message_id))
        if row_in_mem is not None:
            row_in_mem["content"] = new
        # An edit row is message content like any other: it lives under the same
        # guild window as the message (the sweeper prunes edits by guild + ts),
        # so every tier records it — a free guild's second edit must show the
        # real "Before", not the original text.
        with self._conn() as c:
            c.execute("UPDATE messages SET content=? WHERE message_id=?",
                      (new, str(payload.message_id)))
            c.execute("INSERT INTO edits(message_id,guild_id,edited_ts,old_content,new_content) "
                      "VALUES (?,?,?,?,?)",
                      (str(payload.message_id), str(payload.guild_id), edited_ts, old, new))

        cfg = get_config(payload.guild_id)
        log_ch = self._log_channel(guild, cfg)
        if not cfg.get("msglog_edits") or log_ch is None \
                or self._skip_logging(cfg, payload.channel_id, log_ch):
            return
        author = data.get("author") or {}
        if (author.get("bot") or data.get("webhook_id")) and not cfg.get("msglog_log_bots"):
            return
        embed = discord.Embed(title="✏️ Message edited", color=COLOR_EDIT)
        aid = author.get("id") or (row and row["author_id"])
        if aid:
            embed.add_field(name="Author", value=f"<@{aid}> (`{aid}`)", inline=True)
        embed.add_field(name="Channel", value=f"<#{payload.channel_id}>", inline=True)
        embed.add_field(
            name="Jump",
            value=f"[to message](https://discord.com/channels/"
                  f"{payload.guild_id}/{payload.channel_id}/{payload.message_id})", inline=True)
        embed.add_field(name="Before",
                        value=_trunc(old) if old is not None else "*not in archive*", inline=False)
        embed.add_field(name="After", value=_trunc(new) or "*empty*", inline=False)
        if old:  # edit-away ghost ping: mention gone from the new content
            gu, gr, ge, gh = mentions_removed(old, new)
            if gu or gr or ge or gh:
                gone = await self._mention_lines(guild, gu, gr, ge, gh)
                embed.add_field(name="📣 Pings edited out",
                                value=_trunc("\n".join(gone)), inline=False)
        embed.set_footer(text=f"Message ID {payload.message_id}")
        await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    # ------------------------------------------------------------- retention sweep
    def _operator_media_cutoff(self, now):
        # Only the operator's own guilds may influence how long THEIR media is
        # kept — a granted tenant must not be able to extend retention on our disk.
        days = MEDIA_DAYS
        for gid in OPERATOR_GUILDS:
            days = max(days, int(get_config(gid).get("msglog_media_days", MEDIA_DAYS)))
        return now - days * 86400

    def _guild_windows(self, guild_id, now):
        """(text_cutoff, media_cutoff) for one guild — None = never swept."""
        gid = str(guild_id)
        if gid in ARCHIVE_GUILDS:
            return None, self._operator_media_cutoff(now)
        # Lapsed Pro keeps its archive for PRO_GRACE_DAYS so a late renewal
        # doesn't lose history; after that it is swept down to the free window.
        if consent_ok(gid) and pro_active(gid, now, grace_days=PRO_GRACE_DAYS):
            return now - PRO_TEXT_DAYS * 86400, now - PRO_MEDIA_DAYS * 86400
        return now - RECENT_HOURS * 3600, now - RECENT_HOURS * 3600

    def _sweep_rows(self, now):
        """Blocking: delete rows past their guild's window. Returns {gid: n}."""
        swept = {}
        with self._conn() as c:
            gids = {r[0] for r in c.execute("SELECT DISTINCT guild_id FROM messages")}
            gids |= {r[0] for r in c.execute("SELECT DISTINCT guild_id FROM identity_events")}
            for gid in gids:
                if not gid:
                    continue
                text_cutoff, _ = self._guild_windows(gid, now)
                if text_cutoff is None:
                    continue
                n = c.execute("DELETE FROM messages WHERE guild_id=? AND created_ts<?",
                              (gid, text_cutoff)).rowcount
                n += c.execute("DELETE FROM edits WHERE guild_id=? AND edited_ts<?",
                               (gid, text_cutoff)).rowcount
                if retention_tier(gid) == "recent":
                    # The ledger is a Pro feature; a lapsed/never-Pro guild keeps none.
                    n += c.execute("DELETE FROM identity_events WHERE guild_id=?", (gid,)).rowcount
                if n:
                    swept[gid] = n
            if swept:
                c.execute("DELETE FROM avatar_blobs WHERE uid NOT IN"
                          " (SELECT DISTINCT uid FROM identity_events)")
        # In-memory copies must not outlive the rows they mirror.
        for mid, row in list(self._recent.items()):
            text_cutoff, _ = self._guild_windows(row.get("guild_id"), now)
            if text_cutoff is not None and float(row.get("created_ts") or 0) < text_cutoff:
                self._recent.pop(mid, None)
        return swept

    def _sweep_files(self, now):
        """Blocking: age out cached files per guild window, then the global cap."""
        removed = 0
        legacy_cutoff = self._operator_media_cutoff(now)
        for path, mtime, _ in self._all_media_entries():
            parent = os.path.basename(os.path.dirname(path))
            if parent.isdigit() and parent not in ARCHIVE_GUILDS:
                _, media_cutoff = self._guild_windows(parent, now)
            else:
                media_cutoff = legacy_cutoff      # operator guilds + the old flat layout
            if media_cutoff is not None and mtime < media_cutoff:
                if self._remove_media_file(path):
                    removed += 1
        self._guild_bytes.clear()  # recomputed lazily from disk after a sweep
        return removed

    @tasks.loop(minutes=SWEEP_MINUTES)
    async def retention_sweeper(self):
        now = time.time()
        self._flush()
        try:
            swept = await asyncio.to_thread(self._sweep_rows, now)
            removed = await asyncio.to_thread(self._sweep_files, now)
        except Exception as e:  # a failed sweep must never kill the loop
            print(f"[mod_log] retention sweep failed: {e!r}")
            return
        await asyncio.to_thread(self._enforce_media_cap)
        if swept or removed:
            print(f"[mod_log] retention sweep: rows {sum(swept.values())} across "
                  f"{len(swept)} guild(s), files {removed}")

    def _enforce_media_cap(self):
        """Hard whole-cache size cap, oldest evicted first. Age retention alone
        leaves a disk-fill DoS open (a Nitro account can post ~250MB/message);
        this turns the worst case into 'attacker evicts old memes'."""
        # The cap is the OPERATOR's disk budget and comes from their environment
        # only. Reading it from guild config (even an allowlisted guild's) let a
        # granted server raise a shared global limit from its own settings page.
        cap_gb = MEDIA_CAP_GB
        entries = self._all_media_entries()
        evict = files_to_evict(entries, cap_gb * 1024 ** 3)
        for path in evict:
            self._remove_media_file(path)
        if evict:
            print(f"[mod_log] media cache over {cap_gb}GB cap — evicted {len(evict)} oldest file(s)")

    @retention_sweeper.before_loop
    async def _before_sweep(self):
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------- member lifecycle
    # Joins (with the invite used, read from the invites cog's attribution DB),
    # leaves, and kick/ban/unban with WHO + reason. Kick/ban executors come from
    # AUDIT_LOG_ENTRY_CREATE (real-time, carries the reason); on_member_remove
    # waits briefly for that record so a kick is never mislogged as a leave.

    def _members_log_channel(self, guild):
        if not is_enabled(guild.id, "msglog"):
            return None, None
        cfg = get_config(guild.id)
        if not cfg.get("msglog_members", 1):
            return None, None
        return self._log_channel(guild, cfg, "members"), cfg

    def _join_invite_line(self, uid, guild_id):
        """Latest invite attribution the invites cog recorded for this join."""
        try:
            c = sqlite3.connect(INVITES_DB, timeout=5)
            c.row_factory = sqlite3.Row
            r = c.execute("SELECT * FROM invite_attributions WHERE uid=? AND guild_id=? "
                          "ORDER BY joined_at DESC LIMIT 1", (str(uid), str(guild_id))).fetchone()
            c.close()
        except sqlite3.Error:
            return None
        if not r or time.time() - (r["joined_at"] or 0) > 300:
            return None  # stale row from a previous join — not this one
        if r["kind"] == "vanity":
            return "vanity URL"
        if r["kind"] == "discovery":
            return "Server Discovery / untracked"
        if r["code"]:
            line = f"`discord.gg/{r['code']}`"
            if r["inviter_id"]:
                line += f" from <@{r['inviter_id']}>"
            if r["label"] and r["kind"] in ("public", "tracked"):
                line += f" · “{r['label']}”"
            return line
        return None

    @staticmethod
    def _mod_line(rec):
        who = f"<@{rec['by_id']}>" if rec.get("by_id") else "?"
        if rec.get("by_name"):
            who += f" ({rec['by_name']})"
        return who

    @staticmethod
    def _member_line(member):
        # plain-text name first — mentions of users no longer in the server
        # render as @unknown-user, and the log must say WHO it was regardless
        return (f"**{member}** — {member.mention} (`{member.id}`)"
                + (" 🤖" if member.bot else ""))

    async def _mention_lines(self, guild, users, roles, everyone, here,
                             max_users=15, max_fetch=5):
        """Resolve mention ids to plain-text names for a ping field (same
        @unknown-user rationale as _member_line). fetch_user is capped so a
        paste full of dead ids can't turn one delete into an API storm."""
        lines, fetched = [], 0
        for uid in users[:max_users]:
            u = guild.get_member(int(uid)) or self.bot.get_user(int(uid))
            if u is None and fetched < max_fetch:
                fetched += 1
                try:
                    u = await self.bot.fetch_user(int(uid))
                except discord.HTTPException:
                    u = None
            lines.append(f"**{u}** (`{uid}`)" if u else f"unknown user (`{uid}`)")
        if len(users) > max_users:
            lines.append(f"…and {len(users) - max_users} more users")
        for rid in roles:
            r = guild.get_role(int(rid))
            lines.append(f"role **{r.name}** (`{rid}`)" if r else f"unknown role (`{rid}`)")
        if everyone:
            lines.append("**@everyone**")
        if here:
            lines.append("**@here**")
        return lines

    async def _user_line(self, target, target_id):
        """Same, from an audit-log target that may be a bare Object (ID only)."""
        name = str(target) if isinstance(target, (discord.User, discord.Member)) else None
        if name is None:
            try:
                name = str(await self.bot.fetch_user(target_id))
            except discord.HTTPException:
                pass
        return (f"**{name}** — " if name else "") + f"<@{target_id}> (`{target_id}`)"

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        guild = member.guild
        # Ledger first, unconditionally: the name↔uid pair at join time is the
        # single most valuable row we can hold on an account that later deletes.
        self._record_identity(guild.id, member, "join",
                              after=member.global_name or member.name)
        try:  # baseline the avatar at join so a later change has a "before"
            if member.avatar is not None:
                await self._store_avatar(member, member.avatar)
        except Exception:
            pass
        log_ch, _ = self._members_log_channel(guild)
        if log_ch is None:
            return
        await asyncio.sleep(3.0)  # let the invites cog attribute the join first
        embed = discord.Embed(
            title="📥 Member joined", color=COLOR_JOIN,
            description=self._member_line(member))
        age_days = (time.time() - member.created_at.timestamp()) / 86400
        flag = " · ⚠️ **new account**" if age_days < 7 else ""
        embed.add_field(name="Account created",
                        value=f"<t:{int(member.created_at.timestamp())}:R>{flag}", inline=True)
        embed.add_field(name="Members", value=f"{guild.member_count:,}", inline=True)
        inv = None if member.bot else self._join_invite_line(member.id, guild.id)
        if inv:
            embed.add_field(name="Invite used", value=inv, inline=False)
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.set_footer(text=f"User ID {member.id}")
        await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @commands.Cog.listener()
    async def on_audit_log_entry_create(self, entry: discord.AuditLogEntry):
        guild = entry.guild
        if guild is None or not is_enabled(guild.id, "msglog"):
            return
        A = discord.AuditLogAction
        if entry.action is A.member_role_update:
            self._note_role_change(entry)
            return
        automod_actions = tuple(
            getattr(A, n) for n in ("automod_rule_create", "automod_rule_update",
                                    "automod_rule_delete") if hasattr(A, n))
        if automod_actions and entry.action in automod_actions:
            # Disabling the slur filter is a silent, high-impact change: the
            # protection just stops existing and nothing else says so.
            await self._log_automod_rule_event(entry)
            return
        if entry.action is A.member_update:
            # nickname edits AND timeouts both land here; on_member_update reads
            # this cache to name the actor
            self._note_member_update(entry)
            return
        if entry.action in (A.role_create, A.role_delete, A.role_update):
            await self._log_guild_role_event(entry)
            return
        if entry.action in (A.channel_create, A.channel_delete, A.channel_update,
                            A.overwrite_create, A.overwrite_update, A.overwrite_delete):
            await self._log_channel_event(entry)
            return
        if entry.action in (A.emoji_create, A.emoji_delete, A.emoji_update,
                            A.sticker_create, A.sticker_delete, A.sticker_update):
            await self._log_expression_event(entry)
            return
        if entry.action not in (A.kick, A.ban, A.unban):
            return
        target_id = getattr(entry.target, "id", None)
        if target_id is None:
            return
        now = time.time()
        # lazy prune so the map can't grow unbounded
        self._removals = {k: v for k, v in self._removals.items() if now - v["ts"] < 300}
        rec = {"action": entry.action, "by_id": entry.user_id,
               "by_name": str(entry.user) if entry.user else None,
               "reason": entry.reason, "ts": now}
        if entry.action in (A.kick, A.ban):
            self._removals[target_id] = rec
        if entry.action is A.kick:
            return  # embed posted by on_member_remove — it still has roles/join date
        log_ch, _ = self._members_log_channel(guild)
        if log_ch is None:
            return
        banned = entry.action is A.ban
        self._record_identity(guild.id, entry.target or target_id,
                              "ban" if banned else "unban",
                              by_uid=rec["by_id"], by_name=rec["by_name"],
                              reason=rec["reason"])
        embed = discord.Embed(
            title="🔨 Member banned" if banned else "♻️ Member unbanned",
            color=COLOR_BAN if banned else COLOR_JOIN,
            description=await self._user_line(entry.target, target_id))
        embed.add_field(name="By", value=self._mod_line(rec), inline=True)
        embed.add_field(name="Reason", value=_trunc(rec["reason"] or "No reason provided"), inline=False)
        embed.set_footer(text=f"User ID {target_id}")
        await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    async def _log_automod_rule_event(self, entry):
        """AutoMod rule created / edited / deleted, with the diff."""
        guild = entry.guild
        cfg = get_config(guild.id)
        if not cfg.get("msglog_automod", 1):
            return
        log_ch = self._log_channel(guild, cfg, "automod")
        if log_ch is None:
            return
        A = discord.AuditLogAction
        if entry.action == getattr(A, "automod_rule_create", None):
            title, color = "🆕 AutoMod rule created", COLOR_JOIN
        elif entry.action == getattr(A, "automod_rule_delete", None):
            title, color = "🗑️ AutoMod rule deleted", COLOR_MOD_DELETE
        else:
            title, color = "⚙️ AutoMod rule changed", COLOR_ROLE
        name = getattr(entry.target, "name", None) or str(getattr(entry.target, "id", "?"))
        lines = [f"**{_plain(name)}**"]
        for change in (entry.changes or []):
            key = getattr(change, "key", "?")
            b, a = getattr(change, "before", None), getattr(change, "after", None)
            if key == "enabled":
                lines.append(f"• **enabled**: {b} → {a}"
                             + ("  ⚠️ **protection turned OFF**" if a is False else ""))
            else:
                lines.append(f"• `{key}`: {_trunc(_plain(str(b)), 120)} → "
                             f"{_trunc(_plain(str(a)), 120)}")
        embed = discord.Embed(title=title, color=color, description=_trunc("\n".join(lines), 3000))
        embed.add_field(name="By", value=self._mod_line(
            {"by_id": entry.user_id, "by_name": str(entry.user) if entry.user else None}),
            inline=True)
        if entry.reason:
            embed.add_field(name="Reason", value=_trunc(entry.reason), inline=False)
        await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    def _record_identity(self, guild_id, user, kind, before=None, after=None,
                         by_uid=None, by_name=None, reason=None):
        """Mirror one person-event into identity_events (plain text, uid-keyed).

        Deliberately best-effort and swallowing: the ledger must never be able
        to break the embed that the mods actually see.

        Scoped like the message archive (tier 2): a guild that hasn't accepted
        the terms still gets every join/leave/ban/rename embed live, but we keep
        no lasting history of their members.
        """
        if not archives_messages(guild_id):
            return
        try:
            uid = getattr(user, "id", user)
            uname = getattr(user, "name", None) or str(user)
            with self._conn() as c:
                c.execute(
                    "INSERT INTO identity_events"
                    " (ts, guild_id, uid, username, kind, before, after, by_uid, by_name, reason)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (time.time(), str(guild_id), str(uid), uname, kind,
                     None if before is None else str(before),
                     None if after is None else str(after),
                     None if by_uid is None else str(by_uid), by_name, reason))
        except Exception:
            pass

    def _note_member_update(self, entry):
        """Cache a member_update audit record so on_member_update can say WHO.

        Covers BOTH nickname edits and timeouts — Discord files them under the
        same action, so one cache serves both.
        """
        target_id = getattr(entry.target, "id", None)
        if target_id is None:
            return
        now = time.time()
        self._member_updates = {k: v for k, v in self._member_updates.items()
                                if now - v["ts"] < 300}
        self._member_updates[target_id] = {
            "by_id": entry.user_id,
            "by_name": str(entry.user) if entry.user else None,
            "reason": entry.reason, "ts": now}

    async def _who_changed_member(self, guild, member_id):
        """Actor behind a nick/timeout change: realtime cache first, then a
        one-shot audit poll (aggregated entries don't re-dispatch the gateway
        event — same gap the role logger handles)."""
        rec = None
        for attempt in range(3):
            if attempt:
                await asyncio.sleep(0.8)
            r = self._member_updates.get(member_id)
            if r and time.time() - r["ts"] < 30:
                return r
        try:
            async for e in guild.audit_logs(limit=8, action=discord.AuditLogAction.member_update):
                if getattr(e.target, "id", None) != member_id:
                    continue
                if time.time() - e.created_at.timestamp() < AUDIT_FRESH_WINDOW:
                    rec = {"by_id": e.user.id if e.user else None,
                           "by_name": str(e.user) if e.user else None,
                           "reason": e.reason}
                    break
        except discord.Forbidden:
            pass
        return rec

    def _note_role_change(self, entry):
        """Cache a member_role_update audit record so on_member_update can say WHO."""
        target_id = getattr(entry.target, "id", None)
        if target_id is None:
            return
        now = time.time()
        self._role_changes = {k: v for k, v in self._role_changes.items() if now - v["ts"] < 300}
        self._role_changes[target_id] = {
            "by_id": entry.user_id,
            "by_name": str(entry.user) if entry.user else None,
            "reason": entry.reason, "ts": now,
            # for member_role_update, changes.after.roles = added, before.roles = removed
            "added": {r.id for r in (getattr(entry.changes.after, "roles", None) or [])},
            "removed": {r.id for r in (getattr(entry.changes.before, "roles", None) or [])},
        }

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        # Nickname + timeout ride the same gateway event as role changes but are
        # logged independently (own config key, no rate limiter — a nick edit is
        # never a flood vector the way reaction-role toggles are).
        if before.nick != after.nick:
            await self._log_name_event(after, "nick", before.nick, after.nick)
        if getattr(before, "guild_avatar", None) != getattr(after, "guild_avatar", None):
            await self._log_avatar_event(after, before.guild_avatar, after.guild_avatar,
                                         kind="guild_avatar")
        if getattr(before, "timed_out_until", None) != getattr(after, "timed_out_until", None):
            await self._log_timeout_event(before, after)
        if before.roles == after.roles:
            return
        guild = after.guild
        if not is_enabled(guild.id, "msglog"):
            return
        cfg = get_config(guild.id)
        if not cfg.get("msglog_roles", 1):
            return
        if after.bot and not cfg.get("msglog_log_bots", 0):
            return
        log_ch = self._log_channel(guild, cfg, "member_roles")
        if log_ch is None:
            return
        added = [r for r in after.roles if r not in before.roles]
        removed = [r for r in before.roles if r not in after.roles]
        if not added and not removed:
            return
        changed_ids = {r.id for r in added} | {r.id for r in removed}

        # --- per-member role-log rate limit (anti-troll-flood) -------------
        # Runs BEFORE the audit attribution so a spammer short-circuits cheaply.
        # Mild bursts just pause this member's role logs for a short cooldown —
        # no action against them. A massive burst that's confirmed self-inflicted
        # reaction-role spam (bot actor + self-assign reason, read from the
        # realtime audit cache) is treated as a nuke and quarantined.
        now = time.time()
        hits = [t for t in self._rolelog_hits.get(after.id, ()) if now - t < ROLELOG_WINDOW]
        hits.append(now)
        self._rolelog_hits[after.id] = hits
        if len(hits) >= ROLELOG_NUKE:
            crec = self._role_changes.get(after.id)
            if crec and crec.get("by_id") == self.bot.user.id \
                    and "self-assign role menu" in (crec.get("reason") or "").lower():
                await self._quarantine_role_spammer(after, log_ch, len(hits))
            self._rolelog_cd[after.id] = now + ROLELOG_COOLDOWN
            self._rolelog_hits[after.id] = []
            return
        if now < self._rolelog_cd.get(after.id, 0):
            return  # in cooldown — suppress this member's role logs
        if len(hits) > ROLELOG_LIMIT:
            self._rolelog_cd[after.id] = now + ROLELOG_COOLDOWN
            note = discord.Embed(
                title="🎭 Role logs paused (rate limit)", color=COLOR_ROLE,
                description=f"{self._member_line(after)} changed roles {len(hits)}× "
                            f"in ~{int(ROLELOG_WINDOW)}s — pausing their role logs for "
                            f"{int(ROLELOG_COOLDOWN)}s. No action taken.")
            note.set_footer(text=f"User ID {after.id}")
            await log_ch.send(embed=note, allowed_mentions=discord.AllowedMentions.none())
            return

        # WHO: the audit event usually lands within a second; check the cache
        # immediately, then briefly wait. Aggregated entries (same mod changing
        # the same member again inside Discord's merge window) don't re-dispatch
        # the gateway event, so a one-shot poll covers that gap.
        rec = None
        for attempt in range(4):
            if attempt:
                await asyncio.sleep(1.0)
            r = self._role_changes.get(after.id)
            if r and time.time() - r["ts"] < 30:
                rec = r
                break
        if rec is None or not (changed_ids & (rec["added"] | rec["removed"])):
            try:
                async for e in guild.audit_logs(limit=8, action=discord.AuditLogAction.member_role_update):
                    if getattr(e.target, "id", None) != after.id:
                        continue
                    e_roles = {r.id for r in (getattr(e.changes.after, "roles", None) or [])} \
                            | {r.id for r in (getattr(e.changes.before, "roles", None) or [])}
                    if changed_ids & e_roles and time.time() - e.created_at.timestamp() < AUDIT_FRESH_WINDOW:
                        rec = {"by_id": e.user.id if e.user else None,
                               "by_name": str(e.user) if e.user else None, "reason": e.reason}
                        break
            except discord.Forbidden:
                pass

        # Bot-made role changes split two ways. The automated / self-documenting
        # systems — /levelroles sync + level rewards (bulk), AltGuard
        # quarantine/restore/join-defaults — fire constantly and keep their own
        # trail, so they stay out of the log. Reaction-role self-assigns are the
        # exception: the member clicked the button and the bot only applied it,
        # so they DO belong here (the Age/Pronouns panels most of all),
        # attributed to the member rather than to the bot.
        self_assign = False
        if rec and rec.get("by_id") == self.bot.user.id:
            if "self-assign role menu" in (rec.get("reason") or "").lower():
                self_assign = True
            else:
                return

        embed = discord.Embed(title="🎭 Roles updated", color=COLOR_ROLE,
                              description=self._member_line(after))
        if added:
            embed.add_field(name="Added", value=_trunc(" ".join(r.mention for r in added)), inline=False)
        if removed:
            embed.add_field(name="Removed", value=_trunc(" ".join(r.mention for r in removed)), inline=False)
        by = "🎭 Self-assigned (reaction role)" if self_assign \
            else (self._mod_line(rec) if rec else "? (no audit entry found)")
        embed.add_field(name="By", value=by, inline=True)
        if rec and rec.get("reason") and not self_assign:
            embed.add_field(name="Reason", value=_trunc(rec["reason"]), inline=False)
        embed.set_footer(text=f"User ID {after.id}")
        await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    async def _log_name_event(self, member, kind, old, new):
        """Nickname / username / global-name change → embed + ledger row.

        The ledger row is the point: a name is the only handle you have on an
        account that later deletes itself, and 'Before → After' in plain text is
        what makes the chain reconstructable years later.
        """
        guild = member.guild
        if not is_enabled(guild.id, "msglog"):
            return
        cfg = get_config(guild.id)
        if not cfg.get("msglog_names", 1):
            return
        if member.bot and not cfg.get("msglog_log_bots", 0):
            return
        rec = None
        if kind == "nick":
            rec = await self._who_changed_member(guild, member.id)
            # A member editing their own nick is self-service, not moderation;
            # only surface WHO when somebody else did it.
            if rec and rec.get("by_id") == member.id:
                rec = None
        self._record_identity(guild.id, member, kind, old, new,
                              by_uid=(rec or {}).get("by_id"),
                              by_name=(rec or {}).get("by_name"),
                              reason=(rec or {}).get("reason"))
        log_ch = self._log_channel(guild, cfg, "users")
        if log_ch is None:
            return
        title = {"nick": "🏷️ Nickname changed",
                 "username": "🪪 Username changed",
                 "global_name": "🪪 Display name changed"}.get(kind, "🪪 Name changed")
        embed = discord.Embed(title=title, color=COLOR_ROLE,
                              description=self._member_line(member))
        embed.add_field(name="Before", value=_trunc(_plain(old) or "*(none)*"), inline=True)
        embed.add_field(name="After", value=_trunc(_plain(new) or "*(none)*"), inline=True)
        if rec:
            embed.add_field(name="By", value=self._mod_line(rec), inline=False)
            if rec.get("reason"):
                embed.add_field(name="Reason", value=_trunc(rec["reason"]), inline=False)
        embed.set_footer(text=f"User ID {member.id}")
        await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    async def _store_avatar(self, user, asset):
        """Persist an avatar's bytes once, keyed by Discord's asset hash.

        Returns the hash (stored or not) so the event row always records WHICH
        picture it was, even when byte storage is off or the fetch fails.
        """
        if asset is None:
            return None
        ahash = getattr(asset, "key", None) or str(asset)
        gid = getattr(getattr(user, "guild", None), "id", 0) or 0
        # Bytes are tier 3 like any other cached file — these are members' FACES,
        # so a text-tier guild records which picture it was (hash) and nothing more.
        if not archives_media(gid):
            return ahash
        cfg = get_config(gid)
        if not cfg.get("msglog_avatar_bytes", 1):
            return ahash
        try:
            with self._conn() as c:
                if c.execute("SELECT 1 FROM avatar_blobs WHERE hash=?", (ahash,)).fetchone():
                    return ahash  # deduped — same picture, already held
        except Exception:
            return ahash
        max_kb = int(cfg.get("msglog_avatar_max_kb", 1024))
        try:
            data = await asset.read()
        except Exception:
            return ahash
        if not data or len(data) > max_kb * 1024:
            return ahash
        ctype = "image/gif" if bytes(data[:6]) in (b"GIF87a", b"GIF89a") else "image/png"
        try:
            with self._conn() as c:
                c.execute(
                    "INSERT OR IGNORE INTO avatar_blobs"
                    " (hash, uid, first_seen, content_type, size, data) VALUES (?,?,?,?,?,?)",
                    (ahash, str(getattr(user, "id", "")), time.time(), ctype, len(data), data))
        except Exception:
            pass
        return ahash

    async def _log_avatar_event(self, member, old_asset, new_asset, kind="avatar"):
        """Profile-picture change. The new picture's bytes are captured because
        a reused avatar is one of the strongest cheap alt signals there is, and
        the CDN copy vanishes the moment the account does."""
        guild = member.guild
        if not is_enabled(guild.id, "msglog"):
            return
        cfg = get_config(guild.id)
        if not cfg.get("msglog_names", 1):
            return
        if member.bot and not cfg.get("msglog_log_bots", 0):
            return
        old_hash = getattr(old_asset, "key", None) if old_asset else None
        new_hash = await self._store_avatar(member, new_asset)
        self._record_identity(guild.id, member, kind, old_hash, new_hash)
        log_ch = self._log_channel(guild, cfg, "users")
        if log_ch is None:
            return
        embed = discord.Embed(
            title="🖼️ Avatar changed" if kind == "avatar" else "🖼️ Server avatar changed",
            color=COLOR_ROLE, description=self._member_line(member))
        # Show the PICTURES, not the hashes (Paul, 8/23: "before and after as
        # hashes, that's useless"). Both are fetched right now — the old one is
        # still on the CDN for a moment after the change — and attached as
        # files, so the log keeps its own copy after the CDN forgets. Works on
        # every tier: nothing lands on our disk through this path. The hashes
        # stay in the identity ledger (Pro) and in the footer for cross-referencing.
        files = []

        async def grab(asset, name):
            if asset is None:
                return None
            try:
                ext = "gif" if getattr(asset, "is_animated", lambda: False)() else "png"
                data = await asset.with_size(256).read()
                fname = f"{name}.{ext}"
                files.append(discord.File(io.BytesIO(data), filename=fname))
                return f"attachment://{fname}"
            except Exception:
                return asset.url  # CDN hiccup — fall back to the live link
        before_url = await grab(old_asset, "before")
        after_url = await grab(new_asset, "after")
        embed.add_field(name="Before", value="*default avatar*" if before_url is None else "↗️ top right",
                        inline=True)
        embed.add_field(name="After", value="*default avatar*" if after_url is None else "⬇️ below",
                        inline=True)
        if before_url:
            embed.set_thumbnail(url=before_url)
        if after_url:
            embed.set_image(url=after_url)
        short = lambda h: (h[:12] + "…") if h and len(h) > 14 else (h or "default")  # noqa: E731
        embed.set_footer(text=f"User ID {member.id} · {short(old_hash)} → {short(new_hash)}")
        await log_ch.send(embed=embed, files=files or discord.utils.MISSING,
                          allowed_mentions=discord.AllowedMentions.none())

    async def _log_timeout_event(self, before, after):
        """Timeout applied / lifted. Previously invisible entirely — a mod
        timing someone out left no trace in our logs at all."""
        guild = after.guild
        if not is_enabled(guild.id, "msglog"):
            return
        cfg = get_config(guild.id)
        if not cfg.get("msglog_names", 1):
            return
        until = getattr(after, "timed_out_until", None)
        was = getattr(before, "timed_out_until", None)
        # Discord expires a timeout by leaving the (now past) stamp in place, so
        # "removed" means cleared or moved into the past.
        applied = until is not None and until.timestamp() > time.time()
        if not applied and was is not None and was.timestamp() <= time.time():
            return  # natural expiry, not a mod action — don't log noise
        rec = await self._who_changed_member(guild, after.id)
        self._record_identity(
            guild.id, after, "timeout" if applied else "untimeout",
            before=was.isoformat() if was else None,
            after=until.isoformat() if until else None,
            by_uid=(rec or {}).get("by_id"), by_name=(rec or {}).get("by_name"),
            reason=(rec or {}).get("reason"))
        log_ch = self._log_channel(guild, cfg, "mod")
        if log_ch is None:
            return
        embed = discord.Embed(
            title="⏳ Member timed out" if applied else "⏱️ Timeout removed",
            color=COLOR_BAN if applied else COLOR_JOIN,
            description=self._member_line(after))
        if applied:
            embed.add_field(name="Until",
                            value=f"<t:{int(until.timestamp())}:F> (<t:{int(until.timestamp())}:R>)",
                            inline=False)
        embed.add_field(name="By", value=self._mod_line(rec) if rec else "? (no audit entry found)",
                        inline=True)
        if rec and rec.get("reason"):
            embed.add_field(name="Reason", value=_trunc(rec["reason"]), inline=False)
        embed.set_footer(text=f"User ID {after.id}")
        await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @commands.Cog.listener()
    async def on_automod_action(self, execution: discord.AutoModAction):
        """AutoMod-blocked content is the ONE thing the archive can never hold.

        A blocked message is never created, so on_message never fires and no
        row is ever written — meaning the single worst thing anyone tried to
        say is precisely what we have no record of. Discord hands us the
        rejected text here and nowhere else, so this is the only chance to keep
        it. Also records a ledger row: repeat attempts are a pattern.
        """
        guild = self.bot.get_guild(execution.guild_id)
        if guild is None or not is_enabled(guild.id, "msglog"):
            return
        cfg = get_config(guild.id)
        if not cfg.get("msglog_automod", 1):
            return
        log_ch = self._log_channel(guild, cfg, "automod")
        if log_ch is None:
            return
        member = guild.get_member(execution.user_id)
        action = getattr(getattr(execution, "action", None), "type", None)
        blocked = getattr(execution, "content", None) or getattr(execution, "matched_content", None)
        self._record_identity(
            guild.id, member or execution.user_id, "automod",
            before=getattr(execution, "matched_keyword", None),
            after=_trunc(blocked or "", 400),
            reason=f"rule:{execution.rule_id} action:{getattr(action, 'name', action)}")
        embed = discord.Embed(
            title="🛡️ AutoMod blocked a message", color=COLOR_MOD_DELETE,
            description=(self._member_line(member) if member
                         else f"<@{execution.user_id}> (`{execution.user_id}`)"))
        if execution.channel_id:
            embed.add_field(name="Channel", value=f"<#{execution.channel_id}>", inline=True)
        embed.add_field(name="Action", value=str(getattr(action, "name", action)), inline=True)
        if getattr(execution, "matched_keyword", None):
            embed.add_field(name="Matched", value=_trunc(_plain(execution.matched_keyword), 200),
                            inline=True)
        if blocked:
            # the payload Discord refused to deliver — kept verbatim, escaped
            embed.add_field(name="Blocked content", value=_trunc(_plain(blocked)), inline=False)
        embed.set_footer(text=f"User ID {execution.user_id} · rule {execution.rule_id}")
        await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @commands.Cog.listener()
    async def on_user_update(self, before: discord.User, after: discord.User):
        """Account-level username / display-name changes.

        Fires once globally, so fan out to every guild that has us enabled and
        actually contains the user — this is the event that would have caught
        an adversary renaming an account mid-incident.
        """
        changes = []
        if before.name != after.name:
            changes.append(("username", before.name, after.name))
        if getattr(before, "global_name", None) != getattr(after, "global_name", None):
            changes.append(("global_name", before.global_name, after.global_name))
        avatar_changed = before.avatar != after.avatar
        if not changes and not avatar_changed:
            return
        for guild in self.bot.guilds:
            member = guild.get_member(after.id)
            if member is None:
                continue
            for kind, old, new in changes:
                try:
                    await self._log_name_event(member, kind, old, new)
                except Exception:
                    pass
            if avatar_changed:
                try:
                    await self._log_avatar_event(member, before.avatar, after.avatar)
                except Exception:
                    pass

    async def _quarantine_role_spammer(self, member, log_ch, count):
        """Massive self-inflicted reaction-role churn = griefing. Reuse anti-nuke's
        quarantine (strip roles + lock out, saved for restore via /altguard-release)
        so the response matches a real nuke. Falls back to an alert if anti-nuke is
        off or no quarantine role is configured."""
        guild = member.guild
        cfg = get_config(guild.id)
        reason = f"role-toggle spam ({count}× in ~{int(ROLELOG_WINDOW)}s)"
        anti = self.bot.get_cog("AntiNuke")
        done = False
        if anti is not None and cfg.get("quarantine_role_id"):
            try:
                done = await anti._quarantine_offender(guild, member, reason, cfg)
            except Exception:
                done = False
        embed = discord.Embed(
            title="🚨 Role-spam quarantine" if done else "🚨 Role-spam detected",
            color=COLOR_MOD_DELETE,
            description=f"{self._member_line(member)} — {reason}."
                        + ("\nQuarantined (roles stripped + locked out). Reversible with `/altguard-release`."
                           if done else
                           "\n⚠️ Could not auto-quarantine (anti-nuke off or no quarantine role set) — review manually."))
        embed.set_footer(text=f"User ID {member.id}")
        await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    async def _log_guild_role_event(self, entry):
        """Role created / deleted / edited — straight from the audit event, which
        carries actor + diff in one payload (no aggregation for these actions)."""
        guild = entry.guild
        cfg = get_config(guild.id)
        if not cfg.get("msglog_roles", 1):
            return
        log_ch = self._log_channel(guild, cfg, "roles")
        if log_ch is None:
            return
        A = discord.AuditLogAction
        role = entry.target  # discord.Role, or bare Object once deleted
        role_id = getattr(role, "id", "?")
        name = getattr(role, "name", None) or getattr(entry.changes.before, "name", None) \
            or getattr(entry.changes.after, "name", None) or f"ID {role_id}"

        if entry.action is A.role_create:
            title, color = "🆕 Role created", COLOR_JOIN
            lines = [role.mention if isinstance(role, discord.Role) else f"**{name}**"]
        elif entry.action is A.role_delete:
            title, color = "🗑️ Role deleted", COLOR_MOD_DELETE
            lines = [f"**{name}**"]
        else:  # role_update — show what changed, skip position-only reorders (noise)
            before, after = entry.changes.before, entry.changes.after
            diffs = []
            for attr in ("name", "hoist", "mentionable"):
                if hasattr(before, attr) or hasattr(after, attr):
                    diffs.append(f"{attr}: `{getattr(before, attr, '?')}` → `{getattr(after, attr, '?')}`")
            if hasattr(before, "colour") or hasattr(after, "colour"):
                diffs.append(f"color: `{getattr(before, 'colour', '?')}` → `{getattr(after, 'colour', '?')}`")
            if hasattr(before, "permissions") or hasattr(after, "permissions"):
                pb = dict(getattr(before, "permissions", None) or discord.Permissions.none())
                pa = dict(getattr(after, "permissions", None) or discord.Permissions.none())
                gained = sorted(p for p, v in pa.items() if v and not pb.get(p))
                lost = sorted(p for p, v in pb.items() if v and not pa.get(p))
                if gained:
                    diffs.append("perms **+** " + ", ".join(f"`{p}`" for p in gained))
                if lost:
                    diffs.append("perms **−** " + ", ".join(f"`{p}`" for p in lost))
            if not diffs:
                return
            title, color = "✏️ Role edited", COLOR_EDIT
            lines = [role.mention if isinstance(role, discord.Role) else f"**{name}**"] + diffs

        embed = discord.Embed(title=title, color=color, description=_trunc("\n".join(lines), 4000))
        embed.add_field(name="By", value=self._mod_line(
            {"by_id": entry.user_id, "by_name": str(entry.user) if entry.user else None}), inline=True)
        if entry.reason:
            embed.add_field(name="Reason", value=_trunc(entry.reason), inline=False)
        embed.set_footer(text=f"Role ID {role_id}")
        await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    async def _log_expression_event(self, entry):
        """Custom emoji / sticker created, deleted or edited — audit event
        carries actor + name diff. On DELETION the asset is grabbed and
        re-posted: the CDN keeps serving a deleted expression's image by id,
        so fetching at event time and attaching it to the log preserves the
        image in Discord permanently (the CDN copy is not guaranteed to)."""
        guild = entry.guild
        cfg = get_config(guild.id)
        if not cfg.get("msglog_expressions", 1):
            return
        log_ch = self._log_channel(guild, cfg, "expressions")
        if log_ch is None:
            return
        A = discord.AuditLogAction
        is_sticker = entry.action in (A.sticker_create, A.sticker_delete, A.sticker_update)
        kind = "Sticker" if is_sticker else "Emoji"
        target_id = getattr(entry.target, "id", None)
        before, after = entry.changes.before, entry.changes.after
        name = (getattr(after, "name", None) or getattr(before, "name", None)
                or getattr(entry.target, "name", None) or f"ID {target_id}")

        if entry.action in (A.emoji_create, A.sticker_create):
            title, color, lines = f"🆕 {kind} created", COLOR_JOIN, [f"**{name}**"]
        elif entry.action in (A.emoji_delete, A.sticker_delete):
            title, color, lines = f"🗑️ {kind} deleted", COLOR_MOD_DELETE, [f"**{name}**"]
        else:
            diffs = []
            for attr in ("name", "description"):
                if hasattr(before, attr) or hasattr(after, attr):
                    diffs.append(f"{attr}: `{getattr(before, attr, '?')}` → `{getattr(after, attr, '?')}`")
            if not diffs:
                return
            title, color, lines = f"✏️ {kind} edited", COLOR_EDIT, [f"**{name}**"] + diffs

        embed = discord.Embed(title=title, color=color, description=_trunc("\n".join(lines), 4000))
        embed.add_field(name="By", value=self._mod_line(
            {"by_id": entry.user_id, "by_name": str(entry.user) if entry.user else None}), inline=True)
        if entry.reason:
            embed.add_field(name="Reason", value=_trunc(entry.reason), inline=False)
        embed.set_footer(text=f"{kind} ID {target_id}")

        file = discord.utils.MISSING
        data, ext = await self._fetch_expression_asset(target_id, is_sticker)
        if data:
            fname = f"{safe_filename(name)}.{ext}"
            file = discord.File(io.BytesIO(data), filename=fname)
            embed.set_thumbnail(url=f"attachment://{fname}")
        elif entry.action in (A.emoji_delete, A.sticker_delete):
            embed.add_field(name="Image", value="not recoverable (CDN no longer serves it)",
                            inline=False)
        await log_ch.send(embed=embed, file=file,
                          allowed_mentions=discord.AllowedMentions.none())

    async def _fetch_expression_asset(self, target_id, is_sticker):
        """Expression image bytes straight off the CDN by id. Animated emojis
        must be asked for as .gif (a .png request serves the first frame), so
        gif is tried first; static ones 415 on .gif and fall through to png."""
        if not target_id:
            return None, None
        if is_sticker:
            urls = [(f"https://media.discordapp.net/stickers/{target_id}.png", "png"),
                    (f"https://cdn.discordapp.com/stickers/{target_id}.gif", "gif")]
        else:
            urls = [(f"https://cdn.discordapp.com/emojis/{target_id}.gif", "gif"),
                    (f"https://cdn.discordapp.com/emojis/{target_id}.png", "png")]
        for url, ext in urls:
            try:
                return await self.bot.http.get_from_cdn(url), ext
            except discord.HTTPException:
                continue
            except Exception:
                break
        return None, None

    async def _log_channel_event(self, entry):
        """Channel created / deleted / edited + permission-overwrite changes —
        straight from the audit event (actor + diff in one payload, same shape
        as role events). Own-bot changes ARE logged, unlike member-role ones:
        overwrite edits made through the bot (REST tooling, lockdowns) are
        exactly the kind of change the log must show, and they're rare enough
        not to be noise. Position-only channel reorders are skipped."""
        guild = entry.guild
        cfg = get_config(guild.id)
        if not cfg.get("msglog_channels", 1):
            return
        log_ch = self._log_channel(guild, cfg, "channels")
        if log_ch is None:
            return
        A = discord.AuditLogAction
        target_id = getattr(entry.target, "id", None)
        chan = guild.get_channel(target_id) if target_id else None
        name = (getattr(chan, "name", None)
                or getattr(entry.changes.before, "name", None)
                or getattr(entry.changes.after, "name", None) or f"ID {target_id}")
        where = chan.mention if chan else f"**#{name}**"

        if entry.action is A.channel_create:
            title, color, lines = "🆕 Channel created", COLOR_JOIN, [where]
        elif entry.action is A.channel_delete:
            title, color, lines = "🗑️ Channel deleted", COLOR_MOD_DELETE, [f"**#{name}**"]
        elif entry.action is A.channel_update:
            before, after = entry.changes.before, entry.changes.after
            diffs = []
            for attr in ("name", "topic", "nsfw", "slowmode_delay", "bitrate", "user_limit"):
                if hasattr(before, attr) or hasattr(after, attr):
                    diffs.append(f"{attr}: `{getattr(before, attr, '?')}` → `{getattr(after, attr, '?')}`")
            if not diffs:
                return
            title, color, lines = "✏️ Channel edited", COLOR_EDIT, [where] + diffs
        else:  # overwrite_create / overwrite_update / overwrite_delete
            who = entry.extra  # Role | Member | bare Object with .id/.type
            if isinstance(who, discord.Role):
                subject = f"role {who.mention}"
            elif isinstance(who, (discord.Member, discord.User)):
                subject = f"member {who.mention}"
            else:
                kind = "member" if str(getattr(who, "type", "")) == "member" else "role"
                subject = f"{kind} <@{'&' if kind == 'role' else ''}{getattr(who, 'id', '?')}>"

            def pd(obj, attr):
                return dict(getattr(obj, attr, None) or discord.Permissions.none())
            b, a = entry.changes.before, entry.changes.after
            if entry.action is A.overwrite_delete:
                title, color = "🔐 Channel permissions removed", COLOR_MOD_DELETE
                diffs = perm_diff_lines(pd(b, "allow"), pd(b, "deny"), {}, {})
            else:
                title = ("🔐 Channel permissions added" if entry.action is A.overwrite_create
                         else "🔐 Channel permissions edited")
                color = COLOR_CHANNEL
                diffs = perm_diff_lines(pd(b, "allow"), pd(b, "deny"), pd(a, "allow"), pd(a, "deny"))
            if not diffs:
                return
            lines = [f"{where} — {subject}"] + diffs

        embed = discord.Embed(title=title, color=color, description=_trunc("\n".join(lines), 4000))
        embed.add_field(name="By", value=self._mod_line(
            {"by_id": entry.user_id, "by_name": str(entry.user) if entry.user else None}), inline=True)
        if entry.reason:
            embed.add_field(name="Reason", value=_trunc(entry.reason), inline=False)
        embed.set_footer(text=f"Channel ID {target_id}")
        await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        guild = member.guild
        if member.id == self.bot.user.id:
            return
        log_ch, _ = self._members_log_channel(guild)
        if log_ch is None:
            return
        # wait for the audit event to classify this removal (kick/ban/leave)
        rec = None
        for _ in range(3):
            await asyncio.sleep(1.5)
            r = self._removals.get(member.id)
            if r and time.time() - r["ts"] < 30:
                rec = self._removals.pop(member.id)
                break
        if rec is None:
            # audit event missed (no perm / gateway drop) — poll once as fallback
            for action in (discord.AuditLogAction.kick, discord.AuditLogAction.ban):
                for en in await self._fetch_entries(guild, action, limit=6):
                    if en["target_id"] == member.id and time.time() - en["created_ts"] < 15 \
                            and en["id"] not in self._removal_ids_seen:
                        self._removal_ids_seen.add(en["id"])
                        rec = {"action": action, "by_id": en["user_id"],
                               "by_name": en["user_name"], "reason": en["reason"], "ts": time.time()}
                        break
                if rec:
                    break
        self._record_identity(
            guild.id, member,
            "kick" if rec and rec["action"] is discord.AuditLogAction.kick
            else ("ban" if rec and rec["action"] is discord.AuditLogAction.ban else "leave"),
            before=member.nick, after=member.global_name or member.name,
            by_uid=(rec or {}).get("by_id"), by_name=(rec or {}).get("by_name"),
            reason=(rec or {}).get("reason"))
        if is_quiet(member.id):
            return  # silenced removal — the ledger row above is written either way
        if rec and rec["action"] is discord.AuditLogAction.ban:
            return  # the ban embed (with reason) is posted from the audit event
        joined = f"<t:{int(member.joined_at.timestamp())}:R>" if member.joined_at else "?"
        roles = [r.mention for r in reversed(member.roles) if r != guild.default_role]
        if rec:  # kick
            embed = discord.Embed(
                title="👢 Member kicked", color=COLOR_KICK,
                description=self._member_line(member))
            embed.add_field(name="By", value=self._mod_line(rec), inline=True)
            embed.add_field(name="Joined", value=joined, inline=True)
            embed.add_field(name="Reason", value=_trunc(rec["reason"] or "No reason provided"), inline=False)
        else:    # plain leave
            embed = discord.Embed(
                title="📤 Member left", color=COLOR_LEAVE,
                description=self._member_line(member))
            embed.add_field(name="Joined", value=joined, inline=True)
            embed.add_field(name="Members", value=f"{guild.member_count:,}", inline=True)
        if roles:
            embed.add_field(name="Roles", value=_trunc(" ".join(roles), 1024), inline=False)
        embed.set_footer(text=f"User ID {member.id}")
        await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        # Voice channel join / leave / move, plus SERVER mute/deafen (a mod did
        # it to them — attributed via the audit log). Self-mute/deafen, stream
        # and camera toggles are deliberately NOT logged — that's device state,
        # not moderation. Own bot excluded; other bots gated by msglog_log_bots.
        guild = member.guild
        if guild is None or member.id == self.bot.user.id:
            return
        b, a = before.channel, after.channel
        server_state = (before.mute, before.deaf) != (after.mute, after.deaf)
        if b == a and not server_state:
            return
        if not is_enabled(guild.id, "msglog"):
            return
        cfg = get_config(guild.id)
        if not cfg.get("msglog_voice", 1):
            return
        if member.bot and not cfg.get("msglog_log_bots", 0):
            return
        log_ch = self._log_channel(guild, cfg, "voice")
        if log_ch is None:
            return

        if b == a:             # same channel: server mute / deafen changed
            if a is not None and self._skip_logging(cfg, a.id, log_ch):
                return
            await self._log_server_voice_state(member, before, after, log_ch)
            return

        if b is None:          # joined voice
            if self._skip_logging(cfg, a.id, log_ch):
                return
            title, color, channel = "🔊 Joined voice", COLOR_VOICE, a.mention
        elif a is None:        # left voice
            if self._skip_logging(cfg, b.id, log_ch):
                return
            title, color, channel = "🔇 Left voice", COLOR_LEAVE, b.mention
        else:                  # moved between channels
            if self._skip_logging(cfg, a.id, log_ch) and self._skip_logging(cfg, b.id, log_ch):
                return
            title, color, channel = "🔀 Switched voice", COLOR_VOICE, f"{b.mention} → {a.mention}"

        embed = discord.Embed(title=title, color=color, description=self._member_line(member))
        embed.add_field(name="Channel", value=channel, inline=False)
        embed.set_footer(text=f"User ID {member.id}")
        await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    async def _log_server_voice_state(self, member, before, after, log_ch):
        """Server mute/deafen applied or lifted by a moderator (Quark Pro's
        'see who muted/deafened users' — free here). One embed per event even
        when both flip at once."""
        changes = []
        if before.mute != after.mute:
            changes.append(("🔇 Server muted", "🔈 Server unmuted", after.mute))
        if before.deaf != after.deaf:
            changes.append(("🙉 Server deafened", "👂 Server undeafened", after.deaf))
        rec = await self._who_changed_member(member.guild, member.id)
        by = self._mod_line(rec) if rec else "? (no audit entry found)"
        for on_title, off_title, now_on in changes:
            embed = discord.Embed(title=on_title if now_on else off_title,
                                  color=COLOR_VOICE, description=self._member_line(member))
            if after.channel is not None:
                embed.add_field(name="Channel", value=after.channel.mention, inline=True)
            embed.add_field(name="By", value=by, inline=True)
            if rec and rec.get("reason"):
                embed.add_field(name="Reason", value=_trunc(rec["reason"]), inline=False)
            embed.set_footer(text=f"User ID {member.id}")
            await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    # ------------------------------------------------------------- commands
    msglog = app_commands.Group(
        name="msglog", description="Message archive + deletion/edit logging (Manage Server)",
        guild_only=True, default_permissions=discord.Permissions(manage_guild=True))

    @msglog.command(name="enable", description="Turn on the message archive + mod-log.")
    @app_commands.describe(channel="Where delete/edit logs go (defaults to the security mod-log)")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def enable_cmd(self, interaction: discord.Interaction,
                         channel: discord.TextChannel = None):
        fields = {"msglog_enabled": 1}
        if channel is not None:
            fields["msglog_channel_id"] = str(channel.id)
        cfg = set_config(interaction.guild.id, **fields)
        target = cfg.get("msglog_channel_id") or cfg.get("modlog_channel_id")
        msg = ("✅ **Message log enabled** — deletes (with who-deleted-it attribution), "
               "edits and bulk deletes get logged"
               + (f" to <#{target}>." if target else
                  ".\n⚠️ No log channel set — pass `channel:` or nothing will post."))
        if archives_messages(interaction.guild.id):
            msg += ("\n🗄️ **Logging Pro archive is on** — deleted text and images are recoverable "
                    f"for {PRO_TEXT_DAYS} days and searchable with `/msglog deleted`.")
        else:
            msg += (f"\n🕐 **Free window:** deleted/edited messages and their images are recoverable "
                    f"for {RECENT_HOURS} hours, then swept. `/msglog pro` for the long archive.")
        await interaction.response.send_message(msg, ephemeral=True)

    # ------------------------------------------------------------- Pro
    def _pro_state_lines(self, guild):
        gid = str(guild.id)
        tier = retention_tier(gid)
        row = _PRO.get(gid)
        lines = []
        if tier == "operator":
            lines.append("🟢 **Operator archive** — unlimited, nothing is swept.")
        elif tier == "pro":
            exp = row.get("expires_ts") if row else None
            until = f"until <t:{int(exp)}:D>" if exp else "no expiry"
            lines.append(f"🟢 **Logging Pro active** ({until}) — text kept {PRO_TEXT_DAYS} days, "
                         f"files {PRO_MEDIA_DAYS} days ({PRO_MEDIA_MB} MB), identity history, "
                         "`/msglog deleted` + `/msglog history`.")
        else:
            lines.append(f"🕐 **Free window** — deleted/edited messages and images recoverable for "
                         f"{RECENT_HOURS}h ({RECENT_MEDIA_MB} MB of files), then swept.")
            if row and not pro_active(gid):
                lines.append(f"⏳ Pro **expired** <t:{int(row['expires_ts'])}:R>. Archive is kept "
                             f"{PRO_GRACE_DAYS} more days, then falls back to the free window. "
                             f"Renew: {PRO_URL}")
            elif row and pro_active(gid) and not consent_ok(gid):
                lines.append("💳 Pro is **paid for** but the archive is still off — anyone with "
                             "Manage Server must accept the retention terms: "
                             "`/msglog accept-terms confirm:True` (read `/msglog terms` first).")
            else:
                lines.append(f"⬆️ **Logging Pro** — {PRO_TEXT_DAYS} days of deleted-message history, "
                             f"{PRO_MEDIA_DAYS} days of deleted images, searchable. Quark Pro keeps 4 "
                             f"weeks. Get it at {PRO_URL}, then accept the terms here with "
                             "`/msglog accept-terms confirm:True`.")
        return lines

    @msglog.command(name="pro", description="Logging Pro: what this server has, what it would get.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def pro_cmd(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            "⭐ **Logging Pro**\n" + "\n".join(self._pro_state_lines(interaction.guild)),
            ephemeral=True)

    @msglog.command(name="pro-grant",
                    description="Operator: grant/extend/revoke Logging Pro for a server (home guild only).")
    @app_commands.describe(guild_id="Server to grant", days="Days to add from now (0 = no expiry, -1 = revoke)",
                           note="Why (order id, comp, trial…)")
    @app_commands.checks.has_permissions(administrator=True)
    async def pro_grant_cmd(self, interaction: discord.Interaction, guild_id: str,
                            days: int, note: str = ""):
        # Fulfilment is operator-only, from the operator's own guild — this
        # hands out storage on our disk. Same shape as /ai-credit-grant.
        if str(interaction.guild_id) not in OPERATOR_GUILDS:
            await interaction.response.send_message("Operator command — home guild only.", ephemeral=True)
            return
        gid = guild_id.strip()
        if not gid.isdigit():
            await interaction.response.send_message("guild_id must be numeric.", ephemeral=True)
            return
        target = self.bot.get_guild(int(gid))
        gname = target.name if target else None
        now = time.time()
        with self._conn() as c:
            if days < 0:
                c.execute("DELETE FROM archive_pro WHERE guild_id=?", (gid,))
            else:
                cur = c.execute("SELECT expires_ts FROM archive_pro WHERE guild_id=?", (gid,)).fetchone()
                if days == 0:
                    exp = None
                else:
                    # Extend from the current expiry when still active — a
                    # renewal paid early must not eat the days already owned.
                    base = now
                    if cur and cur["expires_ts"] and float(cur["expires_ts"]) > now:
                        base = float(cur["expires_ts"])
                    exp = base + days * 86400
                c.execute(
                    "INSERT INTO archive_pro (guild_id, guild_name, expires_ts, granted_ts, granted_by, note, order_ref)"
                    " VALUES (?,?,?,?,?,?,NULL)"
                    " ON CONFLICT(guild_id) DO UPDATE SET guild_name=COALESCE(excluded.guild_name, guild_name),"
                    " expires_ts=excluded.expires_ts, granted_ts=excluded.granted_ts,"
                    " granted_by=excluded.granted_by, note=excluded.note",
                    (gid, gname, exp, now, str(interaction.user), note or None))
        self._load_pro()
        if days < 0:
            await interaction.response.send_message(
                f"🗑️ Pro revoked for `{gid}`{f' ({gname})' if gname else ''}. Its archive is kept "
                f"{PRO_GRACE_DAYS} days, then swept to the free window.", ephemeral=True)
            return
        row = _PRO.get(gid) or {}
        exp = row.get("expires_ts")
        until = f"until <t:{int(exp)}:D>" if exp else "no expiry"
        state = ("archive ON" if consent_ok(gid)
                 else "terms NOT accepted yet — archive stays on the free window until they run "
                      "`/msglog accept-terms confirm:True`")
        await interaction.response.send_message(
            f"⭐ Pro granted to `{gid}`{f' ({gname})' if gname else ''} — {until}. {state}.",
            ephemeral=True)
        # Tell the server. Pro without accepted terms does nothing, and the
        # admin who paid is not necessarily the one in the log channel.
        if target is not None:
            cfg = get_config(target.id)
            log_ch = self._log_channel(target, cfg)
            if log_ch is not None:
                try:
                    embed = discord.Embed(title="⭐ Logging Pro is active on this server",
                                          color=COLOR_EDIT,
                                          description="\n".join(self._pro_state_lines(target)))
                    await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
                except discord.HTTPException:
                    pass

    @msglog.command(name="terms",
                    description="What the archive stores, and how to turn it on or off.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def terms_cmd(self, interaction: discord.Interaction):
        gid = str(interaction.guild.id)
        row = _CONSENT.get(gid)
        if gid in ARCHIVE_GUILDS:
            state = "🟢 **Full archive** — text and files, unlimited (operator-run server)."
        elif row and consent_ok(gid):
            state = (f"🟢 **Accepted** by {_plain(row.get('username') or row.get('uid'))} "
                     f"<t:{int(row.get('accepted_ts') or 0)}:D> (v{row.get('version')}). "
                     f"Revoke any time with `/msglog revoke-terms`.")
            if not pro_active(gid):
                state += f"\n⚪ No active Pro entitlement, so only the free {RECENT_HOURS}h window applies."
        elif row:
            state = (f"⚠️ Accepted **v{row.get('version')}** — the terms have changed (now "
                     f"v{TERMS_VERSION}). Re-accept with `/msglog accept-terms confirm:True`.")
        else:
            state = (f"⚪ **Not accepted** — only the free {RECENT_HOURS}h window applies. Anyone with "
                     "**Manage Server** can accept with `/msglog accept-terms confirm:True` "
                     "(takes effect with an active Pro entitlement — `/msglog pro`).")
        await interaction.response.send_message(f"{TERMS_TEXT}\n\n{state}", ephemeral=True)

    @msglog.command(name="accept-terms",
                    description="Manage Server: agree to the retention terms and turn the archive on.")
    @app_commands.describe(confirm="Yes, I've read /msglog terms and I accept on behalf of this server")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def accept_terms_cmd(self, interaction: discord.Interaction, confirm: bool = False):
        # Manage Server is enough here (Paul, 2026-08-21: terms gate only AltGuard,
        # whose consent covers member device/network data and stays owner-only).
        # The acceptance is still recorded by name, version and time.
        if not confirm:
            await interaction.response.send_message(
                f"{TERMS_TEXT}\n\nRe-run with `confirm:True` to accept.", ephemeral=True)
            return
        now = time.time()
        with self._conn() as c:
            c.execute(
                "INSERT INTO archive_consent"
                " (guild_id, guild_name, uid, username, accepted_ts, version, revoked_ts)"
                " VALUES (?,?,?,?,?,?,NULL)"
                " ON CONFLICT(guild_id) DO UPDATE SET guild_name=excluded.guild_name,"
                " uid=excluded.uid, username=excluded.username,"
                " accepted_ts=excluded.accepted_ts, version=excluded.version, revoked_ts=NULL",
                (str(interaction.guild.id), interaction.guild.name, str(interaction.user.id),
                 str(interaction.user), now, TERMS_VERSION))
        self._load_consent()
        gid = str(interaction.guild.id)
        if archives_messages(gid):
            msg = (f"✅ **Archive on** (terms v{TERMS_VERSION} accepted). Text is kept {PRO_TEXT_DAYS} "
                   f"days and files {PRO_MEDIA_DAYS} days from now on — nothing before the free "
                   f"window exists. `/msglog deleted` and `/msglog history` now work here.\n"
                   "↩️ `/msglog revoke-terms` stops it and deletes everything.")
        else:
            msg = (f"✅ **Terms v{TERMS_VERSION} accepted** and recorded. This server has no active "
                   f"Logging Pro entitlement yet, so the free {RECENT_HOURS}h window still applies; "
                   f"the archive switches on by itself the moment Pro is granted. `/msglog pro` "
                   f"for how to get it.")
        await interaction.response.send_message(msg, ephemeral=True)

    @msglog.command(name="revoke-terms",
                    description="Manage Server: stop storing this server's data and delete what's stored.")
    @app_commands.describe(confirm="Yes — stop retention and permanently delete this server's archive")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def revoke_terms_cmd(self, interaction: discord.Interaction, confirm: bool = False):
        if not confirm:
            await interaction.response.send_message(
                "⚠️ This **permanently deletes** this server's stored messages, edit history and "
                "member/identity records, and stops any further storage. Logging keeps working; "
                "only the memory goes. Re-run with `confirm:True`.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        gid = str(interaction.guild.id)
        self._flush()
        counts = await asyncio.to_thread(self._purge_guild, gid)
        with self._conn() as c:
            c.execute("UPDATE archive_consent SET revoked_ts=? WHERE guild_id=?", (time.time(), gid))
        self._load_consent()
        note = ("\n⚠️ This server is on the operator's allowlist (`MSGLOG_ARCHIVE_GUILDS`), so "
                "retention stays ON until that entry is removed — the data above was still deleted."
                if gid in ARCHIVE_GUILDS else "")
        await interaction.followup.send(
            f"🗑️ **Archive revoked and purged.** Deleted {counts['messages']:,} messages, "
            f"{counts['edits']:,} edit records, {counts['identity']:,} member events and "
            f"{counts['files']:,} cached files. Nothing further is stored." + note,
            ephemeral=True)

    def _purge_guild(self, gid):
        """Delete every stored row (and cached file) belonging to one guild.

        Blocking on purpose — the caller runs it off the event loop. Media files
        are named by message id, so the ids have to be read before the rows go.
        """
        counts = {"messages": 0, "edits": 0, "identity": 0, "files": 0}
        with self._conn() as c:
            ids = [r[0] for r in c.execute("SELECT message_id FROM messages WHERE guild_id=?", (gid,))]
            counts["edits"] = c.execute("DELETE FROM edits WHERE guild_id=?", (gid,)).rowcount
            counts["messages"] = c.execute("DELETE FROM messages WHERE guild_id=?", (gid,)).rowcount
            counts["identity"] = c.execute("DELETE FROM identity_events WHERE guild_id=?",
                                           (gid,)).rowcount
            # Avatar blobs are shared across guilds by design (one picture, one
            # row) — drop only those no surviving identity row still points at.
            c.execute("DELETE FROM avatar_blobs WHERE uid NOT IN"
                      " (SELECT DISTINCT uid FROM identity_events)")
        for mid in ids:
            for p in glob.glob(os.path.join(MEDIA_DIR, f"{mid}_*")):  # legacy flat layout
                try:
                    os.remove(p)
                    counts["files"] += 1
                except OSError:
                    pass
        gdir = self._media_dir(gid)
        if os.path.isdir(gdir):
            for p in glob.glob(os.path.join(gdir, "*")):
                if self._remove_media_file(p):
                    counts["files"] += 1
            try:
                os.rmdir(gdir)
            except OSError:
                pass
        self._guild_bytes.pop(gid, None)
        return counts

    @msglog.command(name="disable", description="Turn off archiving + logging.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def disable_cmd(self, interaction: discord.Interaction):
        set_config(interaction.guild.id, msglog_enabled=0)
        await interaction.response.send_message(
            "⏸️ Message log disabled — no archiving or logging. Existing archive kept.",
            ephemeral=True)

    @msglog.command(name="ignore", description="Toggle a channel out of delete/edit LOGGING (still archived).")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def ignore_cmd(self, interaction: discord.Interaction, channel: discord.TextChannel):
        cfg = get_config(interaction.guild.id)
        ignored = [str(x) for x in cfg.get("msglog_ignore_channels") or []]
        if str(channel.id) in ignored:
            ignored.remove(str(channel.id))
            verb = "▶️ logging again"
        else:
            ignored.append(str(channel.id))
            verb = "🔇 no longer logged (still archived)"
        set_config(interaction.guild.id, msglog_ignore_channels=ignored)
        await interaction.response.send_message(f"{channel.mention}: {verb}.", ephemeral=True)

    @msglog.command(name="media-channel",
                    description="Route deleted-media re-posts to a separate channel (e.g. 18+ staff only).")
    @app_commands.describe(channel="Destination for media re-posts; omit to attach media to the log embeds again")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def media_channel_cmd(self, interaction: discord.Interaction,
                                channel: discord.TextChannel = None):
        set_config(interaction.guild.id,
                   msglog_media_channel_id=str(channel.id) if channel else None)
        await interaction.response.send_message(
            f"🖼️ Deleted-media re-posts now go to {channel.mention}; the text log embeds "
            f"reference them there." if channel else
            "🖼️ Media routing cleared — deleted media attaches to the log embeds again.",
            ephemeral=True)

    @msglog.command(name="voice", description="Toggle voice channel join/leave/move logging on or off.")
    @app_commands.describe(enabled="On = log voice join/leave/switch to the mod-log")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def voice_cmd(self, interaction: discord.Interaction, enabled: bool):
        set_config(interaction.guild.id, msglog_voice=1 if enabled else 0)
        await interaction.response.send_message(
            f"🔊 Voice logging **{'on' if enabled else 'off'}** — "
            + ("join/leave/switch events post to the mod-log."
               if enabled else "voice movement is no longer logged."),
            ephemeral=True)

    @msglog.command(name="names", description="Toggle nickname/username/timeout logging on or off.")
    @app_commands.describe(enabled="On = log nickname, username and timeout changes")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def names_cmd(self, interaction: discord.Interaction, enabled: bool):
        set_config(interaction.guild.id, msglog_names=1 if enabled else 0)
        await interaction.response.send_message(
            f"🪪 Name/timeout logging **{'on' if enabled else 'off'}** — "
            + ("nickname, username and timeout changes post to the mod-log."
               if enabled else "these changes are no longer posted."
                               " (The identity ledger keeps recording regardless.)"),
            ephemeral=True)

    @msglog.command(name="history", description="Every recorded name, timeout and lifecycle event for a user ID.")
    @app_commands.describe(user_id="Numeric user ID — works for users who already left or deleted their account")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def history_cmd(self, interaction: discord.Interaction, user_id: str):
        uid = "".join(ch for ch in user_id if ch.isdigit())
        if not uid:
            await interaction.response.send_message("Give me a numeric user ID.", ephemeral=True)
            return
        with self._conn() as c:
            rows = list(c.execute(
                "SELECT ts, kind, username, before, after, by_name, reason FROM identity_events"
                " WHERE uid=? AND guild_id=? ORDER BY ts DESC LIMIT 40",
                (uid, str(interaction.guild.id))))
        if not rows:
            await interaction.response.send_message(
                f"No identity events recorded for `{uid}`.", ephemeral=True)
            return
        lines = []
        for r in rows:
            when = f"<t:{int(r['ts'])}:f>"
            bit = f"{when} · **{r['kind']}**"
            if r["before"] or r["after"]:
                bit += f" · {_plain(r['before']) or '∅'} → {_plain(r['after']) or '∅'}"
            if r["by_name"]:
                bit += f" · by {_plain(r['by_name'])}"
            if r["reason"]:
                bit += f" · _{_plain(r['reason'])[:60]}_"
            lines.append(bit)
        embed = discord.Embed(
            title=f"🪪 Identity history — {rows[0]['username'] or uid}",
            color=COLOR_ROLE, description=_trunc("\n".join(lines), 4000))
        embed.set_footer(text=f"User ID {uid} · {len(rows)} event(s)")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @msglog.command(name="automod", description="Toggle AutoMod block + rule-change logging.")
    @app_commands.describe(enabled="On = log what AutoMod blocks and any rule changes")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def automod_cmd(self, interaction: discord.Interaction, enabled: bool):
        set_config(interaction.guild.id, msglog_automod=1 if enabled else 0)
        await interaction.response.send_message(
            f"🛡️ AutoMod logging **{'on' if enabled else 'off'}** — "
            + ("blocked content and rule changes post to the mod-log. Blocked messages "
               "exist nowhere else: they are never created, so the archive never sees them."
               if enabled else "AutoMod activity is no longer logged."),
            ephemeral=True)

    @msglog.command(name="forget", description="Erase all identity records + stored avatars for a user ID.")
    @app_commands.describe(user_id="Numeric user ID to erase from the identity ledger")
    @app_commands.checks.has_permissions(administrator=True)
    async def forget_cmd(self, interaction: discord.Interaction, user_id: str):
        """Right-to-erasure path. The ledger holds names and profile PICTURES,
        and this server skews young — an under-age purge (or any 'delete my
        data' request) has to be one command, not a hand-written DELETE."""
        uid = "".join(ch for ch in user_id if ch.isdigit())
        if not uid:
            await interaction.response.send_message("Give me a numeric user ID.", ephemeral=True)
            return
        # Scope: a server admin erases THEIR server's records. Only the operator's
        # own guilds purge a user everywhere (the under-age / right-to-be-forgotten
        # case) — before 8/23 this deleted across every guild, which let any
        # remote admin wipe the home ledger for a user.
        gid = str(interaction.guild.id)
        everywhere = gid in OPERATOR_GUILDS
        with self._conn() as c:
            if everywhere:
                ev = c.execute("DELETE FROM identity_events WHERE uid=?", (uid,)).rowcount
            else:
                ev = c.execute("DELETE FROM identity_events WHERE uid=? AND guild_id=?",
                               (uid, gid)).rowcount
            # only drop blobs this uid alone owns — a shared/default asset hash
            # could belong to someone else's history too
            av = c.execute(
                "DELETE FROM avatar_blobs WHERE uid=? AND hash NOT IN"
                " (SELECT after FROM identity_events WHERE after IS NOT NULL)", (uid,)).rowcount
        # Conduct record + evidence files live in their own store; a purge that
        # leaves them behind is the "survives a purge everyone thought was
        # complete" hole. Same scope rule.
        try:
            from utils import conduct as conduct_store
            cr = conduct_store.forget_user(uid, None if everywhere else gid)
            conduct_line = (f" Conduct: **{cr['entries']}** entr{'y' if cr['entries'] == 1 else 'ies'}, "
                            f"**{cr['files']}** evidence file(s).")
        except Exception as e:  # the cog may not be loaded on this deployment
            conduct_line = f" Conduct record not purged ({type(e).__name__})."
        scope = "everywhere (operator)" if everywhere else "in this server"
        await interaction.response.send_message(
            f"🧹 Erased **{ev}** identity event(s) and **{av}** stored avatar(s) for `{uid}` {scope}."
            f"{conduct_line}\nMessage archive is untouched — purge that separately if needed.",
            ephemeral=True)

    @msglog.command(name="roles", description="Toggle role-change logging on or off.")
    @app_commands.describe(enabled="On = log member role add/remove + role create/delete/edit")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def roles_cmd(self, interaction: discord.Interaction, enabled: bool):
        set_config(interaction.guild.id, msglog_roles=1 if enabled else 0)
        await interaction.response.send_message(
            f"🎭 Role logging **{'on' if enabled else 'off'}** — "
            + ("member role changes (with who) + role create/delete/edit post to the mod-log. "
               "Changes made by this bot itself (level roles, role menus, quarantine) are not logged."
               if enabled else "role changes are no longer logged."),
            ephemeral=True)

    @msglog.command(name="channels", description="Toggle channel-change logging on or off.")
    @app_commands.describe(enabled="On = log channel create/delete/edit + permission-overwrite changes")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def channels_cmd(self, interaction: discord.Interaction, enabled: bool):
        set_config(interaction.guild.id, msglog_channels=1 if enabled else 0)
        await interaction.response.send_message(
            f"🔐 Channel logging **{'on' if enabled else 'off'}** — "
            + ("channel create/delete/edit + permission-overwrite changes (with who) "
               "post to the mod-log, including changes made through this bot."
               if enabled else "channel changes are no longer logged."),
            ephemeral=True)

    @msglog.command(name="expressions", description="Toggle emoji/sticker create/delete/edit logging on or off.")
    @app_commands.describe(enabled="On = log custom emoji + sticker changes, with the image grabbed on deletion")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def expressions_cmd(self, interaction: discord.Interaction, enabled: bool):
        set_config(interaction.guild.id, msglog_expressions=1 if enabled else 0)
        await interaction.response.send_message(
            f"😀 Expression logging **{'on' if enabled else 'off'}** — "
            + ("custom emoji + sticker create/delete/edit (with who) post to the mod-log; "
               "deleted ones get their image re-posted so it isn't lost."
               if enabled else "emoji/sticker changes are no longer logged."),
            ephemeral=True)

    @msglog.command(name="status", description="Archive totals + configuration.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def status_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        self._flush()
        gid = str(interaction.guild.id)
        with self._conn() as c:
            total = c.execute("SELECT COUNT(*) FROM messages WHERE guild_id=?", (gid,)).fetchone()[0]
            deleted = c.execute("SELECT COUNT(*) FROM messages WHERE guild_id=? AND deleted_ts IS NOT NULL",
                                (gid,)).fetchone()[0]
            edits = c.execute("SELECT COUNT(*) FROM edits WHERE guild_id=?", (gid,)).fetchone()[0]
            span = c.execute("SELECT MIN(created_ts), MAX(created_ts) FROM messages WHERE guild_id=?",
                             (gid,)).fetchone()
        # This guild's own files only — the shared cache total is the operator's business.
        gdir = self._media_dir(interaction.guild.id)
        n_files, n_bytes = 0, 0
        try:
            for e in os.scandir(gdir):
                if e.is_file():
                    n_files += 1
                    n_bytes += e.stat().st_size
        except OSError:
            pass
        if str(interaction.guild.id) in ARCHIVE_GUILDS:  # operator: include the legacy flat cache
            try:
                for e in os.scandir(MEDIA_DIR):
                    if e.is_file():
                        n_files += 1
                        n_bytes += e.stat().st_size
            except OSError:
                pass
        cfg = get_config(interaction.guild.id)
        target = cfg.get("msglog_channel_id") or cfg.get("modlog_channel_id")
        db_mb = os.path.getsize(DB_PATH) / 1e6 if os.path.exists(DB_PATH) else 0
        tier = self._pro_state_lines(interaction.guild)[0]
        lines = [
            f"{'🟢 ON' if cfg.get('msglog_enabled') else '🔴 OFF'} · log → "
            + (f"<#{target}>" if target else "*none*"),
            tier,
            f"**{total:,}** messages archived"
            + (f" (<t:{int(span[0])}:d> → <t:{int(span[1])}:d>)" if span and span[0] else ""),
            f"**{deleted:,}** deletions · **{edits:,}** edits recorded",
            f"Members: {'🟢' if cfg.get('msglog_members', 1) else '🔴'} · "
            f"Voice: {'🟢' if cfg.get('msglog_voice', 1) else '🔴'} · "
            f"Roles: {'🟢' if cfg.get('msglog_roles', 1) else '🔴'} · "
            f"Names/timeouts: {'🟢' if cfg.get('msglog_names', 1) else '🔴'} · "
            f"AutoMod: {'🟢' if cfg.get('msglog_automod', 1) else '🔴'} · "
            f"Channels: {'🟢' if cfg.get('msglog_channels', 1) else '🔴'} · "
            f"Expressions: {'🟢' if cfg.get('msglog_expressions', 1) else '🔴'}",
        ]
        gcap = guild_media_cap_bytes(interaction.guild.id)
        if gcap is None:
            lines.append(
                f"Media cache: **{n_files}** files, {n_bytes/1e6:.1f} MB "
                f"(≤{cfg.get('msglog_media_max_mb')} MB/file, {cfg.get('msglog_media_days')}d "
                f"retention, {MEDIA_CAP_GB}GB cap) · DB: {db_mb:.1f} MB")
        else:
            lines.append(
                f"Files held: **{n_files}** ({n_bytes/1e6:.1f} of {gcap/1e6:.0f} MB, "
                f"≤{cfg.get('msglog_media_max_mb')} MB/file)")
        ignored = cfg.get("msglog_ignore_channels") or []
        if ignored:
            lines.append("Ignored: " + " ".join(f"<#{i}>" for i in ignored))
        await interaction.followup.send("📜 **Message log**\n" + "\n".join(lines), ephemeral=True)

    @msglog.command(name="deleted", description="A user's recently deleted messages, from the archive.")
    @app_commands.describe(user="Whose deleted messages", limit="How many (default 10, max 25)")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def deleted_cmd(self, interaction: discord.Interaction, user: discord.User,
                          limit: app_commands.Range[int, 1, 25] = 10):
        await interaction.response.defer(ephemeral=True, thinking=True)
        self._flush()
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM messages WHERE guild_id=? AND author_id=? AND deleted_ts IS NOT NULL "
                "ORDER BY deleted_ts DESC LIMIT ?",
                (str(interaction.guild.id), str(user.id), limit)).fetchall()
        if not rows:
            await interaction.followup.send(f"No archived deletions for {user.mention}.", ephemeral=True)
            return
        lines = []
        for r in rows:
            by = f" · deleted by **{r['deleted_by_name']}**" if r["deleted_by"] else ""
            kind = {"bulk": " · bulk", "mod": ""}.get(r["delete_kind"] or "", "")
            txt = r["content"]
            if not txt:
                st = parse_stickers(r["stickers"])
                pm = parse_json_obj(r["poll"])
                fw = parse_json_obj(r["forward"])
                if st:
                    txt = f"[sticker: {', '.join(s['name'] for s in st)}]"
                elif pm:
                    txt = f"[poll: {format_poll(pm, joiner=' | ')}]"
                elif fw:
                    txt = f"[forwarded: {fw.get('content') or ''}]"
                else:
                    txt = "*no text*"
            lines.append(f"<t:{int(r['deleted_ts'])}:R> in <#{r['channel_id']}>{by}{kind}\n"
                         f"> {_trunc(txt, 150)}")
        embed = discord.Embed(title=f"🗑️ Deleted messages — {user}",
                              description=_trunc("\n".join(lines), 4000), color=COLOR_SELF_DELETE)
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(ModLog(bot))
