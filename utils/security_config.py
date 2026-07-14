"""Per-guild security configuration — the foundation for multi-guild, opt-in
anti-nuke / AltGuard / quarantine-lock.

Replaces the single-guild ALTGUARD_*/ANTINUKE_* env vars with a per-guild store
so the security suite can protect ANY server that opts in (via the dashboard or
/security commands), not just ALTGUARD_GUILD_ID.

Storage: SQLite (security_config.db), same pattern as server_backup/stats — the
bot and the co-located dashboard both read/write this one file. Default for every
guild is OFF: nothing acts until an admin explicitly enables it.

Global (stay in env, NOT per-guild): ALTGUARD_SECRET, ALTGUARD_GATE_URL — gate
infrastructure shared across all guilds.
"""
import os
import json
import time
import sqlite3

DB_PATH = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "security_config.db"))

# Default config — everything OFF / safe. A guild only departs from these once an
# admin explicitly enables a feature.
DEFAULTS = {
    # master per-feature opt-in (the MEE6-card Enable/Active toggles)
    "antinuke_enabled": 0,
    "altguard_enabled": 0,
    "qlock_enabled": 0,
    # shared targets
    "quarantine_role_id": None,
    "modlog_channel_id": None,
    # anti-nuke tunables
    "antinuke_enforce": 0,          # 0 = shadow/alert-only, 1 = act
    "antinuke_timeout_min": 10,
    "antinuke_restore_bans": 1,
    "whitelist": [],                # ids never acted on
    # per-vector rate overrides on top of the code defaults (antinuke.ACTION_LIMITS):
    # {vector: [count, window_s]}. Set via /antinuke. Missing vector = default.
    "antinuke_limits": {},
    # message-flood: server default [count, window_s] (None = code default FLOOD_RATE)
    "antinuke_flood": None,
    # per-channel message-flood override: {channel_id(str): [count, window_s]}
    "antinuke_channel_flood": {},
    # channels where message-flood is NOT enforced (spam/bot channels — "allowed
    # to be spammed"). mention-bomb / @everyone spam still apply everywhere.
    "antinuke_spam_channels": [],
    # hard lockdown: granting a role carrying Administrator / Manage-Server is
    # instantly reverted + the granter stripped, unless the granter is the guild
    # OWNER or this bot (ignores the general whitelist). 1 = on.
    "antinuke_admin_lockdown": 1,
    # altguard tunables
    "quarantine_on_join": 0,        # forced gate
    "dm_on_join": 1,
    "min_account_age_days": 7,
    "autoban_evasion": 0,
    "spoof_ban_threshold": 60,
    "default_role_ids": [],
    "verify_channel_id": None,
    # quarantine-lock: channel ids to leave visible (e.g. a #verify channel)
    "lockdown_exempt": [],
    # link-guard (canary-token / IP-grabber link detection)
    "linkguard_enabled": 0,          # master opt-in
    "linkguard_enforce": 0,          # 0 = shadow/alert-only, 1 = act
    "linkguard_delete": 1,           # delete the offending message (enforce only)
    "linkguard_extra_domains": [],   # per-guild additions to the base hitlist
    "linkguard_allow_domains": [],   # per-guild false-positive escapes (removed from list)
    "linkguard_resolve_ips": 1,      # resolve unknown link hosts + match known tracker origin IPs (DNS only)
    "linkguard_tracker_ips": [],     # extra known-tracker IPs, merged with the auto-learned grabify set
    # response — HIGH severity (real tracker/canary/hidden-embed hit): loud.
    "linkguard_catch_timeout_min": 60,   # timeout the poster on a confirmed catch
    "linkguard_taunt": 1,                # public "we caught you 😈" + laughing gifs
    "linkguard_quarantine": 1,           # also quarantine (strip roles + lock out)
    "linkguard_quarantine_delay_sec": 600,  # ...this long AFTER the timeout (theatrics)
    "linkguard_ping": "here",            # modlog ping: "here" | "everyone" | "none" | <role_id>
    "linkguard_taunt_gifs": [],          # override the default laughing gifs (list of urls)
    "linkguard_taunt_text": "",          # override the default taunt line
    # response — LOW severity (URL-shortener-only hit, may be a legit member): gentle.
    "linkguard_timeout_min": 10,         # short timeout, no quarantine, no public shame
    # message archive + mod-log (msglog) — MEE6/Quark/Carl-bot log replacement
    "msglog_enabled": 0,             # master opt-in (archive + logging)
    "msglog_channel_id": None,       # log channel; falls back to modlog_channel_id
    "msglog_deletes": 1,             # log single deletes (with audit-log WHO attribution)
    "msglog_edits": 1,               # log before/after on edits
    "msglog_bulk": 1,                # log bulk deletes with a transcript file
    "msglog_log_bots": 0,            # also log EDITS by bots/webhooks (their deletes always log)
    "msglog_media": 1,               # cache attachments to disk so deleted media can be re-posted
    "msglog_media_channel_id": None, # route deleted-media re-posts here (e.g. an 18+ staff channel); None = with the log embeds
    "msglog_media_max_mb": 25,       # per-file cache cap
    "msglog_media_days": 30,         # media cache retention (log re-posts persist in Discord)
    "msglog_ignore_channels": [],    # channels never LOGGED (still archived)
    "msglog_members": 1,             # member lifecycle: join (w/ invite used), leave, kick/ban/unban w/ WHO+reason
    "msglog_roles": 1,               # member role add/remove (w/ WHO) + role create/delete/edit; own-bot changes never logged
}


