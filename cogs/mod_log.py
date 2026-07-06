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
MENTION_RE = re.compile(r"<@[!&]?\d+>|@everyone|@here")

COLOR_SELF_DELETE = 0xE67E22
COLOR_MOD_DELETE = 0xC0392B
COLOR_BULK = 0x8B0000
COLOR_EDIT = 0x3498DB
COLOR_JOIN = 0x3BA55D
COLOR_LEAVE = 0x95A5A6
COLOR_KICK = 0xE8A33D
COLOR_BAN = 0x992D22
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
        lines.append(f"[{ts}] {r['author_name'] or r['author_id']} ({r['author_id']}): "
                     f"{r['content'] or ''}{atts}")
    return "\n".join(lines) + "\n"


def files_to_prune(entries, cutoff_ts):
    """entries = [(path, mtime)]; return paths older than cutoff. Pure for tests."""
    return [p for p, m in entries if m < cutoff_ts]


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
            "stickers": json.dumps([s.name for s in message.stickers]) if message.stickers else None,
        })
        if atts:
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

    def _skip_logging(self, cfg, channel_id, log_ch):
        if log_ch is not None and int(channel_id) == log_ch.id:
            return True  # never log the log channel — feedback loop
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
                   "reply_to": None, "attachments": None, "stickers": None}

        author_id = int(row["author_id"]) if row else None
        hit = await self._attribute(guild, payload.channel_id, author_id)
        kind = "mod" if hit else ("self" if row else "unknown")
        self._mark_deleted(payload.message_id, kind,
                           by_id=hit and hit["user_id"], by_name=hit and _deleter_name(hit))

        cfg = get_config(payload.guild_id)
        log_ch = self._log_channel(guild, cfg)
        if not cfg.get("msglog_deletes") or log_ch is None \
                or self._skip_logging(cfg, payload.channel_id, log_ch):
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
        else:
            embed.description = (f"Message `{payload.message_id}` in <#{payload.channel_id}> "
                                 f"— **not in the archive** (predates tracking).")
        if hit:
            embed.add_field(name="Deleted by", value=_deleter_line(hit), inline=False)
        files = []
        atts = json.loads(row["attachments"]) if row and row.get("attachments") else []
        for i, path in enumerate(self._cached_media(payload.message_id)):
            if len(files) >= 9:
                break
            try:
                if os.path.getsize(path) <= guild.filesize_limit:
                    orig = atts[i]["filename"] if i < len(atts) else os.path.basename(path)
                    files.append(discord.File(path, filename=orig))
            except OSError:
                pass
        if atts and not files:
            embed.add_field(name="Attachments (not recoverable)",
                            value=_trunc("\n".join(a.get("filename", "?") for a in atts), 512),
                            inline=False)
        elif files:
            embed.add_field(name="🖼️ Deleted media re-posted below", value="​", inline=False)
        embed.set_footer(text=f"Message ID {payload.message_id}")
        await log_ch.send(embed=embed, files=files or discord.utils.MISSING,
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

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        guild = member.guild
        log_ch, _ = self._members_log_channel(guild)
        if log_ch is None:
            return
        await asyncio.sleep(3.0)  # let the invites cog attribute the join first
        embed = discord.Embed(
            title="📥 Member joined", color=COLOR_JOIN,
            description=f"{member.mention} (`{member.id}`)" + (" 🤖" if member.bot else ""))
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
            description=f"<@{target_id}> (`{target_id}`)")
        embed.add_field(name="By", value=self._mod_line(rec), inline=True)
        embed.add_field(name="Reason", value=_trunc(rec["reason"] or "No reason provided"), inline=False)
        embed.set_footer(text=f"User ID {target_id}")
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
                description=f"{member.mention} (`{member.id}`)" + (" 🤖" if member.bot else ""))
            embed.add_field(name="By", value=self._mod_line(rec), inline=True)
            embed.add_field(name="Joined", value=joined, inline=True)
            embed.add_field(name="Reason", value=_trunc(rec["reason"] or "No reason provided"), inline=False)
        else:    # plain leave
            embed = discord.Embed(
                title="📤 Member left", color=COLOR_LEAVE,
                description=f"{member.mention} (`{member.id}`)" + (" 🤖" if member.bot else ""))
            embed.add_field(name="Joined", value=joined, inline=True)
            embed.add_field(name="Members", value=f"{guild.member_count:,}", inline=True)
        if roles:
            embed.add_field(name="Roles", value=_trunc(" ".join(roles), 1024), inline=False)
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
            lines.append(f"<t:{int(r['deleted_ts'])}:R> in <#{r['channel_id']}>{by}{kind}\n"
                         f"> {_trunc(r['content'] or '*no text*', 150)}")
        embed = discord.Embed(title=f"🗑️ Deleted messages — {user}",
                              description=_trunc("\n".join(lines), 4000), color=COLOR_SELF_DELETE)
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(ModLog(bot))
