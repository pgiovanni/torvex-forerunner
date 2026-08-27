"""Per-guild config for the honeypot trap channel — cogs/honeypot.py.

A honeypot is one channel real members are told never to touch (or can't even
see). The bot watches it; the first message OR reaction there from a non-staff
member trips the trap and the configured punishment fires. Raid-bots and
self-bots that spray every channel walk straight into it.

Opt-in per guild and inert until armed: nothing happens until `channel_id` is
set and `enabled` is 1. The punishment is configurable — timeout / kick / ban —
because how hard you hit depends on how untouchable the channel really is.

Auto-delete (`delete_messages`, off by default): when on, the bot also removes
the tripper's messages from the trap channel (and their reaction, on a
reaction trip) so bait never accumulates. Off keeps the evidence in place.

Pure module: sqlite3 only, no discord import, tests run locally.
"""

import contextlib
import os
import sqlite3
import time

DB_PATH = os.environ.get(
    "HONEYPOT_DB",
    os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "honeypot.db")),
)

ACTIONS = ("timeout", "kick", "ban")
MAX_TIMEOUT_MIN = 40320   # Discord's hard 28-day cap on timeouts
DEFAULT_TIMEOUT_MIN = 60

_COLS = ("enabled", "channel_id", "action", "timeout_minutes", "log_channel_id",
         "delete_messages", "updated_ts")


@contextlib.contextmanager
def _conn(db=None):
    """Open, create-if-needed, commit on clean exit, and always close — so no
    file handle is left dangling (which on Windows blocks the test db delete)."""
    c = sqlite3.connect(db or DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE IF NOT EXISTS honeypot_config (
        guild_id        INTEGER PRIMARY KEY,
        enabled         INTEGER NOT NULL DEFAULT 0,
        channel_id      INTEGER,
        action          TEXT NOT NULL DEFAULT 'timeout',
        timeout_minutes INTEGER NOT NULL DEFAULT 60,
        log_channel_id  INTEGER,
        updated_ts      INTEGER
    )""")
    # Additive migration: delete_messages arrived after the table shipped (8/27).
    cols = {r[1] for r in c.execute("PRAGMA table_info(honeypot_config)")}
    if "delete_messages" not in cols:
        c.execute("ALTER TABLE honeypot_config ADD COLUMN delete_messages INTEGER NOT NULL DEFAULT 0")
    try:
        yield c
        c.commit()
    finally:
        c.close()


def get(guild_id, db=None) -> dict:
    with _conn(db) as c:
        row = c.execute("SELECT * FROM honeypot_config WHERE guild_id=?", (int(guild_id),)).fetchone()
    if row is None:
        return {"guild_id": int(guild_id), "enabled": 0, "channel_id": None,
                "action": "timeout", "timeout_minutes": DEFAULT_TIMEOUT_MIN,
                "log_channel_id": None, "delete_messages": 0, "updated_ts": None}
    return dict(row)


def _update(guild_id, db=None, **cols):
    bad = set(cols) - set(_COLS)
    if bad:
        raise ValueError(f"unknown columns: {bad}")
    cols["updated_ts"] = int(time.time())
    keys = list(cols)
    with _conn(db) as c:
        c.execute("INSERT OR IGNORE INTO honeypot_config (guild_id) VALUES (?)", (int(guild_id),))
        c.execute(f"UPDATE honeypot_config SET {', '.join(f'{k}=?' for k in keys)} WHERE guild_id=?",
                  [cols[k] for k in keys] + [int(guild_id)])


def set_channel(guild_id, channel_id, db=None):
    """Point the trap at a channel. Arming it (enabled=1) is the caller's step —
    keep them separate so re-pointing a live trap doesn't silently disarm it."""
    _update(guild_id, db=db, channel_id=int(channel_id))


def set_action(guild_id, action, timeout_minutes=None, db=None):
    action = str(action).lower()
    if action not in ACTIONS:
        raise ValueError(f"action must be one of {ACTIONS}")
    fields = {"action": action}
    if action == "timeout":
        mins = DEFAULT_TIMEOUT_MIN if timeout_minutes is None else int(timeout_minutes)
        fields["timeout_minutes"] = max(1, min(MAX_TIMEOUT_MIN, mins))
    _update(guild_id, db=db, **fields)


def set_log_channel(guild_id, channel_id, db=None):
    _update(guild_id, db=db, log_channel_id=int(channel_id) if channel_id else None)


def set_delete_messages(guild_id, on, db=None):
    """Auto-delete the tripper's messages/reaction in the trap channel."""
    _update(guild_id, db=db, delete_messages=1 if on else 0)


def set_enabled(guild_id, on, db=None):
    _update(guild_id, db=db, enabled=1 if on else 0)


def disable(guild_id, db=None):
    _update(guild_id, db=db, enabled=0)


def is_armed(cfg: dict) -> bool:
    """True when a trip should actually punish: enabled and pointed at a channel."""
    return bool(cfg.get("enabled") and cfg.get("channel_id"))
