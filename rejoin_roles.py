"""Returning-member role restore.

When a member leaves, server_backup logs their role list (member_events, and the
periodic roster snapshot). When they rejoin we want their EXPERIENCE back —
self-assigned reaction roles, their age band, their Level N+ roles (the XP itself
never left; it lives in guild_xp) — but NEVER anything that carries power.

This module is the read + safety-filter half. AltGuard calls it from the
verify-pass release path (so a restore can never bypass the gate) and from the
detect-only join path.

Safety model: a role is restorable ONLY if it is not @everyone, not managed
(bot/booster/integration), below the bot's top role, carries NO dangerous
permission, and is not in the caller's explicit deny set (for the handful of
permissionless-but-sensitive roles like an "18+ Staff" access role or the
quarantine role). Staff/admin roles are excluded twice over — by permissions and
by the deny set.
"""
import os
import json
import sqlite3

# server_backup owns this DB; we only read it.
DB_PATH = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "server_backup.db"))

# Any of these permission bits makes a role unsafe to auto-restore.
DANGEROUS = (
    (1 << 3)   # administrator
    | (1 << 1) | (1 << 2)            # kick, ban
    | (1 << 4) | (1 << 5)            # manage channels, manage guild
    | (1 << 7)                       # view audit log
    | (1 << 8)                       # priority speaker
    | (1 << 13)                      # manage messages
    | (1 << 17)                      # mention everyone
    | (1 << 19)                      # view guild insights
    | (1 << 22) | (1 << 23) | (1 << 24)  # mute / deafen / move members
    | (1 << 27) | (1 << 28) | (1 << 29) | (1 << 30)  # manage nicks/roles/webhooks/expressions
    | (1 << 33) | (1 << 34)          # manage events, manage threads
    | (1 << 40)                      # moderate members (timeout)
)


def _from_events(uid, kinds=("leave", "kick")):
    marks = ",".join("?" * len(kinds))
    try:
        with sqlite3.connect(DB_PATH, timeout=30) as c:
            c.row_factory = sqlite3.Row
            row = c.execute(
                "SELECT roles FROM member_events WHERE uid=? AND kind IN (%s) "
                "ORDER BY ts DESC LIMIT 1" % marks, (str(uid), *kinds)).fetchone()
    except sqlite3.Error:
        return []
    return _parse(row)


def _from_roster(uid):
    try:
        with sqlite3.connect(DB_PATH, timeout=30) as c:
            c.row_factory = sqlite3.Row
            row = c.execute("SELECT roles FROM roster WHERE uid=?", (str(uid),)).fetchone()
    except sqlite3.Error:
        return []
    return _parse(row)


def _parse(row):
    if not row or not row["roles"]:
        return []
    try:
        return [int(x) for x in json.loads(row["roles"])]
    except (ValueError, TypeError):
        return []


def last_known_role_ids(uid):
    """Role ids the member held when they last left/were kicked; falls back to
    the most recent roster snapshot. [] if we have no record."""
    return _from_events(uid) or _from_roster(uid)


def is_restorable(perms_value, managed, is_default, rid, deny_ids, above_bot):
    """Pure predicate — the whole safety decision, so it is trivially testable."""
    if is_default or managed or above_bot:
        return False
    if int(rid) in deny_ids:
        return False
    if perms_value & DANGEROUS:
        return False
    return True


def safe_restorable(guild, role_ids, deny_ids, bot_top):
    """Resolve role_ids against `guild` and keep only the ones safe to restore."""
    deny = {int(x) for x in deny_ids if x}
    out = []
    for rid in role_ids:
        r = guild.get_role(int(rid))
        if r is None:
            continue
        above = bot_top is not None and r >= bot_top
        if is_restorable(r.permissions.value, r.managed, r.is_default(), r.id, deny, above):
            out.append(r)
    return out
