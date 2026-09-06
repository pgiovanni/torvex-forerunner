"""Mention index for `/mentions` — who was pinged by which archived message.

Why a table instead of searching content: the archive holds ~1.3M rows for the
home guild alone (2 GB). `content LIKE '%<@id>%'` over that took 24 s cold on
the VPS (2026-09-06) — unusable behind a slash command. `message_mentions`
is written at flush time from the same row the archive stores, indexed on
(guild, uid, ts), so "your latest pings" is one indexed read joined to
`messages` by primary key. Rows follow the message's retention window and are
swept with it; nothing here outlives the archive row it points at.

Pure sqlite + stdlib on purpose: unit-tested locally (no discord.py in the
local venv), imported by cogs/mod_log.py (writer), cogs/mentions.py (reader)
and tools/backfill_mentions.py (one-off historical fill).

Kinds:
  user   — `<@id>` / `<@!id>` markup naming the member
  role   — `<@&id>` markup for a role (matched to the roles the member holds
           only at read time, so the index never has to know memberships)
  reply  — a reply to one of the member's messages (Discord pings the author
           unless the sender turned that off — we can't see that flag, so
           replies are listed separately and can be left out)
@everyone/@here are deliberately NOT indexed — every member would see every
announcement and the useful pings would drown.
"""
import re
import time

USER_RE = re.compile(r"<@!?(\d+)>")
ROLE_RE = re.compile(r"<@&(\d+)>")

SCHEMA = """
CREATE TABLE IF NOT EXISTS message_mentions (
    message_id TEXT NOT NULL,
    guild_id   TEXT NOT NULL,
    uid        TEXT NOT NULL,      -- user id, role id, or replied-to author id
    kind       TEXT NOT NULL,      -- 'user' | 'role' | 'reply'
    created_ts REAL NOT NULL,
    UNIQUE(message_id, uid, kind)
)"""
INDEX = ("CREATE INDEX IF NOT EXISTS idx_mm_target "
         "ON message_mentions(guild_id, uid, created_ts)")

DEFAULT_LOOKBACK_DAYS = 30


def ensure_schema(conn):
    conn.execute(SCHEMA)
    conn.execute(INDEX)


def mention_rows(row, reply_author=None):
    """One archived message row -> [(message_id, guild_id, uid, kind, created_ts)].

    Self-pings and self-replies are dropped — nobody needs a list of the times
    they mentioned themselves. `reply_author` is the author id of the message
    `row['reply_to']` points at, resolved by the caller (None = unknown).
    """
    mid, gid = str(row["message_id"]), str(row["guild_id"])
    author = str(row.get("author_id") or "")
    ts = float(row.get("created_ts") or 0)
    content = row.get("content") or ""
    out = []
    for uid in dict.fromkeys(USER_RE.findall(content)):
        if uid != author:
            out.append((mid, gid, uid, "user", ts))
    for rid in dict.fromkeys(ROLE_RE.findall(content)):
        out.append((mid, gid, rid, "role", ts))
    if row.get("reply_to") and reply_author and str(reply_author) != author:
        out.append((mid, gid, str(reply_author), "reply", ts))
    return out


def resolve_reply_authors(conn, rows, recent=None):
    """{parent_message_id: author_id} for every row that is a reply.

    Checks the in-memory recent cache first (the parent is usually seconds
    old), then the archive in one IN-query. Parents that were never archived
    (pre-bot history, ignored channel) stay unresolved and the reply is simply
    not indexed — a miss, never a wrong attribution.
    """
    want = {str(r["reply_to"]) for r in rows if r.get("reply_to")}
    found = {}
    if recent:
        for pid in list(want):
            parent = recent.get(pid)
            if parent and parent.get("author_id"):
                found[pid] = str(parent["author_id"])
                want.discard(pid)
    if want:
        ids = list(want)
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            q = ("SELECT message_id, author_id FROM messages WHERE message_id IN (%s)"
                 % ",".join("?" * len(chunk)))
            for pid, aid in conn.execute(q, chunk):
                if aid:
                    found[str(pid)] = str(aid)
    return found


def write_mentions(conn, rows, recent=None):
    """Index a batch of freshly flushed archive rows. Returns rows written."""
    if not rows:
        return 0
    parents = resolve_reply_authors(conn, rows, recent)
    out = []
    for r in rows:
        out.extend(mention_rows(r, parents.get(str(r.get("reply_to") or ""))))
    if not out:
        return 0
    before = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO message_mentions(message_id,guild_id,uid,kind,created_ts)"
        " VALUES (?,?,?,?,?)", out)
    return conn.total_changes - before  # rows actually inserted, not attempted


def sweep(conn, guild_id, cutoff_ts):
    """Drop index rows older than the guild's text window (mirrors messages)."""
    return conn.execute("DELETE FROM message_mentions WHERE guild_id=? AND created_ts<?",
                        (str(guild_id), cutoff_ts)).rowcount


