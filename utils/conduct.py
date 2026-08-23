"""Conduct record — warnings, positive notes, and the evidence behind them.

Two deliberate design calls, both from the mod-records policy:

1. **Positives are first-class.** An entry is a `warn` OR a `note`, in one
   timeline. A record that only ever accumulates incidents makes every member
   look worse the longer they stay; recent good conduct has to be able to speak
   as loudly as an old bad night.

2. **Clearing is a soft delete.** `cleared_at/by/reason` are stamped and the row
   stays. A mod who wipes someone's record leaves their own trail — same ethos
   as quarantine being reversible and anti-nuke shipping in shadow mode. The one
   true delete is `forget_user()`, which is the erasure path (COPPA, right-to-be
   -forgotten) and takes the evidence files with it.

Evidence lives on disk, not as a Discord URL: CDN links have carried signed
`ex/is/hm` params since late 2023 and die within a day, so a stored URL is dead
evidence. Same approach mod_log takes with media_cache.

This store deliberately holds NO fingerprint, IP or gate data. Device telemetry
is purpose-bound to alt detection and never joins a character record.
"""
import os
import glob
import time
import hashlib
import sqlite3

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

# Relocatable for the same reason security_config.db is — the dashboard runs as
# a different user and may need read access without write access to the repo.
DB_PATH = os.environ.get("TORVEX_CONDUCT_DB") or os.path.join(ROOT, "conduct.db")
EVIDENCE_DIR = os.environ.get("TORVEX_CONDUCT_EVIDENCE") or os.path.join(ROOT, "conduct_evidence")

KINDS = ("warn", "note")


def _conn():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def _init():
    with _conn() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS entries (
                   id              INTEGER PRIMARY KEY AUTOINCREMENT,
                   guild_id        TEXT NOT NULL,
                   user_id         TEXT NOT NULL,
                   kind            TEXT NOT NULL,      -- warn | note
                   reason          TEXT NOT NULL,
                   moderator_id    TEXT NOT NULL,
                   moderator_name  TEXT,
                   created_at      REAL NOT NULL,
                   cleared_at      REAL,               -- NULL = still standing
                   cleared_by      TEXT,
                   cleared_by_name TEXT,
                   cleared_reason  TEXT
               )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS evidence (
                   id           INTEGER PRIMARY KEY AUTOINCREMENT,
                   entry_id     INTEGER NOT NULL,
                   guild_id     TEXT NOT NULL,
                   user_id      TEXT NOT NULL,
                   filename     TEXT NOT NULL,   -- as uploaded, for display
                   path         TEXT NOT NULL,   -- where the bytes actually are
                   bytes        INTEGER NOT NULL,
                   content_type TEXT,
                   sha256       TEXT,            -- integrity: proves it wasn't swapped
                   created_at   REAL NOT NULL
               )"""
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_entries_gu ON entries(guild_id, user_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_evidence_entry ON evidence(entry_id)")
    os.makedirs(EVIDENCE_DIR, exist_ok=True)


_init()


# ── writing ───────────────────────────────────────────────────────────────────

def add_entry(guild_id, user_id, kind, reason, moderator_id, moderator_name=None) -> int:
    """Record a warning or a note. Returns the entry id (what mods quote to clear)."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO entries(guild_id, user_id, kind, reason, moderator_id, "
            "moderator_name, created_at) VALUES (?,?,?,?,?,?,?)",
            (str(guild_id), str(user_id), kind, reason, str(moderator_id),
             moderator_name, time.time()))
        return cur.lastrowid


def safe_filename(name: str) -> str:
    """Strip anything that could escape EVIDENCE_DIR or confuse a shell.

    Dropping separators alone already makes traversal impossible, but a leftover
    `..` in a stored name is the kind of thing that becomes a bug the day someone
    reuses this string somewhere less careful, so collapse it here.
    """
    keep = "".join(ch for ch in (name or "") if ch.isalnum() or ch in "._- ")
    while ".." in keep:
        keep = keep.replace("..", ".")
    return (keep.strip(" .") or "file")[:80]


def evidence_path(guild_id, user_id, entry_id, attachment_id, filename) -> str:
    """Canonical on-disk name for one piece of evidence.

    The `u<user_id>` segment is what makes an orphaned file — one whose DB row
    is already gone — findable during a purge. This builder and the glob in
    forget_user() have to agree, so they live next to each other; when they were
    written separately the sweep silently matched nothing.
    """
    return os.path.join(
        EVIDENCE_DIR,
        f"{guild_id}_u{user_id}_e{entry_id}_a{attachment_id}_{safe_filename(filename)}")


async def save_attachment(attachment, entry_id, guild_id, user_id) -> dict:
    """Download one Discord attachment into the evidence store and record it.

    Returns the evidence row as a dict. Raises OSError/discord errors up to the
    caller — an evidence upload that silently half-fails is worse than a loud one.
    """
    path = evidence_path(guild_id, user_id, entry_id, attachment.id, attachment.filename)
    await attachment.save(path)

    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    digest = h.hexdigest()
    size = os.path.getsize(path)

    with _conn() as c:
        c.execute(
            "INSERT INTO evidence(entry_id, guild_id, user_id, filename, path, bytes, "
            "content_type, sha256, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (entry_id, str(guild_id), str(user_id), attachment.filename, path, size,
             getattr(attachment, "content_type", None), digest, time.time()))
    return {"filename": attachment.filename, "path": path, "bytes": size, "sha256": digest}


