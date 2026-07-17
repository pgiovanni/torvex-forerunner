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

Storage: ~1.25M messages ≈ 300-600 MB SQLite; media cache is bounded by
msglog_media_days retention (re-posts made to the log channel persist in
Discord, so pruning the disk cache doesn't lose logged evidence).
Per-guild opt-in via security_config (msglog_* keys), same as antinuke/linkguard.
Backfill of pre-cog history = backfill_history.py (REST, writes the same DB).
"""
import asyncio
import glob
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

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
DB_PATH = os.path.join(ROOT, "messages.db")
MEDIA_DIR = os.path.join(ROOT, "media_cache")
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
        lines.append(f"[{ts}] {r['author_name'] or r['author_id']} ({r['author_id']}): "
                     f"{r['content'] or ''}{atts}")
    return "\n".join(lines) + "\n"


def files_to_prune(entries, cutoff_ts):
    """entries = [(path, mtime)]; return paths older than cutoff. Pure for tests."""
    return [p for p, m in entries if m < cutoff_ts]


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
        self._rolelog_hits = {}         # user_id -> [timestamps] for role-log rate limiting
        self._rolelog_cd = {}           # user_id -> cooldown-until ts (logs suppressed while spamming)
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
            c.execute(
                """CREATE TABLE IF NOT EXISTS edits (
                       message_id TEXT, guild_id TEXT,
                       edited_ts REAL, old_content TEXT, new_content TEXT
                   )""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_edits_msg ON edits(message_id)")

    def _conn(self):
        c = sqlite3.connect(DB_PATH, timeout=30)
        c.row_factory = sqlite3.Row
        return c

    async def cog_load(self):
        self.flusher.start()
        self.media_pruner.start()
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
        self.media_pruner.cancel()
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
                        created_ts,content,reply_to,attachments,stickers,
                        deleted_ts,deleted_by,deleted_by_name,delete_kind)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    [(r["message_id"], r["guild_id"], r["channel_id"], r["author_id"],
                      r["author_name"], r["bot"], r["webhook"], r["created_ts"],
                      r["content"], r["reply_to"], r["attachments"], r["stickers"],
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
        atts = [{"filename": a.filename, "url": a.url, "size": a.size,
                 "content_type": a.content_type} for a in message.attachments]
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
        })
        if atts or message.stickers:
            await self._cache_media(message)

    async def _cache_media(self, message):
        cfg = get_config(message.guild.id)
        if not cfg.get("msglog_media"):
            return
        cap = int(cfg.get("msglog_media_max_mb", 25)) * 1024 * 1024
        for i, att in enumerate(message.attachments):
            if att.size > cap:
                continue
            path = os.path.join(MEDIA_DIR, f"{message.id}_{i}_{safe_filename(att.filename)}")
            try:
                await att.save(path)
            except Exception:
                pass  # CDN hiccup — the attachment URL metadata is still archived
        # stickers too: a sticker message has no content and no attachments, so
        # without this the delete log would come out EMPTY (they're tiny, ≤512KB)
        for i, s in enumerate(message.stickers):
            ext = getattr(s.format, "file_extension", "png")
            if ext == "json":
                continue  # Lottie — no image file to re-post
            path = os.path.join(MEDIA_DIR, f"{message.id}_s{i}_{safe_filename(s.name)}.{ext}")
            try:
                data = await s.read()
                with open(path, "wb") as f:
                    f.write(data)
            except Exception:
                pass  # the archived sticker url is still a recovery path

    def _cached_media(self, message_id):
        return sorted(glob.glob(os.path.join(MEDIA_DIR, f"{message_id}_*")))

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
    def _log_channel(self, guild, cfg):
        cid = cfg.get("msglog_channel_id") or cfg.get("modlog_channel_id")
        return guild.get_channel(int(cid)) if cid else None

    def _media_channel(self, guild, cfg):
        """Separate destination for deleted-media re-posts (age-restricted staff
        channel). None = media stays attached to the log embeds as before."""
        cid = cfg.get("msglog_media_channel_id")
        return guild.get_channel(int(cid)) if cid else None

    def _skip_logging(self, cfg, channel_id, log_ch):
        if log_ch is not None and int(channel_id) == log_ch.id:
            return True  # never log the log channel — feedback loop
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
            row = {"message_id": str(m.id), "guild_id": str(payload.guild_id),
                   "channel_id": str(payload.channel_id),
                   "author_id": str(m.author.id), "author_name": str(m.author),
                   "bot": 1 if m.author.bot else 0, "webhook": 1 if m.webhook_id else 0,
                   "created_ts": m.created_at.timestamp(), "content": m.content or "",
                   "reply_to": None, "attachments": None,
                   "stickers": json.dumps(sticker_meta(m.stickers)) if m.stickers else None}

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

        if hit:
            title, color = "🛡️ Message deleted by moderator", COLOR_MOD_DELETE
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
                    embed.add_field(name="📣", value="Contained mentions", inline=True)
            stickers = parse_stickers(row.get("stickers"))
            if stickers:  # sticker messages have no content — this WAS the "empty log" bug
                embed.add_field(
                    name="Sticker" if len(stickers) == 1 else "Stickers",
                    value=_trunc(", ".join(f"[{s['name']}]({s['url']})" if s["url"] else s["name"]
                                           for s in stickers), 1024),
                    inline=True)
        else:
            embed.description = (f"Message `{payload.message_id}` in <#{payload.channel_id}> "
                                 f"— **not in the archive** (predates tracking).")
        if hit:
            embed.add_field(name="Deleted by", value=_deleter_line(hit), inline=False)
        files = []
        atts = json.loads(row["attachments"]) if row and row.get("attachments") else []
        for path in self._cached_media(payload.message_id):
            if len(files) >= 9:
                break
            try:
                if os.path.getsize(path) <= guild.filesize_limit:
                    files.append(discord.File(path, filename=media_display_name(path, atts)))
            except OSError:
                pass
        media_ch = self._media_channel(guild, cfg)
        route_media = bool(files) and media_ch is not None and media_ch.id != log_ch.id
        if atts and not files:
            embed.add_field(name="Attachments (not recoverable)",
                            value=_trunc("\n".join(a.get("filename", "?") for a in atts), 512),
                            inline=False)
        elif route_media:
            embed.add_field(name="🖼️ Deleted media",
                            value=f"re-posted in {media_ch.mention}", inline=False)
        elif files:
            embed.add_field(name="🖼️ Deleted media re-posted below", value="​", inline=False)
        embed.set_footer(text=f"Message ID {payload.message_id}")
        await log_ch.send(embed=embed,
                          files=discord.utils.MISSING if route_media else (files or discord.utils.MISSING),
                          allowed_mentions=discord.AllowedMentions.none())
        if route_media:
            ref = discord.Embed(title="🖼️ Deleted media", color=color)
            ref.description = (f"From message `{payload.message_id}` in <#{payload.channel_id}>"
                               + (f", author <@{row['author_id']}>" if row else ""))
            ref.set_footer(text=f"Message ID {payload.message_id}")
            await media_ch.send(embed=ref, files=files,
                                allowed_mentions=discord.AllowedMentions.none())

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
        media = [p for mid in ids for p in self._cached_media(mid)]
        if media:
            embed.add_field(name="Media", value=f"{len(media)} cached file(s) preserved on disk",
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
            return  # embed-unfurl / pin / component update — not a content edit
        new = data.get("content") or ""
        row = self._get_row(payload.message_id)
        old = row["content"] if row else None
        if old is not None and old == new:
            return  # content unchanged (unfurl adds an embed, fires MESSAGE_UPDATE)
        edited_ts = time.time()
        row_in_mem = self._recent.get(str(payload.message_id))
        if row_in_mem is not None:
            row_in_mem["content"] = new
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
        embed.set_footer(text=f"Message ID {payload.message_id}")
        await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    # ------------------------------------------------------------- media pruning
    @tasks.loop(hours=12)
    async def media_pruner(self):
        days = 30
        for gid in all_enabled("msglog"):
            days = max(days, int(get_config(gid).get("msglog_media_days", 30)))
        cutoff = time.time() - days * 86400
        try:
            entries = [(e.path, e.stat().st_mtime) for e in os.scandir(MEDIA_DIR) if e.is_file()]
        except OSError:
            return
        for path in files_to_prune(entries, cutoff):
            try:
                os.remove(path)
            except OSError:
                pass

    @media_pruner.before_loop
    async def _before_prune(self):
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
        return self._log_channel(guild, cfg), cfg

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
        embed = discord.Embed(
            title="🔨 Member banned" if banned else "♻️ Member unbanned",
            color=COLOR_BAN if banned else COLOR_JOIN,
            description=await self._user_line(entry.target, target_id))
        embed.add_field(name="By", value=self._mod_line(rec), inline=True)
        embed.add_field(name="Reason", value=_trunc(rec["reason"] or "No reason provided"), inline=False)
        embed.set_footer(text=f"User ID {target_id}")
        await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

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
        log_ch = self._log_channel(guild, cfg)
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
        log_ch = self._log_channel(guild, cfg)
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
        log_ch = self._log_channel(guild, cfg)
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
        log_ch = self._log_channel(guild, cfg)
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
        # Voice channel join / leave / move. A same-channel before→after (mute,
        # deafen, stream or camera toggle) is deliberately NOT logged — this
        # tracks which channel a member is in, not their device state. Own bot
        # excluded; other bots gated by msglog_log_bots (music bots are noise).
        guild = member.guild
        if guild is None or member.id == self.bot.user.id:
            return
        b, a = before.channel, after.channel
        if b == a:
            return
        if not is_enabled(guild.id, "msglog"):
            return
        cfg = get_config(guild.id)
        if not cfg.get("msglog_voice", 1):
            return
        if member.bot and not cfg.get("msglog_log_bots", 0):
            return
        log_ch = self._log_channel(guild, cfg)
        if log_ch is None:
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
        await interaction.response.send_message(
            "✅ **Message log enabled** — every message is archived from now on; "
            "deletes (with who-deleted-it attribution), edits and bulk deletes get logged"
            + (f" to <#{target}>." if target else
               ".\n⚠️ No log channel set — pass `channel:` or logs stay archive-only."),
            ephemeral=True)

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
        n_files, n_bytes = 0, 0
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
        lines = [
            f"{'🟢 ON' if cfg.get('msglog_enabled') else '🔴 OFF'} · log → "
            + (f"<#{target}>" if target else "*none*"),
            f"**{total:,}** messages archived"
            + (f" (<t:{int(span[0])}:d> → <t:{int(span[1])}:d>)" if span and span[0] else ""),
            f"**{deleted:,}** deletions · **{edits:,}** edits recorded",
            f"Media cache: **{n_files}** files, {n_bytes/1e6:.1f} MB "
            f"(≤{cfg.get('msglog_media_max_mb')} MB/file, {cfg.get('msglog_media_days')}d retention)",
            f"DB: {db_mb:.1f} MB",
            f"Members: {'🟢' if cfg.get('msglog_members', 1) else '🔴'} · "
            f"Voice: {'🟢' if cfg.get('msglog_voice', 1) else '🔴'} · "
            f"Roles: {'🟢' if cfg.get('msglog_roles', 1) else '🔴'} · "
            f"Channels: {'🟢' if cfg.get('msglog_channels', 1) else '🔴'} · "
            f"Expressions: {'🟢' if cfg.get('msglog_expressions', 1) else '🔴'}",
        ]
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
                txt = f"[sticker: {', '.join(s['name'] for s in st)}]" if st else "*no text*"
            lines.append(f"<t:{int(r['deleted_ts'])}:R> in <#{r['channel_id']}>{by}{kind}\n"
                         f"> {_trunc(txt, 150)}")
        embed = discord.Embed(title=f"🗑️ Deleted messages — {user}",
                              description=_trunc("\n".join(lines), 4000), color=COLOR_SELF_DELETE)
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(ModLog(bot))
