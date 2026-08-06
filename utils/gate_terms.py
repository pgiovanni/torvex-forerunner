"""Verification-gate terms of service — the signature a server gives before
AltGuard is allowed to screen anyone there.

This is a heavier consent than the message-archive terms in cogs/mod_log.py.
That one licenses someone else to hold a server's *messages*; this one licenses
collecting **device and network characteristics from individual members** —
people who never agreed to anything and are simply joining a Discord server. So
the acceptance is owner-only, versioned, and the exact text agreed to is stored
alongside it.

Storing the text (not just a version number) is deliberate. Two copies of this
wording exist — one here, one in the dashboard, which by design does not import
bot code — and they will drift. If they drift, a version number alone would
prove nothing about what was actually on screen when someone pressed the button.
The stored copy is the record; the copies in code are just what gets displayed
next time.

Storage lives in security_config so both processes can read it. The keys are
NEVER declared as plugin fields, so no settings form can write them — acceptance
only happens through the paths in this module.
"""
import time

from utils.security_config import get_config, set_config

# Bump when the wording materially changes. An acceptance of an older version
# stops counting, and the server is re-prompted rather than being treated as
# having agreed to something they never read.
TERMS_VERSION = 1

TERMS_TEXT = (
    "🛡️ **AltGuard verification gate — terms (v1)**\n"
    "Switching this on means members of your server are asked to pass a device check run "
    "on the bot operator's infrastructure. They are not the ones agreeing to this — you are, "
    "on their behalf. Please read it properly.\n\n"
    "**Collected from each person who verifies:** browser and device characteristics "
    "(canvas and WebGL rendering, installed font set, screen dimensions, audio stack, "
    "timezone, platform), their IP address and network characteristics, and their Discord "
    "account identity, confirmed by Discord's own OAuth login.\n"
    "**Third parties:** IP addresses are checked against IPQualityScore for proxy, VPN, Tor, "
    "hosting and reputation data. Nothing else leaves the operator's infrastructure.\n"
    "**Shared across servers:** the device record is global, not per-server. One device seen "
    "in several servers using this gate is one record. You will be told that an account "
    "matches a known device; you will **never** be told which accounts, in which servers, or "
    "shown anything about people who are not your members.\n"
    "**What your staff can see:** the verdict and the reason for it. Raw fingerprints, IP "
    "addresses, and the device-match graph are not exposed to server staff. The bot operator "
    "maintains the database and cannot be locked out of it.\n"
    "**How long:** until you revoke. Revoking stops screening in your server immediately. "
    "Device records already collected are retained, because they are shared evidence that "
    "other servers rely on — ask the operator if you need yours removed.\n"
    "**Your side of it:** you tell your members that joining requires this check and what it "
    "collects. You remain responsible for your community and for your local legal obligations. "
    "The operator supplies the tooling, not your policy.\n"
    "**No guarantee:** fingerprinting is defeatable by a determined person, and every signal "
    "here can produce a false match. That is why a failed check **holds an account for review "
    "instead of removing it**. Treat a verdict as a signal, never as proof about a human being."
)


def status(guild_id) -> dict:
    """{'accepted': bool, 'current': bool, 'version': int, 'uid', 'username', 'at', 'text'}

    `accepted` means they agreed to *something*; `current` means it was this
    version. The two differ only after a terms bump, which is exactly when the
    difference matters.
    """
    cfg = get_config(guild_id)
    try:
        v = int(cfg.get("gate_terms_version") or 0)
    except (TypeError, ValueError):
        v = 0
    return {
        "accepted": v > 0,
        "current": v >= TERMS_VERSION,
        "version": v,
        "uid": cfg.get("gate_terms_uid"),
        "username": cfg.get("gate_terms_username"),
        "at": cfg.get("gate_terms_at"),
        "text": cfg.get("gate_terms_text"),
    }


def accepted(guild_id) -> bool:
    """The gate may screen this server. Anything that starts screening members
    must check this — not just the operator's approval list."""
    return status(guild_id)["current"]


def accept(guild_id, uid, username) -> dict:
    """Record acceptance, with the exact wording that was agreed to."""
    set_config(
        guild_id,
        gate_terms_version=TERMS_VERSION,
        gate_terms_uid=str(uid),
        gate_terms_username=str(username),
        gate_terms_at=time.time(),
        gate_terms_text=TERMS_TEXT,
    )
    return status(guild_id)


def revoke(guild_id) -> dict:
    """Withdraw consent and switch the gate off in the same breath.

    Leaving altguard_enabled on with consent withdrawn would keep screening
    members under an agreement that no longer exists, so the two move together.
    The accepted text is kept as a record of what was previously agreed.
    """
    set_config(guild_id, gate_terms_version=0, altguard_enabled=0)
    return status(guild_id)