# ── reading ───────────────────────────────────────────────────────────────────

def list_entries(guild_id, user_id, include_cleared=False, kind=None):
    """A member's record in this guild, newest first."""
    q = "SELECT * FROM entries WHERE guild_id=? AND user_id=?"
    args = [str(guild_id), str(user_id)]
    if not include_cleared:
        q += " AND cleared_at IS NULL"
    if kind:
        q += " AND kind=?"
        args.append(kind)
    q += " ORDER BY created_at DESC"
    with _conn() as c:
        return [dict(r) for r in c.execute(q, args).fetchall()]


def get_entry(guild_id, entry_id):
    """One entry, scoped to the guild so an id from another server can't be read."""
    with _conn() as c:
        r = c.execute("SELECT * FROM entries WHERE id=? AND guild_id=?",
                      (int(entry_id), str(guild_id))).fetchone()
    return dict(r) if r else None


def evidence_for(entry_id):
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM evidence WHERE entry_id=? ORDER BY id", (int(entry_id),)).fetchall()]


def counts(guild_id, user_id) -> dict:
    """Headline numbers for an embed: standing warnings, notes, and cleared."""
    with _conn() as c:
        row = c.execute(
            "SELECT "
            " SUM(kind='warn' AND cleared_at IS NULL) AS warns,"
            " SUM(kind='note' AND cleared_at IS NULL) AS notes,"
            " SUM(cleared_at IS NOT NULL)            AS cleared "
            "FROM entries WHERE guild_id=? AND user_id=?",
            (str(guild_id), str(user_id))).fetchone()
    return {"warns": row["warns"] or 0, "notes": row["notes"] or 0, "cleared": row["cleared"] or 0}


def guild_evidence_bytes(guild_id) -> int:
    with _conn() as c:
        row = c.execute("SELECT COALESCE(SUM(bytes),0) AS b FROM evidence WHERE guild_id=?",
                        (str(guild_id),)).fetchone()
    return int(row["b"] or 0)


# ── clearing (soft) ───────────────────────────────────────────────────────────

def clear_entry(guild_id, entry_id, by_id, by_name, reason) -> bool:
    """Mark one entry cleared. False if it doesn't exist here or was already cleared."""
    with _conn() as c:
        cur = c.execute(
            "UPDATE entries SET cleared_at=?, cleared_by=?, cleared_by_name=?, cleared_reason=? "
            "WHERE id=? AND guild_id=? AND cleared_at IS NULL",
            (time.time(), str(by_id), by_name, reason, int(entry_id), str(guild_id)))
        return cur.rowcount > 0


def clear_all(guild_id, user_id, by_id, by_name, reason) -> int:
    """Clear every standing entry for a member. Returns how many were cleared."""
    with _conn() as c:
        cur = c.execute(
            "UPDATE entries SET cleared_at=?, cleared_by=?, cleared_by_name=?, cleared_reason=? "
            "WHERE guild_id=? AND user_id=? AND cleared_at IS NULL",
            (time.time(), str(by_id), by_name, reason, str(guild_id), str(user_id)))
        return cur.rowcount


# ── erasure (hard) ────────────────────────────────────────────────────────────

def forget_user(user_id, guild_id=None) -> dict:
    """Really delete: rows AND evidence files. This is the purge path.

    `guild_id=None` erases the user everywhere, which is what an underage or
    right-to-be-forgotten purge needs — a per-guild sweep would leave copies
    behind in servers nobody thought to check.

    Call this from any other forget/purge flow too. Evidence on disk is exactly
    the kind of thing that survives a purge everyone believed was complete.
    """
    uid = str(user_id)
    where, args = "user_id=?", [uid]
    if guild_id is not None:
        where += " AND guild_id=?"
        args.append(str(guild_id))

    removed_files = 0
    with _conn() as c:
        paths = [r["path"] for r in
                 c.execute(f"SELECT path FROM evidence WHERE {where}", args).fetchall()]
        ev = c.execute(f"DELETE FROM evidence WHERE {where}", args).rowcount
        en = c.execute(f"DELETE FROM entries WHERE {where}", args).rowcount

    for p in paths:
        try:
            os.remove(p)
            removed_files += 1
        except FileNotFoundError:
            pass
        except OSError:
            pass

    # Belt and braces: catch orphaned files whose rows were already gone. The
    # `u<uid>` segment comes from evidence_path() — keep the two in step.
    pattern = f"*_u{uid}_*" if guild_id is None else f"{guild_id}_u{uid}_*"
    for p in glob.glob(os.path.join(EVIDENCE_DIR, pattern)):
        try:
            os.remove(p)
            removed_files += 1
        except OSError:
            pass

    return {"entries": en, "evidence": ev, "files": removed_files}


def forget_guild(guild_id) -> dict:
    """Erase a whole server's record — for when a guild revokes consent."""
    gid = str(guild_id)
    removed_files = 0
    with _conn() as c:
        paths = [r["path"] for r in
                 c.execute("SELECT path FROM evidence WHERE guild_id=?", (gid,)).fetchall()]
        ev = c.execute("DELETE FROM evidence WHERE guild_id=?", (gid,)).rowcount
        en = c.execute("DELETE FROM entries WHERE guild_id=?", (gid,)).rowcount
    for p in paths:
        try:
            os.remove(p)
            removed_files += 1
        except OSError:
            pass
    return {"entries": en, "evidence": ev, "files": removed_files}
