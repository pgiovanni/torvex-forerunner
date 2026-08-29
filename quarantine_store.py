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
        # accounts a mod has cleared (via /altguard-release): trusted, so a new
        # account matching THEIR device should still be flagged + quarantined for
        # review, but the cleared member themselves must never be re-quarantined
        # by the alt-cascade. Their fingerprint stays on file as a live detector.
        c.execute(
            """CREATE TABLE IF NOT EXISTS cleared (
                   uid       TEXT PRIMARY KEY,
                   reason    TEXT,
                   cleared_at REAL
               )"""
        )
        # Members the verify-prune declined to kick because the gate holds a
        # high-confidence, clean-scoring link-open for them (they fingerprinted
        # and then bailed at the Discord login). They stay QUARANTINED — this is
        # a stay of execution, not a release — and a mod is asked to decide.
        # Recorded so the ask goes out ONCE, not every hourly sweep.
        c.execute(
            """CREATE TABLE IF NOT EXISTS prune_spared (
                   uid       TEXT PRIMARY KEY,
                   verdict   TEXT,
                   risk      INTEGER,
                   spared_at REAL
               )"""
        )
        c.execute(
            # Which mod-log message belongs to which precapture row, so a card
            # can be corrected after the fact. A link-open alert posts within a
            # second, but the gate's intel drain scores the row on a timer —
            # 109s on the 2026-08-09 alert whose Connection line was a bare IPv6
            # while the gate went on to learn "AS7552 Viettel Group, residential".
            # Without this the card stays wrong forever.
            """CREATE TABLE IF NOT EXISTS precap_cards (
                   precap_id  INTEGER PRIMARY KEY,
                   channel_id TEXT,
                   message_id TEXT,
                   refreshed  INTEGER DEFAULT 0,
                   ts         REAL
               )"""
        )


# --- precapture card tracking ------------------------------------------------
def remember_precap_card(precap_id, channel_id, message_id):
    """Note the message a precapture alert was posted as. REPLACE, not INSERT:
    a re-posted row should track the newest card, never raise."""
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO precap_cards"
                  "(precap_id, channel_id, message_id, refreshed, ts) VALUES (?,?,?,0,?)",
                  (int(precap_id), str(channel_id), str(message_id), time.time()))


def precap_cards_to_refresh(limit=25, max_age_s=86400):
    """Cards posted but not yet corrected. Age-bounded so a row the drain never
    scores (it can decline to) is not retried forever."""
    cutoff = time.time() - max_age_s
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM precap_cards WHERE refreshed=0 AND ts>=? "
            "ORDER BY ts ASC LIMIT ?", (cutoff, int(limit))).fetchall()]


def mark_precap_refreshed(precap_id):
    with _conn() as c:
        c.execute("UPDATE precap_cards SET refreshed=1 WHERE precap_id=?", (int(precap_id),))


# --- prune stay-of-execution -------------------------------------------------
def was_spared(uid):
    with _conn() as c:
        return c.execute("SELECT 1 FROM prune_spared WHERE uid=?", (str(uid),)).fetchone() is not None


def record_spared(uid, verdict, risk):
    """Note that the prune held off on this member. Returns True the FIRST time
    (caller should alert), False on every later sweep (stay quiet)."""
    with _conn() as c:
        if c.execute("SELECT 1 FROM prune_spared WHERE uid=?", (str(uid),)).fetchone():
            return False
        c.execute("INSERT INTO prune_spared(uid, verdict, risk, spared_at) VALUES (?,?,?,?)",
                  (str(uid), verdict, int(risk or 0), time.time()))
        return True


def unspare(uid):
    """Drop the stay — on release, or if a mod decides to let the prune run."""
    with _conn() as c:
        c.execute("DELETE FROM prune_spared WHERE uid=?", (str(uid),))


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


# --- cleared: mod-trusted accounts (released) — keep as a detector, never re-quarantine
def clear(uid, reason=""):
    with _conn() as c:
        c.execute(
            "INSERT INTO cleared(uid, reason, cleared_at) VALUES (?,?,?) "
            "ON CONFLICT(uid) DO UPDATE SET reason=excluded.reason, cleared_at=excluded.cleared_at",
            (str(uid), reason or "", time.time()),
        )


def unclear(uid):
    with _conn() as c:
        cur = c.execute("DELETE FROM cleared WHERE uid=?", (str(uid),))
        return cur.rowcount > 0


def is_cleared(uid):
    with _conn() as c:
        return c.execute("SELECT 1 FROM cleared WHERE uid=?", (str(uid),)).fetchone() is not None


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
            # restart the prune clock on re-quarantine (e.g. leave->rejoin) so the
            # member gets a fresh PRUNE_HOURS window instead of inheriting an ancient
            # ts that insta-kicks on rejoin. Keep the original role snapshot.
            c.execute("UPDATE quarantined SET ts=? WHERE uid=?", (time.time(), str(uid)))
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


def guild_of(uid):
    """Which guild the stored snapshot belongs to, as an int (None if none).

    The table is keyed by uid alone, so a member held in two servers has ONE
    snapshot. Anything that pops a record must check this first — popping in
    server B would throw away server A's roles, and A could then never restore
    them."""
    with _conn() as c:
        row = c.execute("SELECT guild_id FROM quarantined WHERE uid=?", (str(uid),)).fetchone()
    if not row or not row["guild_id"]:
        return None
    try:
        return int(row["guild_id"])
    except (TypeError, ValueError):
        return None


def quarantine_reason(uid):
    with _conn() as c:
        row = c.execute("SELECT reason FROM quarantined WHERE uid=?", (str(uid),)).fetchone()
    return row["reason"] if row else None


def quarantined_alts_of(uid):
    """uids whose quarantine reason marks them as a cascade of `uid`
    ("alt of <uid> (same device)"). The trailing space in the pattern keeps
    uid 123 from matching 'alt of 1234'."""
    with _conn() as c:
        rows = c.execute(
            "SELECT uid FROM quarantined WHERE reason LIKE ?",
            (f"%alt of {uid} %",),
        ).fetchall()
    return [r["uid"] for r in rows]


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


def list_quarantined(guild_id=None):
    """Every uid with a live quarantine record, as ints — one guild's, or all.

    Feeds `/altguard-release everyone:True`: the store is the memory of who is
    held, and it can outlive the member (someone who left while quarantined
    keeps a row, and the reconciliation listener would re-strip them on a
    rejoin), so a bulk release has to walk the store, not just the role."""
    with _conn() as c:
        if guild_id is None:
            rows = c.execute("SELECT uid FROM quarantined").fetchall()
        else:
            rows = c.execute("SELECT uid FROM quarantined WHERE guild_id=?",
                             (str(guild_id),)).fetchall()
    out = []
    for r in rows:
        try:
            out.append(int(r["uid"]))
        except (TypeError, ValueError):
            continue
    return out
