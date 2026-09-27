"""Operator blocklist of servers the bot refuses to stay in.

Why this exists (2026-09-27): BlackNova Store, an 889-member cheat + stolen
account shop, added the bot with kick/ban/role powers, and its ticket channels
were handing over `email:password:token` bundles straight into the message
archive. The public rule is on torvex.app/TrustSafety ("Servers we don't
serve"); this module is the mechanism behind it.

Two places enforce it, both in bot.py:
  * `on_guild_join` — a blocked server that re-adds the bot is left again
    within the same event, before any cog does work for it.
  * `on_ready` — a sweep over every current guild, so blocking a server the
    bot is already in (or restarting after a block was added by hand) also
    leaves it.
The `/blocklist` command group (cogs/guild_blocklist.py) manages the list and
purges whatever the archive holds for that server.

Storage: one small SQLite file in the shared /var/lib/torvex directory when it
exists (the same rule as utils/security_config.py — a one-off script that
forgets the env must still hit the file the bot reads), otherwise next to the
repo for a dev checkout. Pure module: sqlite3 only, no discord import, so the
tests run locally.
"""

import contextlib
import os
import re
import sqlite3
import time

SHARED_DB = "/var/lib/torvex/guild_blocklist.db"
DB_PATH = os.environ.get("TORVEX_BLOCKLIST_DB") or (
    SHARED_DB if os.path.isdir(os.path.dirname(SHARED_DB)) else os.path.abspath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "guild_blocklist.db")))

MAX_REASON = 300

# Posted into a blocked server the moment it re-adds the bot, right before the
# bot leaves again. One line, no reason, no names: the first exit is silent
# (nothing to argue with), but a re-add means they read the silence as a
# glitch and will keep trying — the notice is what makes them stop. Decided
# 9/27 after BlackNova re-added the bot three times in 26 minutes.
NOTICE_TEXT = ("This server is on Torvex's do-not-serve list, so the bot will not stay here. "
               "https://torvex.app/TrustSafety")


def alert_wanted(prior_blocked_rows: int) -> bool:
    """Email the operator for a refused join?

    `prior_blocked_rows` = how many `blocked` departures the ledger already
    holds for this guild. 0 = the server was pre-blocked and this is the first
    refusal; 1 = it was blocked while the bot was inside and this is the first
    re-add. Both are news. From the second re-add on, the ledger row is the
    record and the inbox stays quiet — a server spamming the invite must not
    be able to spam the operator.
    """
    return prior_blocked_rows <= 1


@contextlib.contextmanager
def _conn(db=None):
    """Open, create-if-needed, commit on clean exit, always close (Windows
    tests can't delete a db with a dangling handle)."""
    path = db or DB_PATH
    fresh = not os.path.exists(path)
    c = sqlite3.connect(path, timeout=30)
    c.row_factory = sqlite3.Row
    if fresh:
        # Shared with the dashboard (group torvexcfg): sqlite creates 0644 under
        # the default umask, which locks the dashboard out of writing it.
        try:
            os.chmod(path, 0o660)
        except OSError:
            pass
    c.execute("""CREATE TABLE IF NOT EXISTS blocked_guilds (
        guild_id   TEXT PRIMARY KEY,
        guild_name TEXT,
        reason     TEXT NOT NULL,
        added_by   TEXT,
        added_ts   REAL NOT NULL
    )""")
    try:
        yield c
        c.commit()
    finally:
        c.close()


def parse_guild_id(text):
    """A guild id typed by a human: digits, optionally pasted with whitespace
    or wrapped in backticks/angle brackets. None when it isn't one."""
    if text is None:
        return None
    s = re.sub(r"[\s`<>]", "", str(text))
    if not s.isdigit() or not 15 <= len(s) <= 22:
        return None
    return int(s)


def _row(r):
    return None if r is None else {
        "guild_id": int(r["guild_id"]),
        "guild_name": r["guild_name"],
        "reason": r["reason"],
        "added_by": r["added_by"],
        "added_ts": float(r["added_ts"]),
    }


def get(guild_id, db=None):
    """The block entry for a guild, or None."""
    with _conn(db) as c:
        return _row(c.execute("SELECT * FROM blocked_guilds WHERE guild_id=?",
                              (str(int(guild_id)),)).fetchone())


def is_blocked(guild_id, db=None):
    return get(guild_id, db=db) is not None


def block(guild_id, reason, added_by=None, guild_name=None, db=None, now=None):
    """Add (or update the reason/name of) a blocked guild. Returns the entry."""
    reason = " ".join(str(reason or "").split())[:MAX_REASON]
    if not reason:
        raise ValueError("a reason is required")
    gid = str(int(guild_id))
    with _conn(db) as c:
        c.execute(
            "INSERT INTO blocked_guilds (guild_id, guild_name, reason, added_by, added_ts)"
            " VALUES (?,?,?,?,?)"
            " ON CONFLICT(guild_id) DO UPDATE SET reason=excluded.reason,"
            "   guild_name=COALESCE(excluded.guild_name, blocked_guilds.guild_name),"
            "   added_by=COALESCE(excluded.added_by, blocked_guilds.added_by)",
            (gid, guild_name, reason, None if added_by is None else str(added_by),
             time.time() if now is None else now))
    return get(gid, db=db)


def unblock(guild_id, db=None):
    """Remove a guild from the list. True when a row was actually removed."""
    with _conn(db) as c:
        return c.execute("DELETE FROM blocked_guilds WHERE guild_id=?",
                         (str(int(guild_id)),)).rowcount > 0


def all_blocked(db=None):
    """Every entry, newest first."""
    with _conn(db) as c:
        return [_row(r) for r in c.execute(
            "SELECT * FROM blocked_guilds ORDER BY added_ts DESC")]
