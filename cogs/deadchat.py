"""/deadchat — let members wake a quiet server up by pinging the Dead Chat role.

Paul, 2026-09-12: "give people the ability to ping dead chat ping role. it
should be configurable in the dashboard what role is dead chat ping.
/deadchatping command or something with a message option to ask a question."

The home server already hands a permissionless `Dead Chat Ping` role to
everyone on join (Join & Welcome → roles given on join). Until now only staff
could actually ping it. This command lets ANY member do it, with the guard
rails that make a public role-ping safe:

  * the role is chosen on the dashboard (Dead Chat Ping card), never guessed;
  * one server-wide cooldown, whoever ran it last — a 2,300-member ping is
    not something ten people should be able to fire in a minute;
  * optional channel allow-list, so it can be confined to #general;
  * the ONLY thing that pings is that one role: the member's text is posted
    with AllowedMentions restricted to it, so "@everyone" or a pasted user
    mention in the message renders as plain text.

The bot sends the ping itself, so the role need not be mentionable by
members — but it must be mentionable OR the bot must hold Mention Everyone
in that channel, otherwise Discord shows the mention without notifying
anyone. The command refuses (ephemerally, with the fix) rather than post a
ping that pings nobody.

Who may run it (Paul, 2026-09-13: "change it to staff and level 20+ for my
server"): two dashboard fields, both blank = any member.
  * `deadchat_min_level_role_id` — one of the server's level-reward roles.
    The tier is looked up in the LevelRoles mapping and anyone holding that
    tier OR a higher one passes. This matters because the reward-role sweep
    gives a member only their CURRENT tier — a level-50 member holds
    "Level 50+", not "Level 20+" — so a plain role check would lock out the
    most active people. A role that isn't a level tier simply has to be held.
  * `deadchat_ping_roles` — roles that may always ping (staff, boosters…).
  Members who can Manage Messages or Timeout Members always pass.

Config keys (utils/security_config.DEFAULTS, mirrored in the dashboard):
  deadchat_enabled, deadchat_role_id, deadchat_cooldown_min, deadchat_channels,
  deadchat_min_level_role_id, deadchat_ping_roles
"""
import os
import sys
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from utils.security_config import get_config  # noqa: E402

MAX_MESSAGE = 200
COOLDOWN_MIN_DEFAULT = 30
COOLDOWN_MIN_MAX = 1440


def role_id(cfg) -> Optional[int]:
    """The configured role id as an int, or None. Never a bare int(): a blank
    or junk value must read as 'not configured', not raise inside a command."""
    raw = cfg.get("deadchat_role_id")
    try:
        v = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return v or None


def cooldown_minutes(cfg) -> int:
    try:
        v = int(cfg.get("deadchat_cooldown_min") or COOLDOWN_MIN_DEFAULT)
    except (TypeError, ValueError):
        v = COOLDOWN_MIN_DEFAULT
    return max(1, min(COOLDOWN_MIN_MAX, v))


def cooldown_left(last_ts, cooldown_min, now=None) -> float:
    """Seconds until the next ping is allowed; 0 when it's allowed now."""
    if not last_ts:
        return 0.0
    now = time.time() if now is None else now
    return max(0.0, last_ts + cooldown_min * 60 - now)


def allowed_channels(cfg) -> set:
    out = set()
    for x in cfg.get("deadchat_channels") or []:
        try:
            out.add(int(str(x).strip()))
        except (TypeError, ValueError):
            continue
    return out


def channel_allowed(cfg, channel_id, parent_id=None) -> bool:
    """Empty list = anywhere. A thread counts as its parent channel."""
    allow = allowed_channels(cfg)
    if not allow:
        return True
    return int(channel_id) in allow or (parent_id is not None and int(parent_id) in allow)


def _int_or_none(raw) -> Optional[int]:
    try:
        v = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return v or None


def min_level_role(cfg) -> Optional[int]:
    return _int_or_none(cfg.get("deadchat_min_level_role_id"))


def ping_roles(cfg) -> set:
    out = set()
    for x in cfg.get("deadchat_ping_roles") or []:
        v = _int_or_none(x)
        if v:
            out.add(v)
    return out


def restricted(cfg) -> bool:
    """True when the card limits who may ping (either field set)."""
    return min_level_role(cfg) is not None or bool(ping_roles(cfg))


def qualifying_level_roles(min_rid, mapping) -> set:
    """Every level-reward role at or above the tier `min_rid` belongs to.
    `mapping` is {level: role_id} (LevelRoles._mapping). Not a tier → {min_rid}
    so a plain role still works as "must hold this"."""
    tiers = {int(rid): int(lvl) for lvl, rid in (mapping or {}).items()}
    tier = tiers.get(int(min_rid))
    if tier is None:
        return {int(min_rid)}
    return {rid for rid, lvl in tiers.items() if lvl >= tier}


def can_ping(cfg, member_role_ids, is_mod=False, mapping=None) -> bool:
    """Whether this member may run /deadchat. Open server (nothing configured)
    → yes. Otherwise mods, the always-allowed roles, or a level tier at/above
    the minimum."""
    if not restricted(cfg):
        return True
    if is_mod:
        return True
    held = {int(r) for r in member_role_ids}
    if held & ping_roles(cfg):
        return True
    min_rid = min_level_role(cfg)
    if min_rid is not None and held & qualifying_level_roles(min_rid, mapping):
        return True
    return False