# Small in-memory cache so hot paths (anti-nuke on_message) don't hit SQLite per
# event. TTL is short so a dashboard write (separate process, can't invalidate
# this cache) is reflected within a few seconds.
_CACHE_TTL = 5.0
_cache = {}  # guild_id(str) -> (expiry_ts, cfg dict)


def _conn():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def _init():
    with _conn() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS guild_security (
                   guild_id   TEXT PRIMARY KEY,
                   data       TEXT,     -- json blob of the config dict
                   updated_at REAL
               )"""
        )


_init()


def get_config(guild_id) -> dict:
    """Full config for a guild, with DEFAULTS filled in for any missing keys.
    Cached for _CACHE_TTL seconds; returns a fresh copy each call so callers can
    mutate the result without corrupting the cache."""
    key = str(guild_id)
    now = time.time()
    hit = _cache.get(key)
    if hit and hit[0] > now:
        return dict(hit[1])
    cfg = dict(DEFAULTS)
    with _conn() as c:
        row = c.execute("SELECT data FROM guild_security WHERE guild_id=?", (key,)).fetchone()
    if row and row["data"]:
        try:
            cfg.update(json.loads(row["data"]))
        except (ValueError, TypeError):
            pass
    _cache[key] = (now + _CACHE_TTL, cfg)
    return dict(cfg)


def set_config(guild_id, **fields) -> dict:
    """Merge fields into a guild's config and persist. Returns the new config."""
    cfg = get_config(guild_id)
    cfg.update(fields)
    with _conn() as c:
        c.execute(
            "INSERT INTO guild_security(guild_id, data, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(guild_id) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
            (str(guild_id), json.dumps(cfg), time.time()),
        )
    _cache.pop(str(guild_id), None)  # invalidate so the next read is fresh
    return cfg


def is_enabled(guild_id, feature) -> bool:
    """feature in {'antinuke', 'altguard', 'qlock'}. The opt-in gate each cog checks."""
    return bool(get_config(guild_id).get(f"{feature}_enabled"))


def all_enabled(feature):
    """guild_ids that have `feature` enabled — for cogs that sweep all guilds."""
    out = []
    with _conn() as c:
        rows = c.execute("SELECT guild_id, data FROM guild_security").fetchall()
    for r in rows:
        try:
            if json.loads(r["data"]).get(f"{feature}_enabled"):
                out.append(int(r["guild_id"]))
        except (ValueError, TypeError):
            pass
    return out


def seed_from_env(guild_id) -> bool:
    """One-time migration: seed a guild's config from the legacy ALTGUARD_*/ANTINUKE_*
    env vars so the original main server keeps its CURRENT protection through the
    refactor (no protection gap). Only writes if the guild has no row yet; returns
    True if it seeded."""
    with _conn() as c:
        exists = c.execute("SELECT 1 FROM guild_security WHERE guild_id=?", (str(guild_id),)).fetchone()
    if exists:
        return False

    def _int(name, default=0):
        try:
            return int(os.environ.get(name, default))
        except (TypeError, ValueError):
            return default

    def _ids(name):
        return [int(x) for x in os.environ.get(name, "").replace(",", " ").split() if x.strip().isdigit()]

    set_config(
        guild_id,
        antinuke_enabled=1,        # the main guild was actively protected pre-refactor
        altguard_enabled=1,
        qlock_enabled=1,
        quarantine_role_id=_int("ALTGUARD_QUARANTINE_ROLE_ID") or None,
        modlog_channel_id=_int("ALTGUARD_MODLOG_CHANNEL_ID") or None,
        antinuke_enforce=1 if os.environ.get("ANTINUKE_ENFORCE", "0") != "0" else 0,
        antinuke_timeout_min=_int("ANTINUKE_TIMEOUT_MIN", 10),
        antinuke_restore_bans=1 if os.environ.get("ANTINUKE_RESTORE_BANS", "1") != "0" else 0,
        whitelist=_ids("ANTINUKE_WHITELIST"),
        quarantine_on_join=1 if os.environ.get("ALTGUARD_QUARANTINE_ON_JOIN", "0") != "0" else 0,
        dm_on_join=1 if os.environ.get("ALTGUARD_DM_ON_JOIN", "1") != "0" else 0,
        min_account_age_days=_int("ALTGUARD_MIN_ACCOUNT_AGE_DAYS", 7),
        autoban_evasion=1 if os.environ.get("ALTGUARD_AUTOBAN_EVASION", "0") != "0" else 0,
        spoof_ban_threshold=_int("ALTGUARD_SPOOF_BAN", 60),
        default_role_ids=_ids("ALTGUARD_DEFAULT_ROLES"),
        verify_channel_id=_int("ALTGUARD_VERIFY_CHANNEL_ID") or None,
    )
    return True
