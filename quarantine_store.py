"""Tiny SQLite store remembering which roles AltGuard stripped from a member
when it quarantined them, so a false positive can be fully restored.

Stored per user: the exact role IDs we removed (not @everyone, not managed
roles, not roles above the bot — those were never touched). Survives restarts.
"""
import json
import os
import sqlite3
import time

_PATH = os.path.join(os.path.dirname(__file__), "altguard_quarantine.db")


def _conn():
    c = sqlite3.connect(_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def init():
    with _conn() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS quarantined (
                   uid       TEXT PRIMARY KEY,
                   guild_id  TEXT,
                   role_ids  TEXT,
                   reason    TEXT,
                   ts        REAL
               )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS verifications (
                   uid          TEXT PRIMARY KEY,
                   guild_id     TEXT,
                   issued_at    REAL,
                   dm_delivered INTEGER,
                   status       TEXT,        -- pending | passed | quarantined
                   resolved_at  REAL
               )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS watchlist (
                   uid      TEXT PRIMARY KEY,
                   reason   TEXT,
                   added_at REAL
               )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS settings (
                   key   TEXT PRIMARY KEY,
                   value TEXT
               )"""
        )


# --- runtime settings (KV) — toggles that persist across restarts ------------
def get_setting(key, default=None):
    with _conn() as c:
        r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def set_setting(key, value):
    with _conn() as c:
        c.execute(
            "INSERT INTO settings(key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )


# --- watchlist: banned/wanted accounts to flag loudly if they ever surface ----
def watch(uid, reason):
    with _conn() as c:
        c.execute(
            "INSERT INTO watchlist(uid, reason, added_at) VALUES (?,?,?) "
            "ON CONFLICT(uid) DO UPDATE SET reason=excluded.reason",
            (str(uid), reason or "", time.time()),
        )


def unwatch(uid):
    with _conn() as c:
        cur = c.execute("DELETE FROM watchlist WHERE uid=?", (str(uid),))
        return cur.rowcount > 0


def is_watched(uid):
    with _conn() as c:
        return c.execute("SELECT 1 FROM watchlist WHERE uid=?", (str(uid),)).fetchone() is not None


def watch_reason(uid):
    with _conn() as c:
        r = c.execute("SELECT reason FROM watchlist WHERE uid=?", (str(uid),)).fetchone()
    return r["reason"] if r else None


def list_watch():
    with _conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM watchlist ORDER BY added_at DESC")]


# --- verification issuance tracking (so we never re-DM + keep a record) ------
def was_issued(uid):
    with _conn() as c:
        return c.execute("SELECT 1 FROM verifications WHERE uid=?", (str(uid),)).fetchone() is not None


def record_issue(uid, guild_id, dm_delivered):
    """Log that a verify link was issued. Keeps the first issued_at; refreshes
    dm flag. Does NOT reset a resolved status."""
    with _conn() as c:
        row = c.execute("SELECT uid FROM verifications WHERE uid=?", (str(uid),)).fetchone()
        if row:
            c.execute("UPDATE verifications SET dm_delivered=? WHERE uid=?", (int(dm_delivered), str(uid)))
        else:
            c.execute(
                "INSERT INTO verifications(uid, guild_id, issued_at, dm_delivered, status, resolved_at) "
                "VALUES (?,?,?,?,?,NULL)",
                (str(uid), str(guild_id), time.time(), int(dm_delivered), "pending"),
            )


def set_status(uid, status):
    with _conn() as c:
        c.execute(
            "UPDATE verifications SET status=?, resolved_at=? WHERE uid=?",
            (status, time.time(), str(uid)),
        )


def verification(uid):
    with _conn() as c:
        r = c.execute("SELECT * FROM verifications WHERE uid=?", (str(uid),)).fetchone()
    return dict(r) if r else None


def save(uid, guild_id, role_ids, reason):
    """Record the roles we removed. Won't clobber an earlier snapshot if the
    member is re-quarantined while already quarantined (keeps the original)."""
    with _conn() as c:
        existing = c.execute("SELECT uid FROM quarantined WHERE uid=?", (str(uid),)).fetchone()
        if existing:
            return
        c.execute(
            "INSERT INTO quarantined(uid, guild_id, role_ids, reason, ts) VALUES (?,?,?,?,?)",
            (str(uid), str(guild_id), json.dumps([int(r) for r in role_ids]), reason, time.time()),
        )


def add_roles(uid, guild_id, role_ids, reason="quarantine top-up"):
    """Merge extra role IDs into a held member's stored set — e.g. an autorole
    bot (MEE6) granted a role AFTER we quarantined, and the reconciliation
    listener stripped it. Folding it in here means /altguard-release (and the
    auto-release on pass) gives it back. Creates the record if missing."""
    ids = [int(r) for r in role_ids]
    if not ids:
        return
    with _conn() as c:
        row = c.execute("SELECT role_ids FROM quarantined WHERE uid=?", (str(uid),)).fetchone()
        if row:
            merged = list(dict.fromkeys(json.loads(row["role_ids"]) + ids))  # dedupe, keep order
            c.execute("UPDATE quarantined SET role_ids=? WHERE uid=?", (json.dumps(merged), str(uid)))
        else:
            c.execute(
                "INSERT INTO quarantined(uid, guild_id, role_ids, reason, ts) VALUES (?,?,?,?,?)",
                (str(uid), str(guild_id), json.dumps(ids), reason, time.time()),
            )


def get(uid):
    with _conn() as c:
        row = c.execute("SELECT * FROM quarantined WHERE uid=?", (str(uid),)).fetchone()
    return json.loads(row["role_ids"]) if row else None


def quarantine_reason(uid):
    with _conn() as c:
        row = c.execute("SELECT reason FROM quarantined WHERE uid=?", (str(uid),)).fetchone()
    return row["reason"] if row else None


def quarantined_since(uid):
    """Epoch seconds when the quarantine role was applied (the verify clock
    start). None if not on record."""
    with _conn() as c:
        row = c.execute("SELECT ts FROM quarantined WHERE uid=?", (str(uid),)).fetchone()
    return row["ts"] if row else None


def pop(uid):
    """Return stored role IDs and delete the record (used on release)."""
    role_ids = get(uid)
    with _conn() as c:
        c.execute("DELETE FROM quarantined WHERE uid=?", (str(uid),))
    return role_ids or []


def is_quarantined(uid):
    with _conn() as c:
        return c.execute("SELECT 1 FROM quarantined WHERE uid=?", (str(uid),)).fetchone() is not None
