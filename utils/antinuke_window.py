"""Maintenance windows — time-boxed anti-nuke headroom for ONE person.

The problem this solves: `member_role` is (5, 15). A mod told to "give the new
role to everyone in the events team" trips it on the fifth member and gets
stripped and quarantined by our own bot. The blunt fixes are both bad — putting
them on the whitelist waives all nine vectors forever, and raising the limit
with /antinuke set-limit weakens the vector for everybody, permanently, because
nobody remembers to put it back.

So: a window. Someone with Manage Server names WHO is doing bulk work and for
HOW LONG, and only that person, only those vectors, only until it expires.

Three deliberate constraints, none of them accidental:

1. **Only additive vectors are raisable.** ELEVATED below is the entire menu and
   it is code, not operator input. channel_delete / role_delete / ban / kick /
   webhook are absent and must stay absent — there is no legitimate bulk task
   that needs to delete three channels in twelve seconds, and the one time this
   server lost history to an admin (2026-08-06, #emote-suggestions-2) it was a
   channel deletion. A window must never be able to widen the hole it was
   opened next to.

2. **The numbers are fixed, not supplied.** The form asks who / how long / why.
   It does not ask "how high", because the answer to that question is always
   "higher than I need" and it gets stored. A caller cannot pass 99999 here.

3. **Expiry is read-time, never a scheduled write.** is_active() compares a
   timestamp. There is no un-set step that a crash, a restart, or a closed
   laptop can skip, so the failure mode of every bug in this file is that the
   window ends EARLY. Cleanup and announcement still happen (the cog's watcher
   does both) but they are cosmetic — the limits are already back.

What a window explicitly does NOT do: touch the admin-grant lockdown. Granting
a role carrying Administrator or Manage-Server stays owner-only, always, and is
checked before any rate limit is consulted. A window is for volume, not for
privilege.

Pure — no discord import — so tests run anywhere.
"""
import time

# The whole menu. Adding a destructive vector here is not a tuning change, it's
# a redesign; read the module docstring first.
ELEVATED = {
    "member_role": (200, 15),   # mass role GRANTS  — the actual use case
    "role_remove": (200, 15),   # mass role REMOVES — "take the old role off everyone"
    "role_create": (25, 12),    # building out a role set in one sitting
}

MIN_MINUTES = 5
DEFAULT_MINUTES = 30
MAX_MINUTES = 120

# How long a closed/expired window stays in config so the watcher can announce
# it before clearing. Nothing reads limits from an expired window.
LINGER_SECONDS = 300


def _now(now=None):
    return time.time() if now is None else now


def open_window(uid, username, minutes, opened_by, opened_by_name,
                reason="", now=None):
    """Build a window record. Duration is clamped, never trusted.

    Returns the dict to store under the `antinuke_window` config key. Storing it
    is the caller's job — this module never touches the config store, so both
    the bot and the dashboard can use it without either owning the write.
    """
    t = _now(now)
    try:
        mins = int(minutes)
    except (TypeError, ValueError):
        mins = DEFAULT_MINUTES
    mins = max(MIN_MINUTES, min(MAX_MINUTES, mins))
    return {
        "uid": str(uid),
        "username": str(username or uid),
        "opened_at": t,
        "expires_at": t + mins * 60,
        "minutes": mins,
        "opened_by": str(opened_by),
        "opened_by_name": str(opened_by_name or opened_by),
        "reason": (reason or "")[:200],
        # the watcher flips these; they exist so an announcement can't be sent
        # twice across a bot restart
        "announced_open": 0,
        "announced_close": 0,
        # set when a human closes it early, so the close notice can say so
        "closed_early_by": None,
    }


def close_window(win, closed_by_name=None, now=None):
    """Expire a window immediately, preserving it for the close announcement."""
    if not win:
        return None
    out = dict(win)
    out["expires_at"] = _now(now)
    out["closed_early_by"] = closed_by_name
    return out


def is_active(win, now=None):
    """True only while the clock says so. The single source of truth."""
    if not isinstance(win, dict):
        return False
    try:
        return _now(now) < float(win.get("expires_at") or 0)
    except (TypeError, ValueError):
        return False


def remaining(win, now=None):
    """Seconds left, floored at 0."""
    if not isinstance(win, dict):
        return 0
    try:
        return max(0, int(float(win.get("expires_at") or 0) - _now(now)))
    except (TypeError, ValueError):
        return 0


def applies_to(win, actor_id, now=None):
    """True when this actor is the one the window was opened for.

    Ids round-trip through JSON as strings and arrive from discord.py as ints,
    so both sides are normalised rather than assumed.
    """
    if not is_active(win, now) or actor_id is None:
        return False
    return str(win.get("uid")) == str(actor_id)


def effective_limits(base, win, actor_id, now=None):
    """Layer the window's elevated limits over `base` for one actor.

    Returns `base` unchanged (not a copy) when nothing applies, so the hot path
    in _record_action allocates nothing on the overwhelming majority of events.
    A vector absent from ELEVATED is untouched even if it somehow appears in the
    stored record — the code list wins over anything on disk.

    A window can only ever LOOSEN. If a guild has already configured a vector
    more permissively than ELEVATED (say member_role at 500/15 via
    /antinuke set-limit), the higher allowed rate is kept — opening a window to
    do bulk work and thereby getting stripped SOONER would be an absurd way to
    lose an afternoon, and it's exactly the kind of thing nobody would test for.
    """
    if not applies_to(win, actor_id, now):
        return base
    out = dict(base)
    for vector, (count, window) in ELEVATED.items():
        if vector not in out:
            continue
        have_count, have_window = out[vector]
        # compare as rates so (200,15) vs (500,60) is decided on throughput,
        # not on the raw count
        if (count / window) > (have_count / have_window):
            out[vector] = (count, window)
    return out


def needs_open_notice(win, now=None):
    return bool(win) and is_active(win, now) and not win.get("announced_open")


def needs_close_notice(win, now=None):
    """An expired window that was announced on the way in deserves one on the
    way out — otherwise the mod-log shows a server being opened and never shut."""
    return bool(win) and not is_active(win, now) and not win.get("announced_close")


def is_reapable(win, now=None):
    """Expired, both notices sent, and lingered long enough to drop."""
    if not win or is_active(win, now):
        return False
    if not win.get("announced_close"):
        return False
    try:
        return _now(now) - float(win.get("expires_at") or 0) > LINGER_SECONDS
    except (TypeError, ValueError):
        return True


def describe(win, now=None):
    """One-line human summary for a mod-log card or the dashboard."""
    if not win:
        return "No maintenance window."
    who = win.get("username") or win.get("uid")
    if is_active(win, now):
        mins = max(1, remaining(win, now) // 60)
        return f"Open for {who} — about {mins} min left."
    if win.get("closed_early_by"):
        return f"Closed early for {who} by {win['closed_early_by']}."
    return f"Expired for {who}."


def vector_summary():
    """What a window actually raises, for the UI. Order is stable."""
    return [(v, ELEVATED[v]) for v in ("member_role", "role_remove", "role_create")]
