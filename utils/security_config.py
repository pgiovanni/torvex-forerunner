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

# Relocatable so the web dashboard (separate user/service) can share this file
# without being granted write access to the bot directory. Falls back to the
# in-repo path when the env var is unset.
DB_PATH = os.environ.get("TORVEX_SECURITY_DB") or os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "security_config.db"))

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
    "antinuke_trusted_bots": [],    # bot ids with FULL anti-nuke exemption (this server trusts them)
    "antinuke_watch_bot_roles": 0,  # 1 = bots' role-grant bursts count toward the rate rules again
    "partner_roles": [],            # partner-bot verification roles AltGuard must never re-strip
    "altguard_mode": "",            # "" = legacy (quarantine_on_join) · observe | assist | gate | off
    "altguard_modlog_channel_id": None,  # AltGuard's own reports; falls back to modlog_channel_id
    # per-vector rate overrides on top of the code defaults (antinuke.ACTION_LIMITS):
    # {vector: [count, window_s]}. Set via /antinuke. Missing vector = default.
    "antinuke_limits": {},
    # message-flood: server default [count, window_s] (None = code default FLOOD_RATE)
    "antinuke_flood": None,
    # time-boxed headroom for ONE person doing bulk work (see utils/antinuke_window.py).
    # None = no window. Expiry is read-time, so a stale record here is inert.
    "antinuke_window": None,
    # per-channel message-flood override: {channel_id(str): [count, window_s]}
    "antinuke_channel_flood": {},
    # channels where message-flood is NOT enforced (spam/bot channels — "allowed
    # to be spammed"). mention-bomb / @everyone spam still apply everywhere.
    "antinuke_spam_channels": [],
    # hard lockdown: granting a role carrying Administrator / Manage-Server is
    # instantly reverted + the granter stripped, unless the granter is the guild
    # OWNER or this bot (ignores the general whitelist). 1 = on.
    "antinuke_admin_lockdown": 1,
    # user-installed application guard. These apps live on the PERSON, not the
    # server — invisible in Server Settings → Integrations, no bot_add event —
    # so this listener is the only server-side visibility that exists.
    "appguard_enabled": 0,
    "appguard_action": "log",        # log | delete | timeout | quarantine
    "appguard_timeout_min": 10,
    "appguard_allow_apps": [],       # application ids explicitly permitted here
    # altguard tunables
    "quarantine_on_join": 0,        # forced gate
    "dm_on_join": 1,
    "min_account_age_days": 7,
    "autoban_evasion": 0,
    "spoof_ban_threshold": 60,
    "default_role_ids": [],
    "verify_channel_id": None,
    # Greeting a held member in the verify channel. The panel button lives there
    # either way, so this is purely about whether a public @ping fires on join:
    # "always" | "dm_failed" (only when their DM never landed) | "never".
    # dm_failed is the setting that keeps a closed-DM joiner from being stranded
    # without ever pinging anyone else.
    "verify_ping": "always",
    # ── verification-gate terms of service (utils/gate_terms.py) ─────────────
    # The owner's signature, required before the gate may screen anyone here.
    # NEVER declare these as plugin fields: a settings form must not be able to
    # write a consent record, or "I agree" becomes a checkbox someone else ticks.
    "gate_terms_version": 0,        # 0 = never accepted
    "gate_terms_uid": None,         # who accepted
    "gate_terms_username": None,
    "gate_terms_at": None,          # epoch seconds
    "gate_terms_text": None,        # the exact wording that was agreed to
    # ── verify-prune: what happens to someone who never finishes verifying ────
    # A held member who just sits there forever is a foothold, so there's a
    # clock. Every part of it is a server's own call — including turning the
    # removal off entirely and letting them sit, which is what prune_enabled=0
    # means. Nothing here ever touches a member who doesn't hold the quarantine
    # role, so it can't sweep an existing server.
    "prune_enabled": 0,             # master: off = nobody is ever auto-removed
    "prune_enforce": 0,             # 0 = shadow (name them in the log, act on nobody)
    "prune_hours": 72,              # clock starts when they're HELD, not when they join
    "prune_action": "kick",         # kick (they can rejoin) | ban
    "prune_max_per_cycle": 25,      # removals per sweep — a cap on any single mistake
    "prune_spare_clean": 1,         # honour a clean link-open instead of removing
    "prune_spare_action": "review",  # review (stay held, ask a mod) | release (auto-approve)
    "prune_dm": "",                 # "" = the built-in wording (verify_prune.DM_DEFAULT)
    "prune_seeded": 0,              # legacy PRUNE_* env → config migration marker
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
    "msglog_media_max_gb": 5,        # whole-cache hard cap — oldest evicted beyond this (disk-fill DoS guard)
    # split log — every event class may have its own channel; None = the log channel
    "msglog_members_channel_id": None,       # joins / leaves / kicks / bans / unbans
    "msglog_users_channel_id": None,         # username / nickname / avatar changes
    "msglog_member_roles_channel_id": None,  # roles added to / removed from a member
    "msglog_voice_channel_id": None,         # voice join / leave / move, server mute / deafen
    "msglog_channels_channel_id": None,      # channel create / delete / edit
    "msglog_roles_channel_id": None,         # role create / delete / edit
    "msglog_expressions_channel_id": None,   # emoji + sticker create / delete
    "msglog_automod_channel_id": None,       # AutoMod blocks + rule changes
    "msglog_ignore_channels": [],    # channels never LOGGED (still archived)
    "msglog_members": 1,             # member lifecycle: join (w/ invite used), leave, kick/ban/unban w/ WHO+reason
    "msglog_roles": 1,               # member role add/remove (w/ WHO) + role create/delete/edit; own-bot changes never logged
    "msglog_fastdel_secs": 20,       # self-delete within N seconds = "they knew that was bad"
    "msglog_fastdel_ping": 0,        # ...but DON'T @ the owner for it by default. The embed still
                                     # says "⚡ Deleted Ns after posting", so the signal is kept;
                                     # a ping on every fast delete is just noise on a busy server.
    # ── moderation (/ban /kick /timeout /prune-messages) ──────────────────────
    "mod_enabled": 0,                # master opt-in for the CONFIG below; the
                                     # commands themselves are always available
                                     # (they're gated by Discord permissions)
    "mod_log_channel_id": None,      # where actions post; falls back to msglog_channel_id -> modlog_channel_id
    "mod_dm_on_action": 1,           # DM the member what happened + why before it lands
    "mod_require_reason": 0,         # refuse ban/kick/timeout with no reason given
    "mod_default_timeout_min": 60,   # /timeout default when no duration is passed
    "mod_ban_delete_days": 0,        # delete this many days of the banned user's messages (0-7)
    # ── conduct record (/warn /note /warnings) — utils/conduct.py ─────────────
    # The commands are always available (Discord permissions gate them); these
    # only govern behaviour. Clearing is always a soft delete that keeps who did
    # it and why, so none of these can turn the audit trail off.
    "conduct_dm_on_warn": 1,         # tell the member they were warned, and why
    "conduct_public_warn": 1,        # also ping them in the channel /warn was used in, with the reason
    "conduct_require_reason": 1,     # ON by default: a warning with no reason isn't a record
    "conduct_evidence": 1,           # allow screenshot/file evidence on entries
    "conduct_evidence_max_mb": 25,   # per-file cap
    "conduct_evidence_max_gb": 2,    # per-guild total. Uploads are REFUSED past this
                                     # rather than evicting old files — silently
                                     # deleting evidence is the one failure mode
                                     # a record like this must never have.
    "conduct_log_channel_id": None,  # falls back to mod_log_channel_id -> msglog_channel_id -> modlog_channel_id
    # ── AI chat (/ask + ping-to-chat) — cogs/ai.py ────────────────────────────
    # Enablement is NOT this key: the home community is always on and any
    # other server is on once it holds prepaid credit (ai_credit ledger).
    # These govern how members may SPEND it; set via /ai-config, read per ask.
    "ai_enabled": 0,                 # unused — kept so older dashboard builds don't choke
    "ai_mode": "energy",             # energy = per-member daily allowance · unlimited = straight drawdown of the server's pool
    "ai_daily_energy": 100,          # per-member energy per UTC day in energy mode (10–2000)
    "ai_max_paid_asks": 10,          # HOME ONLY: bucks-paid asks per member per day once energy is gone (0 = no paid tier)
    # ── automation (join roles + welcome/goodbye) ─────────────────────────────
    "auto_enabled": 0,               # master opt-in
    "autorole_ids": [],              # roles granted automatically on join
    "autorole_delay_sec": 0,         # wait before granting (lets a raid filter act first)
    "autorole_skip_pending": 1,      # don't grant until Discord onboarding/rules are done
    "welcome_channel_id": None,      # None = welcome message off
    "welcome_message": "",           # {user} {mention} {server} {count} are substituted
    "goodbye_channel_id": None,
    "goodbye_message": "",
    # ── automation rules (cogs/auto_rules.py) ─────────────────────────────────
    # The dashboard's rule builder writes these. `rules` is a LIST of rule
    # objects, not flat keys — see cogs/auto_rules.py for the shape and for why
    # the engine treats anything it doesn't recognise as False rather than True.
    # Off by default and the master switch is separate from each rule's own, so
    # an admin can stop every rule at once without editing any of them.
    "rules_enabled": 0,
    "rules": [],
    # ── leveling: where level-up announcements go (cogs/economy.py) ───────────
    # "here" = the channel they were talking in (MEE6's behaviour and ours since
    # day one), "channel" = one fixed channel, "dm" = message the member, "off"
    # = announce nothing. Resolved by utils/level_notify.py.
    #
    # This is the SERVER's side of the choice only. A member can always silence
    # their own level-ups with /notifications, and that opt-out wins over every
    # value here — including "dm", so nobody starts receiving DMs because an
    # admin changed a server setting.
    "levels_announce": "here",
    "levels_announce_channel_id": None,
    # one-time marker: the old boolean guild_settings.levelup_notifs (Postgres)
    # has been folded into levels_announce for this guild. Same pattern as
    # prune_seeded — without it the migration would re-run and stomp later edits.
    "levels_seeded": 0,
    # ── activity stats (cogs/stats.py) ────────────────────────────────────────
    # DELIBERATELY ON BY DEFAULT, unlike everything else in this file. This tier
    # stores NUMBERS ONLY — one row per (day, channel, user, count), never a
    # word of anyone's message — and a day nobody counted is gone forever, so
    # defaulting it off would quietly cost every server its history in exchange
    # for privacy it already has. Turning it off is one toggle on the Stats card.
    #
    # Scale, measured rather than guessed (2026-08-05, home guild):
    #   1,325,685 archived messages -> messages.db  1.6 GB   (content)
    #   the same traffic            -> stats.db     1.1 MB   (counts)
    # ~1500x. That ratio is the whole reason these are two separate settings:
    # "all time" for counts is megabytes, "all time" for content is gigabytes.
    "stats_enabled": 1,
    "stats_voice": 1,                # voice-session tracking (also numbers only)
    "stats_ignore_channels": [],     # never counted (staff rooms, bot spam)
    # "forward" = count from now on. "all" = also backfill history, which is
    # what makes "since server start" graphs possible on a server that added
    # the bot last week. Set by the Stats card; the backfill itself is a queued
    # job (utils/stats_jobs.py) so a REST crawl never blocks the gateway.
    "stats_scope": "forward",
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
