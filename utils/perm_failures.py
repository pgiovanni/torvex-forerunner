"""Permission failures, recorded once and explained in plain words.

The bot used to fail quietly: a log channel it couldn't post into produced a
16-line traceback in the journal (about 50 a day, 9/22–9/23) and nothing the
server's admin could ever see. Every cog swallowed `Forbidden` in its own way,
so "why isn't X working" had no answer short of reading the VPS journal.

Now there is ONE place a permission failure goes:

  * `report(bot, guild, channel, what, exc)` — async. Records it, logs ONE
    line naming the guild and channel, and (once per channel per day) DMs the
    server's alert contact a sentence they can act on. Use it in any cog's
    `except discord.Forbidden` block.
  * `note(guild, channel, what, exc)` — the sync half (record + log line), for
    places that can't await.
  * `ok(guild_id, channel_id, what)` — call after a SUCCESSFUL send at a site
    that failed before, so the ledger clears itself once the admin fixes it.
  * `audit(guild, cfg)` — every configured channel in this guild with what's
    wrong with it, for `/check-perms`.
  * `recent(guild_id)` — the last day's failures, also for `/check-perms`.

Rows live in the shared config database (security_config.db) so the dashboard
can show the same list without a new file — there are 23 sqlite files already
and the plan is fewer, not more ([[db-consolidation-postgres]]).

Wording rule: say what the bot could not do, in which channel, and which
permission is missing. "Missing Access" on its own is what Discord says; it is
not an explanation.
"""
import json
import logging
import sqlite3
import time

import discord

from utils import security_config

log = logging.getLogger("perms")

# Permissions a channel needs before the bot can post an embed with a file in it.
# Checked in this order so the first missing one is the one that matters most:
# no point saying "can't attach files" in a channel the bot can't even see.
NEEDED = (
    ("view_channel", "View Channel"),
    ("send_messages", "Send Messages"),
    ("embed_links", "Embed Links"),
    ("attach_files", "Attach Files"),
)

# Every config key that names a channel the bot is expected to post into, with
# the words an admin would use for it. Order = how /check-perms lists them.
CONFIGURED = (
    ("msglog_channel_id",              "Message log"),
    ("modlog_channel_id",              "Mod log"),
    ("mod_log_channel_id",             "Moderation actions"),
    ("msglog_media_channel_id",        "Deleted-media re-posts"),
    ("msglog_members_channel_id",      "Joins / leaves log"),
    ("msglog_users_channel_id",        "Name / avatar log"),
    ("msglog_member_roles_channel_id", "Member-roles log"),
    ("msglog_voice_channel_id",        "Voice log"),
    ("msglog_channels_channel_id",     "Channel-changes log"),
    ("msglog_roles_channel_id",        "Role-changes log"),
    ("msglog_expressions_channel_id",  "Emoji / sticker log"),
    ("msglog_automod_channel_id",      "AutoMod log"),
    ("conduct_log_channel_id",         "Conduct log"),
    ("altguard_modlog_channel_id",     "AltGuard reports"),
    ("verify_channel_id",              "Verify panel"),
    ("welcome_channel_id",             "Welcome messages"),
    ("goodbye_channel_id",             "Goodbye messages"),
    ("levels_announce_channel_id",     "Level-up announcements"),
    ("race_channel_id",                "Last to survive"),
)

LOG_EVERY = 600        # one journal line per (guild, channel, what) per 10 min
NOTIFY_EVERY = 86400   # one DM per (guild, channel) per day
KEEP_DAYS = 30         # rows older than this are dropped on the next write

# In-process throttle so a busy voice channel can't turn one broken log
# channel into a write per event. key -> last journal line / last DB write.
_last_log = {}
_last_write = {}


