"""State store for /lock and /unlock (cogs/moderation.py).

A lock denies the standard "talk" permissions to @everyone in one channel.
The whole point of this module is that /unlock RESTORES what was there
before, not "reset to neutral": a channel that already carried an explicit
@everyone deny (a staff channel) must not come out of a lock/unlock cycle
readable-writable to the server.

So on lock we snapshot the tri-state (True / False / None=inherit) of every
permission we are about to touch — for @everyone and for the bot's own
member overwrite — and on unlock we put those exact values back.

Pure module: sqlite3 + json only, no discord import, tests run locally.
"""

import json
import os
import sqlite3
import time

DB_PATH = os.environ.get(
    "CHANNEL_LOCKS_DB",
    os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "channel_locks.db")),
)

# What a lock denies. Voice channels get the text set too — they have chat.
TEXT_PERMS = ("send_messages", "send_messages_in_threads",
              "create_public_threads", "create_private_threads")
VOICE_PERMS = ("connect", "speak")


def _conn(db=None):
    c = sqlite3.connect(db or DB_PATH, timeout=30)
    c.execute("""CREATE TABLE IF NOT EXISTS channel_locks (
        guild_id       INTEGER NOT NULL,
        channel_id     INTEGER NOT NULL,
        locked_by      INTEGER,
        locked_by_name TEXT,
        reason         TEXT,
        prev_json      TEXT NOT NULL,
        locked_ts      INTEGER NOT NULL,
        PRIMARY KEY (guild_id, channel_id)
    )""")
    return c


def pack_prev(everyone: dict, me: dict) -> str:
    """Serialize the pre-lock tri-states. Values must be True/False/None."""
    return json.dumps({"everyone": everyone, "me": me})


def unpack_prev(raw: str) -> dict:
    """Inverse of pack_prev. json null round-trips back to None (=inherit)."""
    out = json.loads(raw)
    return {"everyone": out.get("everyone", {}), "me": out.get("me", {})}


def save_lock(guild_id: int, channel_id: int, locked_by: int, locked_by_name: str,
              reason: str, prev_json: str, db=None):
    with _conn(db) as c:
        c.execute(
            "INSERT OR REPLACE INTO channel_locks VALUES (?,?,?,?,?,?,?)",
            (guild_id, channel_id, locked_by, locked_by_name, reason,
             prev_json, int(time.time())))


def get_lock(guild_id: int, channel_id: int, db=None):
    with _conn(db) as c:
        row = c.execute(
            "SELECT locked_by, locked_by_name, reason, prev_json, locked_ts "
            "FROM channel_locks WHERE guild_id=? AND channel_id=?",
            (guild_id, channel_id)).fetchone()
    if row is None:
        return None
    return {"locked_by": row[0], "locked_by_name": row[1], "reason": row[2],
            "prev": unpack_prev(row[3]), "locked_ts": row[4]}


def clear_lock(guild_id: int, channel_id: int, db=None):
    with _conn(db) as c:
        c.execute("DELETE FROM channel_locks WHERE guild_id=? AND channel_id=?",
                  (guild_id, channel_id))


def list_locks(guild_id: int, db=None):
    with _conn(db) as c:
        rows = c.execute(
            "SELECT channel_id, locked_by_name, reason, locked_ts "
            "FROM channel_locks WHERE guild_id=? ORDER BY locked_ts",
            (guild_id,)).fetchall()
    return [{"channel_id": r[0], "locked_by_name": r[1], "reason": r[2], "locked_ts": r[3]}
            for r in rows]
