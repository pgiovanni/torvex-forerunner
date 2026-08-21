"""Per-guild config for the simple (non-AltGuard) verify — cogs/simpleverify.py.

This is the lightweight verify: on join a member gets an **Unverified** role and
sees only the verify channel; a button there swaps Unverified -> Verified. No
fingerprinting, no hosted gate, no device data — just two roles. It exists for
servers that want a one-click human check without running the AltGuard gate.

Opt-in per guild: a guild does nothing until `channel_id` + both role ids are
set and `enabled` is 1, so adding the bot never changes a server on its own.
It is independent of AltGuard's `security_config` (the home guild's gate) — a
guild runs one or the other, never both against the same join.

Pure module: sqlite3 only, no discord import, tests run locally.
"""

import contextlib
import os
import sqlite3
import time

DB_PATH = os.environ.get(
    "SIMPLEVERIFY_DB",
    os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "simpleverify.db")),
)

_COLS = ("enabled", "unverified_role_id", "verified_role_id", "channel_id",
         "panel_message_id", "updated_ts")


@contextlib.contextmanager
def _conn(db=None):
    """Open, create-if-needed, commit on clean exit, and always close — so no
    file handle is left dangling (which on Windows blocks the test db delete)."""
    c = sqlite3.connect(db or DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE IF NOT EXISTS verify_config (
        guild_id           INTEGER PRIMARY KEY,
        enabled            INTEGER NOT NULL DEFAULT 0,
        unverified_role_id INTEGER,
        verified_role_id   INTEGER,
        channel_id         INTEGER,
        panel_message_id   INTEGER,
        updated_ts         INTEGER
    )""")
    try:
        yield c
        c.commit()
    finally:
        c.close()


def get(guild_id, db=None) -> dict:
    """Current config for a guild, always a dict (defaults when no row yet)."""
    with _conn(db) as c:
        row = c.execute("SELECT * FROM verify_config WHERE guild_id=?", (int(guild_id),)).fetchone()
    if row is None:
        return {"guild_id": int(guild_id), "enabled": 0, "unverified_role_id": None,
                "verified_role_id": None, "channel_id": None, "panel_message_id": None,
                "updated_ts": None}
    return dict(row)


def _update(guild_id, db=None, **cols):
    bad = set(cols) - set(_COLS)
    if bad:
        raise ValueError(f"unknown columns: {bad}")
    cols["updated_ts"] = int(time.time())
    keys = list(cols)
    with _conn(db) as c:
        c.execute("INSERT OR IGNORE INTO verify_config (guild_id) VALUES (?)", (int(guild_id),))
        c.execute(f"UPDATE verify_config SET {', '.join(f'{k}=?' for k in keys)} WHERE guild_id=?",
                  [cols[k] for k in keys] + [int(guild_id)])


def set_roles(guild_id, unverified_role_id, verified_role_id, db=None):
    _update(guild_id, db=db,
            unverified_role_id=int(unverified_role_id),
            verified_role_id=int(verified_role_id))


def set_channel(guild_id, channel_id, db=None):
    _update(guild_id, db=db, channel_id=int(channel_id))


def set_panel_message(guild_id, message_id, db=None):
    _update(guild_id, db=db, panel_message_id=int(message_id) if message_id else None)


def set_enabled(guild_id, on, db=None):
    _update(guild_id, db=db, enabled=1 if on else 0)


def disable(guild_id, db=None):
    _update(guild_id, db=db, enabled=0)


def is_ready(cfg: dict) -> bool:
    """True when a guild has everything the join hook + button need to run."""
    return bool(cfg.get("enabled") and cfg.get("unverified_role_id")
                and cfg.get("verified_role_id") and cfg.get("channel_id"))


def is_enabled(guild_id, db=None) -> bool:
    return is_ready(get(guild_id, db=db))