def _conn():
    c = sqlite3.connect(security_config.DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def _init():
    with _conn() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS perm_failures (
                   guild_id    TEXT,
                   channel_id  TEXT,
                   what        TEXT,     -- "post the voice log", "run rule 'x' (react)"
                   code        INTEGER,  -- Discord error code (50001, 50013, 10003) or NULL
                   missing     TEXT,     -- json list of permission names we could see were missing
                   detail      TEXT,     -- the one-sentence explanation, as shown to the admin
                   first_ts    REAL,
                   last_ts     REAL,
                   count       INTEGER,
                   notified_ts REAL,     -- last time the alert contact was DMed about this channel
                   PRIMARY KEY (guild_id, channel_id, what)
               )"""
        )


_init()


# ───────────────────────────────────────────────────────────── pure (testable)
def missing_perms(channel, me):
    """Names of the NEEDED permissions `me` lacks in `channel`, most basic
    first. Empty when the bot has them all — or when `channel` is None, since
    there is nothing to inspect."""
    if channel is None or me is None:
        return []
    try:
        p = channel.permissions_for(me)
    except Exception:
        return []
    if getattr(p, "administrator", False):
        return []
    return [label for attr, label in NEEDED if not getattr(p, attr, True)]


def explain(what, channel, missing, code=None):
    """The sentence an admin sees. `channel` may be a channel object, an id, or
    None; `missing` is from missing_perms(); `code` is Discord's error code."""
    where = _channel_ref(channel)
    if code == 10003 or channel is None or isinstance(channel, (int, str)):
        return (f"Couldn't {what}: the channel {where} no longer exists, or the bot "
                f"can't see it. Pick a channel that exists on the dashboard.")
    if missing:
        return (f"Couldn't {what} in {where}: the bot is missing "
                f"**{' + '.join(missing)}** there.")
    if code == 50013:
        return (f"Couldn't {what} in {where}: Discord refused (Missing Permissions). "
                f"Check the channel's permission overrides for the bot's role.")
    return (f"Couldn't {what} in {where}: Discord refused (Missing Access). The channel "
            f"is probably private to roles the bot doesn't have — give the bot's role "
            f"View Channel + Send Messages there, or pick another channel.")


def _channel_ref(channel):
    if channel is None:
        return "(no channel)"
    if isinstance(channel, (int, str)):
        return f"`{channel}`"
    return getattr(channel, "mention", None) or f"#{getattr(channel, 'name', channel)}"


def _channel_id(channel):
    if channel is None:
        return "0"
    if isinstance(channel, (int, str)):
        return str(channel)
    return str(getattr(channel, "id", 0))


def _code(exc):
    return getattr(exc, "code", None) if exc is not None else None


# ──────────────────────────────────────────────────────────────────── record
def note(guild, channel, what, exc=None):
    """Record the failure + one journal line (throttled). Returns the
    explanation. Never raises — a broken ledger must not break the cog."""
    gid, cid = str(getattr(guild, "id", guild)), _channel_id(channel)
    me = getattr(guild, "me", None)
    missing = missing_perms(channel if not isinstance(channel, (int, str)) else None, me)
    code = _code(exc)
    detail = explain(what, channel, missing, code)
    key = (gid, cid, what)
    now = time.time()
    if now - _last_log.get(key, 0) >= LOG_EVERY:
        _last_log[key] = now
        log.warning("guild %s (%s): %s", gid, getattr(guild, "name", "?"), detail)
    try:
        _write(gid, cid, what, code, missing, detail, now)
    except Exception as e:                       # never let bookkeeping raise
        log.debug("perm_failures: write failed: %s", e)
    return detail


def _write(gid, cid, what, code, missing, detail, now):
    with _conn() as c:
        c.execute(
            """INSERT INTO perm_failures
                   (guild_id, channel_id, what, code, missing, detail, first_ts, last_ts, count)
               VALUES (?,?,?,?,?,?,?,?,1)
               ON CONFLICT(guild_id, channel_id, what) DO UPDATE SET
                   code=excluded.code, missing=excluded.missing, detail=excluded.detail,
                   last_ts=excluded.last_ts, count=perm_failures.count+1""",
            (gid, cid, what, code, json.dumps(missing), detail, now, now))
        key = (gid, cid, what)
        if now - _last_write.get(key, 0) > 3600:  # housekeeping, not per event
            _last_write[key] = now
            c.execute("DELETE FROM perm_failures WHERE last_ts < ?", (now - KEEP_DAYS * 86400,))


def ok(guild_id, channel_id, what):
    """A send that failed before just worked: forget it. Cheap when there was
    nothing to forget (the throttle dict says whether we ever recorded it)."""
    key = (str(guild_id), str(channel_id), what)
    if key not in _last_log:
        return
    _last_log.pop(key, None)
    try:
        with _conn() as c:
            c.execute("DELETE FROM perm_failures WHERE guild_id=? AND channel_id=? AND what=?", key)
    except Exception:
        pass


def recent(guild_id, hours=24):
    """This guild's failures in the window, newest first."""
    try:
        with _conn() as c:
            rows = c.execute(
                "SELECT * FROM perm_failures WHERE guild_id=? AND last_ts>=? "
                "ORDER BY last_ts DESC", (str(guild_id), time.time() - hours * 3600)).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _claim_notify(gid, cid, now):
    """True once per (guild, channel) per NOTIFY_EVERY. Stamps every row for the
    channel so a second `what` on the same channel doesn't send a second DM."""
    try:
        with _conn() as c:
            row = c.execute(
                "SELECT MAX(notified_ts) AS t FROM perm_failures WHERE guild_id=? AND channel_id=?",
                (gid, cid)).fetchone()
            if row and row["t"] and now - row["t"] < NOTIFY_EVERY:
                return False
            c.execute("UPDATE perm_failures SET notified_ts=? WHERE guild_id=? AND channel_id=?",
                      (now, gid, cid))
        return True
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────── report
def alert_target(guild, cfg):
    """Who hears about it: msglog_alert_ping when it names a USER, else the
    owner. '0' = nobody (the same switch that silences the self-delete alert)."""
    pid = cfg.get("msglog_alert_ping")
    if str(pid) == "0":
        return None
    if pid and guild.get_role(int(pid)) is None:
        return guild.get_member(int(pid))
    return guild.owner


async def report(bot, guild, channel, what, exc=None):
    """note() + a one-a-day DM to the alert contact. Returns the explanation."""
    detail = note(guild, channel, what, exc)
    if guild is None:
        return detail
    gid, cid = str(guild.id), _channel_id(channel)
    if not _claim_notify(gid, cid, time.time()):
        return detail
    try:
        cfg = security_config.get_config(guild.id)
        who = alert_target(guild, cfg)
        if who is None:
            return detail
        await who.send(
            f"⚠️ **{guild.name}** — {detail}\n"
            f"Run `/check-perms` in the server to see everything that's failing and why. "
            f"I'll only message you about this channel once a day.",
            allowed_mentions=discord.AllowedMentions.none())
    except (discord.Forbidden, discord.HTTPException, AttributeError, ValueError):
        pass                                   # closed DMs are normal; the ledger has it
    return detail


# ──────────────────────────────────────────────────────────────────── audit
def audit(guild, cfg):
    """One row per configured channel: (label, key, channel_id, problem-or-None).
    `problem` is the sentence to show; None means the bot can post there."""
    out = []
    me = guild.me
    for key, label in CONFIGURED:
        cid = cfg.get(key)
        if not cid:
            continue
        try:
            channel = guild.get_channel(int(cid))
        except (TypeError, ValueError):
            out.append((label, key, str(cid), f"`{cid}` isn't a channel id — re-pick it on the dashboard."))
            continue
        if channel is None:
            out.append((label, key, str(cid), "channel no longer exists, or the bot can't see it"))
            continue
        missing = missing_perms(channel, me)
        out.append((label, key, str(cid),
                    f"missing **{' + '.join(missing)}**" if missing else None))
    return out
