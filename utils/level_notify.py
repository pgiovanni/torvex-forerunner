"""Where a level-up announcement goes — the pure half of the decision.

Split out of cogs/economy.py so it can be unit-tested without discord.py
installed (the local venv has no discord; anything importing a cog only runs on
the VPS).

Two independent switches decide whether a member's level-up is announced:

* the MEMBER's own opt-out (`discord_users.levelup_notifs`, /notifications) —
  personal, applies in every server;
* the SERVER's choice, which is what this module resolves: announce where they
  were talking, announce in one fixed channel, DM the member, or say nothing.

The member's opt-out wins over all of them, including DM — someone who turned
their level-ups off should not start getting them in their inbox because an
admin changed a server setting.
"""

MODES = ("here", "channel", "dm", "off")

# What each mode is called on the dashboard and in the slash command, so the two
# surfaces can never drift into describing the same setting differently.
MODE_LABELS = {
    "here": "In the channel they were talking in",
    "channel": "In one specific channel",
    "dm": "Direct message to the member",
    "off": "Don't announce level-ups",
}


def normalise_mode(value) -> str:
    """Any stored/posted value → a mode this module understands.

    Unknown values fall back to "here", which is the behaviour every guild had
    before this setting existed: a corrupt or hand-edited config loses the
    redirect, never the feature.
    """
    if isinstance(value, str) and value.strip().lower() in MODES:
        return value.strip().lower()
    return "here"


def _as_id(value):
    try:
        cid = int(value)
    except (TypeError, ValueError):
        return None
    return cid if cid > 0 else None


def destination(cfg, origin_channel_id):
    """(kind, channel_id) for one level-up.

    kind is "channel" (send there), "dm" (message the member) or "off".

    `origin_channel_id` is where the message that levelled them up was sent.

    Note the deliberate asymmetry: mode "channel" with no usable channel id
    resolves to OFF, not back to the origin channel. An admin who redirected
    announcements did so to keep them out of general chat; silently putting them
    back there when the target channel is deleted would undo the very thing they
    asked for, and a missing announcement is the easier failure to notice and
    fix of the two.
    """
    mode = normalise_mode((cfg or {}).get("levels_announce"))
    if mode == "off":
        return ("off", None)
    if mode == "dm":
        return ("dm", None)
    if mode == "channel":
        cid = _as_id((cfg or {}).get("levels_announce_channel_id"))
        return ("channel", cid) if cid else ("off", None)
    cid = _as_id(origin_channel_id)
    return ("channel", cid) if cid else ("off", None)


def describe(cfg, channel_name=None) -> str:
    """One line describing the current setting, for /server-notifications and
    the dashboard to show back to whoever just changed it."""
    mode = normalise_mode((cfg or {}).get("levels_announce"))
    if mode == "channel":
        cid = _as_id((cfg or {}).get("levels_announce_channel_id"))
        if not cid:
            return "Off — a channel was chosen but it no longer exists"
        return f"In {channel_name or f'<#{cid}>'}"
    return MODE_LABELS[mode]
