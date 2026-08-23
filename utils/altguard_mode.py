"""Per-guild AltGuard mode resolution — the multi-server ladder.

Design: altguard/docs/MULTI-SERVER-DESIGN.md §4. Replaces the single global
`quarantine_on_join` flag and the `guild.id != GUILD_ID` hard-return that made
every path home-guild-only.

    observe   optional verify link, ZERO role changes, findings to their mod-log
    assist    their verification is the front door; we screen after it and
              quarantine only on a FAIL
    gate      full quarantine-on-join (what the home guild runs)
    off       not configured (the default for a guild that just added the bot)

Two rules this module exists to enforce, both learned the hard way:

1. **The home guild resolves exactly as it did before.** Its role/channel ids
   come from the operator's environment, not from a config row a dashboard
   session could overwrite. An existing deployment must not change behaviour
   because a multi-guild layer appeared underneath it.

2. **A remote guild cannot enter an enforcing mode until the GATE is
   per-guild.** The gate's results poll is still single-guild: a remote server
   in `gate` mode would quarantine members whose verdicts nobody ever reads,
   and hold them forever. So `effective_mode()` degrades enforcing modes to
   `observe` while `ALTGUARD_REMOTE_ENFORCE` is off, and says so. Config is
   accepted and stored — it simply doesn't act yet.
"""
import os

MODES = ("off", "observe", "assist", "gate")
ENFORCING = ("assist", "gate")

# Flipped on when the gate speaks per-guild (per-guild credentials + a results
# poll that routes by guild). Until then remote guilds are observe-only, no
# matter what their config says.
REMOTE_ENFORCE = os.environ.get("ALTGUARD_REMOTE_ENFORCE", "").strip().lower() in ("1", "true", "yes")


def _int(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError, AttributeError):
        return None


def configured_mode(cfg) -> str:
    """What the guild's config asks for, normalized. Legacy rows that only have
    the old boolean `quarantine_on_join` are read as `gate`, so a server
    configured before the ladder existed keeps the behaviour it chose."""
    raw = (cfg.get("altguard_mode") or "").strip().lower()
    if raw in MODES:
        return raw
    if not cfg.get("altguard_enabled"):
        return "off"
    return "gate" if cfg.get("quarantine_on_join") else "observe"


def effective_mode(guild_id, cfg, home_guild_id=None, remote_enforce=None) -> str:
    """The mode that will actually run. Never returns an enforcing mode for a
    remote guild while remote enforcement is disabled — see rule 2 above."""
    mode = configured_mode(cfg)
    if mode == "off":
        return "off"
    if home_guild_id is not None and _int(guild_id) == _int(home_guild_id):
        return mode
    allow = REMOTE_ENFORCE if remote_enforce is None else remote_enforce
    if mode in ENFORCING and not allow:
        return "observe"
    return mode


def is_degraded(guild_id, cfg, home_guild_id=None, remote_enforce=None) -> bool:
    """True when the guild asked for more than it's getting — the dashboard and
    /altguard-gate say so plainly rather than letting an admin believe members
    are being held when they aren't."""
    return (configured_mode(cfg) in ENFORCING
            and effective_mode(guild_id, cfg, home_guild_id, remote_enforce) == "observe")


def acts_on_roles(mode) -> bool:
    """Whether this mode may add or remove roles at all. `observe` must never
    touch a member — that promise is what makes it safe to switch on in a
    stranger's server without asking."""
    return mode in ENFORCING


def holds_on_join(mode) -> bool:
    """Quarantine at the door. `assist` deliberately does NOT: the partner bot
    gates the front door and we screen after their grant, quarantining only on
    a fail."""
    return mode == "gate"


def resolve_ids(guild_id, cfg, home_guild_id, env_ids: dict) -> dict:
    """Role/channel ids for this guild.

    Home guild: the operator's environment, always — a config row must not be
    able to repoint the operator's own quarantine role. Everyone else: their
    own config, with no env fallback (inheriting the home guild's role ids into
    a stranger's server would point at roles that don't exist there, or worse,
    at ids that happen to collide with something real).
    """
    if _int(guild_id) == _int(home_guild_id):
        return dict(env_ids)
    return {
        "quarantine_role_id": _int(cfg.get("quarantine_role_id")),
        "verify_channel_id": _int(cfg.get("verify_channel_id")),
        "modlog_channel_id": _int(cfg.get("altguard_modlog_channel_id")
                                  or cfg.get("modlog_channel_id")),
        "almost_role_id": _int(cfg.get("almost_role_id")),
        "default_role_ids": [i for i in
                             (_int(x) for x in (cfg.get("altguard_default_roles") or []))
                             if i is not None],
    }


def missing_requirements(mode, ids: dict) -> list:
    """Config a mode needs before it can run, as human-readable strings. An
    enforcing mode with no quarantine role would hold nobody while reporting
    success, so it is refused rather than half-run."""
    out = []
    if mode == "off":
        return out
    if acts_on_roles(mode) and not ids.get("quarantine_role_id"):
        out.append("a quarantine role")
    if not ids.get("modlog_channel_id"):
        out.append("a mod-log channel to report to")
    return out


def partner_role_ids(cfg) -> set:
    """Verification roles owned by another bot (carl-bot etc.) that AltGuard
    must never strip — see cogs/altguard.py on_member_update."""
    return {i for i in (_int(x) for x in (cfg.get("partner_roles") or [])) if i is not None}


# ── observe-mode reporting ────────────────────────────────────────────────
# What a guild can learn WITHOUT the verification gate: join-time signals the
# bot already has. This is the honest content of `observe` until the gate
# speaks per-guild — no fingerprint, no DM, no role ever touched. It is also
# the pitch: "watch what we'd have caught for two weeks."

def join_risk_signals(age_days, has_avatar, joins_in_window, window_secs,
                      min_age_days=7, burst_threshold=5) -> list:
    """Human-readable risk notes for one join. Empty list = nothing worth
    saying, and observe mode stays silent rather than posting noise on every
    ordinary member."""
    out = []
    if age_days is not None and age_days < min_age_days:
        out.append(f"account only **{age_days}d** old")
    if has_avatar is False:
        out.append("no profile picture")
    if joins_in_window and joins_in_window >= burst_threshold:
        out.append(f"**{joins_in_window} joins** in {int(window_secs)}s — possible raid")
    return out