def backfill(conn, guild_id=None, batch=5000, progress=None):
    """One-off: index every archived message that carries a ping or is a reply.

    Walks `messages` by rowid in batches, committing each — safe to re-run
    (UNIQUE + OR IGNORE) and to stop halfway. Reply parents are resolved
    through the archive itself. Returns (messages scanned, index rows written).
    """
    ensure_schema(conn)
    where = "(content LIKE '%<@%' OR reply_to IS NOT NULL)"
    params = []
    if guild_id:
        where += " AND guild_id=?"
        params.append(str(guild_id))
    last, scanned, written = 0, 0, 0
    while True:
        rows = conn.execute(
            "SELECT rowid, message_id, guild_id, author_id, created_ts, content, reply_to"
            f" FROM messages WHERE rowid>? AND {where} ORDER BY rowid LIMIT ?",
            [last, *params, batch]).fetchall()
        if not rows:
            break
        dicts = [dict(zip(("rowid", "message_id", "guild_id", "author_id", "created_ts",
                           "content", "reply_to"), r)) for r in rows]
        written += write_mentions(conn, dicts)
        conn.commit()
        scanned += len(dicts)
        last = dicts[-1]["rowid"]
        if progress:
            progress(scanned, written)
    return scanned, written


# ── read side ───────────────────────────────────────────────────────────────

def fetch_mentions(conn, guild_id, uid, role_ids=(), since_ts=0.0, limit=10,
                   include_bots=False, include_replies=True, include_roles=False):
    """Latest messages that pinged `uid`, newest first, joined to the archive.

    One row per message (a message that pings you AND your role counts once,
    as a user ping). Over-fetches so the caller can drop channels the member
    can't see and still fill `limit`. Own messages are never returned.
    """
    gid, uid = str(guild_id), str(uid)
    kinds = ["'user'"]
    targets = [uid]
    if include_replies:
        kinds.append("'reply'")
    roles = [str(r) for r in role_ids] if include_roles else []
    cond = f"(mm.uid=? AND mm.kind IN ({','.join(kinds)}))"
    if roles:
        cond = f"({cond} OR (mm.kind='role' AND mm.uid IN ({','.join('?' * len(roles))})))"
        targets.extend(roles)
    sql = (
        "SELECT mm.message_id, mm.kind, mm.uid AS target, m.channel_id, m.author_id,"
        " m.author_name, m.bot, m.webhook, m.created_ts, m.content, m.attachments,"
        " m.stickers, m.deleted_ts, m.delete_kind, m.deleted_by, m.deleted_by_name"
        " FROM message_mentions mm JOIN messages m ON m.message_id = mm.message_id"
        f" WHERE mm.guild_id=? AND {cond} AND mm.created_ts>=? AND m.author_id!=?"
    )
    params = [gid, *targets, float(since_ts), uid]
    if not include_bots:
        sql += " AND COALESCE(m.bot,0)=0 AND COALESCE(m.webhook,0)=0"
    sql += " ORDER BY mm.created_ts DESC LIMIT ?"
    params.append(max(limit, 1) * 4)
    seen, out = {}, []
    cur = conn.execute(sql, params)
    cols = [c[0] for c in cur.description]
    for r in cur.fetchall():
        d = dict(zip(cols, tuple(r)))
        mid = d["message_id"]
        prev = seen.get(mid)
        if prev is None:
            seen[mid] = d
            out.append(d)
        elif prev["kind"] != "user" and d["kind"] == "user":
            prev.update(kind="user", target=d["target"])
    return out


def mentions_user(content, uid):
    return f"<@{uid}>" in (content or "") or f"<@!{uid}>" in (content or "")


def original_ping_text(conn, message_id, uid):
    """The earliest edit whose BEFORE text still carried the ping — what the
    member's notification actually said before it was edited away."""
    for (old,) in conn.execute(
            "SELECT old_content FROM edits WHERE message_id=? ORDER BY edited_ts",
            (str(message_id),)):
        if mentions_user(old, uid):
            return old
    return None


def classify(row, uid, conn=None):
    """-> (state, text)  state ∈ live | deleted | removed | edited_out.

    deleted    = the author took it back (self-delete / unattributed) — the
                 classic ghost ping; the text is shown so the member knows
                 what it said.
    removed    = a moderator or a bulk purge took it down — staff already
                 decided that content shouldn't stand, so it is NOT re-shown.
    edited_out = still there, but the ping was edited away; the original
                 wording comes from the edits table.
    """
    text = row.get("content") or ""
    if row.get("deleted_ts"):
        if (row.get("delete_kind") or "") in ("mod", "bulk"):
            return "removed", ""
        return "deleted", text
    if row.get("kind") == "user" and not mentions_user(text, uid):
        old = original_ping_text(conn, row["message_id"], uid) if conn is not None else None
        return "edited_out", old or text
    return "live", text


def snippet(text, n=120):
    t = " ".join((text or "").split())
    if not t:
        return "*no text*"
    return t if len(t) <= n else t[: n - 1] + "…"


def lookback_ts(days=DEFAULT_LOOKBACK_DAYS, now=None):
    return (now if now is not None else time.time()) - days * 86400
