"""Per-guild quarantine — apply and lift, in ANY server that has the role set.

AltGuard's own quarantine (cogs/altguard.py) is still wired to the legacy
ALTGUARD_* env vars and therefore only understands the main guild. This module
is the multi-guild version, driven by security_config, so `/quarantine` works
anywhere the suite is set up and every caller shares one implementation of
"strip, remember, restore".

The mechanism is deliberately identical to the gate's: take the roles we're
allowed to take, write them down BEFORE removing them, and hand exactly those
back on release. A quarantine that loses someone's roles is worse than no
quarantine at all — the mistake becomes permanent.
"""
import os

import discord

import quarantine_store as qstore
from utils.security_config import get_config


def _env_int(name, default=0):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# Legacy single-guild wiring. Only consulted for that one guild, and only when
# its config row has no role of its own — so the main server keeps working
# through the migration without a special case at every call site.
_LEGACY_GUILD_ID = _env_int("ALTGUARD_GUILD_ID")
_LEGACY_ROLE_ID = _env_int("ALTGUARD_QUARANTINE_ROLE_ID")
# Mid-gate access role. Never stripped and never restored — it's granted
# alongside a quarantine on purpose, so treating it as one of the member's own
# roles would have us hand it back to someone who is no longer held.
_ALMOST_ROLE_ID = _env_int("ALTGUARD_ALMOST_ROLE_ID")


def role_id_for(guild) -> int:
    """The configured quarantine role id for this guild, or 0."""
    rid = get_config(guild.id).get("quarantine_role_id")
    try:
        rid = int(rid) if rid else 0
    except (TypeError, ValueError):
        rid = 0
    if not rid and _LEGACY_GUILD_ID and guild.id == _LEGACY_GUILD_ID:
        rid = _LEGACY_ROLE_ID
    return rid


def role_for(guild):
    """The quarantine role object, or None if unset/deleted."""
    rid = role_id_for(guild)
    return guild.get_role(rid) if rid else None


def removable_roles(member: discord.Member, qrole=None):
    """Roles we may strip: not @everyone, not the quarantine or almost role, not
    managed (bot/booster/integration — Discord refuses those), and below the
    bot's own top role."""
    me = member.guild.me
    keep_ids = {_ALMOST_ROLE_ID, qrole.id if qrole else 0}
    out = []
    for r in member.roles:
        if r.is_default() or r.id in keep_ids or r.managed:
            continue
        if me and r >= me.top_role:
            continue
        out.append(r)
    return out


def blocked_roles(member: discord.Member, qrole=None):
    """Roles we CANNOT strip and that carry real power. A quarantine that leaves
    someone their admin role isn't a quarantine, and silently reporting success
    would be the dangerous outcome — so callers surface this."""
    me = member.guild.me
    out = []
    for r in member.roles:
        if r.is_default() or r.managed or (qrole and r.id == qrole.id):
            continue
        if me and r >= me.top_role:
            p = r.permissions
            if p.administrator or p.manage_guild or p.manage_roles or p.ban_members:
                out.append(r)
    return out


async def apply(member: discord.Member, reason: str):
    """Strip + store roles, apply the quarantine role.

    Returns (ok, removed_roles, error) — error is a human sentence when ok is
    False, so the caller never has to invent one.
    """
    guild = member.guild
    qrole = role_for(guild)
    if qrole is None:
        return False, [], ("No quarantine role is set up here — run `/security setup` "
                           "(or set one on the dashboard) first.")
    me = guild.me
    if me and qrole >= me.top_role:
        return False, [], (f"{qrole.mention} sits at or above my own top role, so I can't "
                           f"apply it. Move **{me.top_role.name}** above it.")

    already = qrole in member.roles
    removable = removable_roles(member, qrole)
    # Written down BEFORE anything is removed: a crash between the two leaves a
    # recoverable record rather than a member whose roles are simply gone.
    qstore.save(member.id, guild.id, [r.id for r in removable], reason)

    rm = set(removable)
    target = [r for r in member.roles if not r.is_default() and r not in rm]
    if qrole not in target:
        target.append(qrole)
    if already and not removable:
        return True, [], None  # nothing left to do; don't burn an API call
    try:
        await member.edit(roles=target, reason=f"Quarantine: {reason}"[:400])
    except discord.Forbidden:
        return False, removable, ("I need **Manage Roles**, and my role must sit above "
                                  "theirs.")
    except discord.HTTPException as e:
        return False, removable, f"Discord refused the role change ({e.status})."
    return True, removable, None


async def lift(member: discord.Member, reason: str):
    """Remove the quarantine role and restore exactly what we stripped.

    Returns (ok, restored_roles, error). The stored snapshot is only consumed
    when it belongs to THIS guild — see qstore.guild_of.
    """
    guild = member.guild
    qrole = role_for(guild)
    me = guild.me

    owner_gid = qstore.guild_of(member.id)
    stored = qstore.pop(member.id) if owner_gid in (None, guild.id) else []

    restore = []
    for rid in stored:
        r = guild.get_role(rid)
        if r and not r.managed and me and r < me.top_role:
            restore.append(r)

    target = [r for r in member.roles
              if not r.is_default() and r != qrole and r.id != _ALMOST_ROLE_ID]
    for r in restore:
        if r not in target:
            target.append(r)
    try:
        await member.edit(roles=target, reason=f"Quarantine lifted: {reason}"[:400])
    except discord.Forbidden:
        return False, restore, "I need **Manage Roles**, and my role must sit above theirs."
    except discord.HTTPException as e:
        return False, restore, f"Discord refused the role change ({e.status})."
    return True, restore, None


def is_held(member: discord.Member) -> bool:
    """Do they currently wear this guild's quarantine role?"""
    rid = role_id_for(member.guild)
    return bool(rid) and any(r.id == rid for r in member.roles)