def is_mod(perms) -> bool:
    return bool(perms.administrator or perms.manage_guild or perms.manage_messages
                or perms.moderate_members)


def render(user_mention, rid, message) -> str:
    """The public ping. The member's words go in a quote block, one '> ' per
    line, capped — AllowedMentions on send is what keeps them from pinging."""
    head = f"📣 <@&{rid}> — {user_mention} says the chat is dead."
    text = (message or "").strip()
    if not text:
        return head + " Say something!"
    text = text[:MAX_MESSAGE]
    quoted = "\n".join("> " + ln for ln in text.splitlines() if ln.strip()) or "> " + text
    return f"{head}\n{quoted}"


class DeadChat(commands.Cog):
    """Member-triggered Dead Chat pings, dashboard-configured, server-wide cooldown."""

    def __init__(self, bot):
        self.bot = bot
        self._last = {}     # guild_id -> ts of the last ping (in-memory; a restart resets it)

    @app_commands.command(name="deadchat",
                          description="Ping the Dead Chat role to wake the server up — add a question if you like")
    @app_commands.describe(message="What do you want to talk about? (optional)")
    @app_commands.guild_only()
    @app_commands.checks.cooldown(1, 5, key=lambda i: (i.guild_id, i.user.id))
    async def deadchat(self, interaction: discord.Interaction,
                       message: Optional[app_commands.Range[str, 1, MAX_MESSAGE]] = None):
        guild, channel = interaction.guild, interaction.channel
        cfg = get_config(guild.id)
        rid = role_id(cfg)
        if not cfg.get("deadchat_enabled") or rid is None:
            await interaction.response.send_message(
                "Dead Chat pings aren't set up here. A server manager can pick the role on the "
                "dashboard — **Dead Chat Ping** card.", ephemeral=True)
            return
        role = guild.get_role(rid)
        if role is None:
            await interaction.response.send_message(
                "The configured Dead Chat role no longer exists — a server manager needs to pick "
                "another one on the dashboard (**Dead Chat Ping** card).", ephemeral=True)
            return
        if not can_ping(cfg, [r.id for r in interaction.user.roles],
                        is_mod(interaction.user.guild_permissions),
                        await self._level_mapping(guild.id)):
            await interaction.response.send_message(
                self._who_may_text(guild, cfg), ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none())
            return
        parent_id = getattr(channel, "parent_id", None)
        if not channel_allowed(cfg, channel.id, parent_id):
            where = " ".join(f"<#{c}>" for c in sorted(allowed_channels(cfg))) or "the allowed channels"
            await interaction.response.send_message(
                f"Dead Chat pings only work in {where}.", ephemeral=True)
            return
        cd_min = cooldown_minutes(cfg)
        left = cooldown_left(self._last.get(guild.id), cd_min)
        if left > 0:
            last = int(self._last[guild.id])
            await interaction.response.send_message(
                f"Chat was already pinged <t:{last}:R>. Next one <t:{last + cd_min * 60}:R>.",
                ephemeral=True)
            return
        perms = channel.permissions_for(guild.me)
        if not perms.send_messages:
            await interaction.response.send_message(
                "I can't post in this channel, so the ping would go nowhere.", ephemeral=True)
            return
        if not role.mentionable and not perms.mention_everyone:
            await interaction.response.send_message(
                f"{role.mention} isn't mentionable and I don't have **Mention Everyone** here, so "
                "the ping would notify nobody. A server manager can fix either one.",
                ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            return

        self._last[guild.id] = time.time()
        await interaction.response.send_message(
            render(interaction.user.mention, rid, message),
            allowed_mentions=discord.AllowedMentions(everyone=False, users=False, roles=[role]))

    async def _level_mapping(self, guild_id) -> dict:
        """{level: role_id} from the LevelRoles cog; {} when it isn't loaded or
        its pool is down — the check then treats the minimum role as
        'must hold it', never as 'let everyone through'."""
        cog = self.bot.get_cog("LevelRoles")
        if cog is None or getattr(cog, "pool", None) is None:
            return {}
        try:
            return await cog._mapping(guild_id)
        except Exception as e:  # noqa: BLE001 — a DB blip must not crash the command
            print(f"[deadchat] level mapping unavailable for {guild_id}: {e}")
            return {}

    @staticmethod
    def _who_may_text(guild, cfg) -> str:
        parts = []
        min_rid = min_level_role(cfg)
        if min_rid is not None:
            r = guild.get_role(min_rid)
            parts.append(f"**{r.name}** and up" if r else "a high enough level")
        names = [guild.get_role(x).name for x in sorted(ping_roles(cfg)) if guild.get_role(x)]
        if names:
            parts.append(", ".join(f"**{n}**" for n in names))
        who = " or ".join(parts) if parts else "staff"
        return f"Dead Chat pings here are for {who} (staff always can). Keep chatting and you'll get there!"

    @deadchat.error
    async def _deadchat_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.CommandOnCooldown):
            msg = f"Easy — try again in {error.retry_after:.0f}s."
        else:
            msg = "That didn't work. Try again in a moment."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


async def setup(bot):
    await bot.add_cog(DeadChat(bot))
