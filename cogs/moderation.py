import asyncio
import os
import re
import sys
from datetime import timedelta
from typing import Union

import discord
from discord import app_commands
from discord.ext import commands

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from utils.security_config import get_config  # noqa: E402
from utils.quiet_removals import mark, clear  # noqa: E402
from utils import channel_locks  # noqa: E402

# anti-nuke's kick vector is (5, 20); 6s spacing keeps a bulk quiet-kick well
# under it without the operator having to think about it.
QUIET_KICK_SPACING = 6.0


def _mod_cfg(guild_id) -> dict:
    """Per-guild moderation preferences. The COMMANDS are always available (they
    are gated by Discord permissions); this only governs how they behave, and
    only once an admin opts in — otherwise the historical defaults stand."""
    cfg = get_config(guild_id)
    if not cfg.get("mod_enabled"):
        return {"dm": False, "require_reason": False,
                "default_timeout_min": None, "ban_delete_days": None, "log_channel_id": None}
    return {
        "dm": bool(cfg.get("mod_dm_on_action", 1)),
        "require_reason": bool(cfg.get("mod_require_reason", 0)),
        "default_timeout_min": int(cfg.get("mod_default_timeout_min", 60) or 60),
        "ban_delete_days": int(cfg.get("mod_ban_delete_days", 0) or 0),
        # One channel for AltGuard, one for everything moderation (Paul, 8/23):
        # prefer the message-log channel over the security log when unset.
        "log_channel_id": (cfg.get("mod_log_channel_id") or cfg.get("msglog_channel_id")
                           or cfg.get("modlog_channel_id")),
    }


async def _dm_action(user, guild, verb: str, reason: str, duration: str = ""):
    """Tell the member what happened and why, before it lands. Best-effort:
    closed DMs must never block the moderation action itself."""
    try:
        e = discord.Embed(
            title=f"You were {verb} in {guild.name}",
            description=(reason or "No reason provided"),
            color=0xED4245)
        if duration:
            e.add_field(name="Duration", value=duration, inline=True)
        await user.send(embed=e)
        return True
    except (discord.Forbidden, discord.HTTPException, AttributeError):
        return False


async def _mod_log(bot, guild, cfg, embed):
    """Post an action embed to the configured moderation log, if there is one."""
    cid = cfg.get("log_channel_id")
    if not cid:
        return
    ch = guild.get_channel(int(cid))
    if ch is None:
        return
    try:
        await ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    except (discord.Forbidden, discord.HTTPException):
        pass


def _can_act(invoker: discord.Member, target: discord.Member, me: discord.Member, verb: str = "ban"):
    """Hierarchy/sanity gate for acting on an in-server member. Returns an error
    string if the action is NOT allowed, else None."""
    guild = invoker.guild
    if target.id == invoker.id:
        return f"You can't {verb} yourself."
    if target.id == me.id:
        return f"I can't {verb} myself."
    if target.id == guild.owner_id:
        return f"You can't {verb} the server owner."
    # invoker must outrank the target (owner bypasses the role check)
    if invoker.id != guild.owner_id and target.top_role >= invoker.top_role:
        return f"You can't {verb} {target.mention} — their highest role is above or equal to yours."
    # the bot must outrank the target to carry it out
    if target.top_role >= me.top_role:
        return (f"My role isn't high enough to {verb} {target.mention}. "
                "Move my role above theirs in **Server Settings → Roles**.")
    return None


_DURATION_RE = re.compile(r"(\d+)\s*([smhdw])", re.IGNORECASE)
MAX_TIMEOUT = timedelta(days=28)  # Discord's hard cap


def _parse_duration(text: str):
    """'90m', '2h', '1d', '1h30m', or a bare number (minutes) -> timedelta, else None."""
    text = (text or "").strip().lower()
    if text.isdigit():
        return timedelta(minutes=int(text))
    parts = _DURATION_RE.findall(text)
    # reject if there's junk beyond the matched tokens (e.g. "tomorrow")
    if not parts or _DURATION_RE.sub("", text).strip():
        return None
    unit = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks"}
    return sum((timedelta(**{unit[u]: int(n)}) for n, u in parts), timedelta())


# channel types whose overwrites a lock can edit. Threads inherit the parent's
# permissions and have no overwrites of their own.
LOCKABLE = (discord.TextChannel, discord.VoiceChannel, discord.StageChannel, discord.ForumChannel)
LockableChannel = Union[discord.TextChannel, discord.VoiceChannel, discord.StageChannel, discord.ForumChannel]


def _lock_perm_names(channel):
    perms = list(channel_locks.TEXT_PERMS)
    if isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
        perms += list(channel_locks.VOICE_PERMS)
    return perms


def _fmt_duration(delta: timedelta):
    secs = int(delta.total_seconds())
    out = []
    for label, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        n, secs = divmod(secs, size)
        if n:
            out.append(f"{n}{label}")
    return " ".join(out) or "0s"


class Moderation(commands.Cog):
    """Native moderation commands (ban / unban / kick / timeout / prune-messages) — replacing MEE6."""

    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="ban", description="Ban a member, or pre-ban a user by ID (not in the server).")
    @app_commands.describe(
        user="The member/user to ban (pick them here)",
        user_id="...or a raw Discord ID — for someone not in the server",
        reason="Why they're being banned (shown in the audit log)",
        delete_days="Delete their messages from the last N days (0–7, default 0)",
    )
    @app_commands.default_permissions(ban_members=True)
    @app_commands.checks.has_permissions(ban_members=True)
    @app_commands.guild_only()
    async def ban(self, interaction: discord.Interaction,
                  user: discord.User = None, user_id: str = None,
                  reason: str = None, delete_days: app_commands.Range[int, 0, 7] = 0):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        me = guild.me

        # resolve a single target id from either input
        uid = str(user.id) if user else (user_id or "").strip()
        if not uid.isdigit():
            await interaction.followup.send("Give me a `user` or a numeric `user_id`.", ephemeral=True)
            return
        uid = int(uid)

        if not me.guild_permissions.ban_members:
            await interaction.followup.send("❌ I don't have the **Ban Members** permission.", ephemeral=True)
            return

        # if they're in the server, run the hierarchy checks
        member = guild.get_member(uid)
        if member is not None:
            err = _can_act(interaction.user, member, me)
            if err:
                await interaction.followup.send(f"❌ {err}", ephemeral=True)
                return
        else:
            # not a member — still block self/bot/owner edge cases
            if uid == interaction.user.id:
                await interaction.followup.send("You can't ban yourself.", ephemeral=True)
                return
            if uid == me.id:
                await interaction.followup.send("I can't ban myself.", ephemeral=True)
                return
            if uid == guild.owner_id:
                await interaction.followup.send("You can't ban the server owner.", ephemeral=True)
                return

        # resolve a display name for the log/embed
        target = user
        if target is None:
            try:
                target = await self.bot.fetch_user(uid)
            except discord.HTTPException:
                target = None
        name = target.display_name if target else str(uid)

        mcfg = _mod_cfg(guild.id)
        if mcfg["require_reason"] and not (reason or "").strip():
            await interaction.followup.send(
                "❌ This server requires a reason for moderation actions.", ephemeral=True)
            return
        # a configured default only applies when the mod didn't pass one
        if not delete_days and mcfg["ban_delete_days"]:
            delete_days = max(0, min(7, mcfg["ban_delete_days"]))

        audit = f"{interaction.user} ({interaction.user.id})"
        full_reason = (reason or "No reason provided") + f" — by {audit}"
        # DM BEFORE the ban — afterwards we share no server with them and can't
        dmed = False
        if mcfg["dm"] and member is not None:
            dmed = await _dm_action(member, guild, "banned", reason)
        try:
            await guild.ban(discord.Object(id=uid), reason=full_reason,
                            delete_message_seconds=delete_days * 86400)
        except discord.Forbidden:
            await interaction.followup.send(
                "❌ Discord refused that — usually my role is below theirs, or I'm missing Ban Members.",
                ephemeral=True)
            return
        except discord.HTTPException as e:
            await interaction.followup.send(f"❌ Ban failed: {e}", ephemeral=True)
            return

        not_in = "" if member is not None else " *(was not in the server — pre-banned)*"
        embed = discord.Embed(
            title="🔨 Member banned",
            color=0xE03B3B,
            description=f"**{name}** (`{uid}`) has been banned{not_in}.",
        )
        embed.add_field(name="Reason", value=reason or "No reason provided", inline=False)
        if delete_days:
            embed.add_field(name="Messages deleted", value=f"last {delete_days} day(s)", inline=True)
        if mcfg["dm"] and member is not None:
            embed.add_field(name="Notified", value="DM sent" if dmed else "DMs closed", inline=True)
        embed.set_footer(text=f"Banned by {interaction.user.display_name}")
        await interaction.followup.send(embed=embed)  # public confirmation
        await _mod_log(self.bot, guild, mcfg, embed)
        await interaction.followup.send("✅ Done.", ephemeral=True)

    @app_commands.command(name="unban", description="Unban a user by their Discord ID.")
    @app_commands.describe(
        user_id="The banned user's raw Discord ID",
        reason="Why they're being unbanned (audit log)",
    )
    @app_commands.default_permissions(ban_members=True)
    @app_commands.checks.has_permissions(ban_members=True)
    @app_commands.guild_only()
    async def unban(self, interaction: discord.Interaction, user_id: str, reason: str = None):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        uid = (user_id or "").strip()
        if not uid.isdigit():
            await interaction.followup.send("Give me a numeric `user_id`.", ephemeral=True)
            return
        uid = int(uid)

        if not guild.me.guild_permissions.ban_members:
            await interaction.followup.send("❌ I don't have the **Ban Members** permission.", ephemeral=True)
            return

        audit = f"{interaction.user} ({interaction.user.id})"
        full_reason = (reason or "No reason provided") + f" — by {audit}"
        try:
            await guild.unban(discord.Object(id=uid), reason=full_reason)
        except discord.NotFound:
            await interaction.followup.send(f"⚠️ `{uid}` isn't banned (no ban record found).", ephemeral=True)
            return
        except discord.Forbidden:
            await interaction.followup.send("❌ I'm missing the **Ban Members** permission.", ephemeral=True)
            return
        except discord.HTTPException as e:
            await interaction.followup.send(f"❌ Unban failed: {e}", ephemeral=True)
            return

        try:
            target = await self.bot.fetch_user(uid)
            name = str(target)
        except discord.HTTPException:
            name = str(uid)
        embed = discord.Embed(
            title="♻️ User unbanned",
            color=0x3BA55D,
            description=f"**{name}** (`{uid}`) has been unbanned and can rejoin with an invite.",
        )
        embed.add_field(name="Reason", value=reason or "No reason provided", inline=False)
        embed.set_footer(text=f"Unbanned by {interaction.user.display_name}")
        await interaction.followup.send(embed=embed)  # public confirmation
        await interaction.followup.send("✅ Done.", ephemeral=True)

    @app_commands.command(name="kick", description="Kick a member from the server (they can rejoin with an invite).")
    @app_commands.describe(
        member="The member to kick",
        reason="Why they're being kicked (shown in the audit log)",
    )
    @app_commands.default_permissions(kick_members=True)
    @app_commands.checks.has_permissions(kick_members=True)
    @app_commands.guild_only()
    async def kick(self, interaction: discord.Interaction, member: discord.Member, reason: str = None):
        await interaction.response.defer(ephemeral=True)
        me = interaction.guild.me

        if not me.guild_permissions.kick_members:
            await interaction.followup.send("❌ I don't have the **Kick Members** permission.", ephemeral=True)
            return
        err = _can_act(interaction.user, member, me, verb="kick")
        if err:
            await interaction.followup.send(f"❌ {err}", ephemeral=True)
            return

        name = member.display_name
        uid = member.id
        mcfg = _mod_cfg(interaction.guild.id)
        if mcfg["require_reason"] and not (reason or "").strip():
            await interaction.followup.send(
                "❌ This server requires a reason for moderation actions.", ephemeral=True)
            return
        audit = f"{interaction.user} ({interaction.user.id})"
        full_reason = (reason or "No reason provided") + f" — by {audit}"
        dmed = await _dm_action(member, interaction.guild, "kicked", reason) if mcfg["dm"] else False
        try:
            await member.kick(reason=full_reason)
        except discord.Forbidden:
            await interaction.followup.send(
                "❌ Discord refused that — usually my role is below theirs, or I'm missing Kick Members.",
                ephemeral=True)
            return
        except discord.HTTPException as e:
            await interaction.followup.send(f"❌ Kick failed: {e}", ephemeral=True)
            return

        embed = discord.Embed(
            title="👢 Member kicked",
            color=0xE8763B,
            description=f"**{name}** (`{uid}`) has been kicked. They can rejoin with an invite.",
        )
        embed.add_field(name="Reason", value=reason or "No reason provided", inline=False)
        if mcfg["dm"]:
            embed.add_field(name="Notified", value="DM sent" if dmed else "DMs closed", inline=True)
        embed.set_footer(text=f"Kicked by {interaction.user.display_name}")
        await interaction.followup.send(embed=embed)  # public confirmation
        await _mod_log(self.bot, interaction.guild, mcfg, embed)
        await interaction.followup.send("✅ Done.", ephemeral=True)

    @app_commands.command(
        name="quiet-kick",
        description="Kick a member with no goodbye message and no mod-log embed (still recorded).")
    @app_commands.describe(
        member="The member to kick quietly",
        user_ids="Or several at once — user IDs separated by spaces or commas",
        reason="Why (still written to Discord's audit log and the identity ledger)",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def quiet_kick(self, interaction: discord.Interaction,
                         member: discord.Member = None, user_ids: str = None,
                         reason: str = None):
        """For clearing out dormant shells and abandoned alts without the
        channel reading like a purge. Nothing is hidden from the RECORD —
        member_events, the identity ledger and Discord's own audit log all
        still get the kick. What's suppressed is the announcement.

        Bulk kicks are PACED. anti-nuke trips at 5 kicks in 20s, and clearing
        out six shells is not a reason to strip your own roles."""
        await interaction.response.defer(ephemeral=True)
        guild, me = interaction.guild, interaction.guild.me
        if not me.guild_permissions.kick_members:
            await interaction.followup.send("❌ I don't have the **Kick Members** permission.", ephemeral=True)
            return

        targets, unknown = [], []
        if member:
            targets.append(member)
        for raw in re.split(r"[\s,]+", (user_ids or "").strip()):
            if not raw:
                continue
            m = guild.get_member(int(raw)) if raw.isdigit() else None
            (targets.append(m) if m else unknown.append(raw))
        targets = list({t.id: t for t in targets}.values())
        if not targets:
            await interaction.followup.send(
                "❌ Nobody to kick — pass a member or one or more user IDs of people "
                f"currently in the server.{' Not found: ' + ', '.join(unknown) if unknown else ''}",
                ephemeral=True)
            return

        audit = f"{interaction.user} ({interaction.user.id})"
        full_reason = (reason or "Quiet kick") + f" — by {audit}"
        done, failed = [], []
        for i, t in enumerate(targets):
            err = _can_act(interaction.user, t, me, verb="kick")
            if err:
                failed.append(f"{t.display_name} — {err}")
                continue
            if i:
                await asyncio.sleep(QUIET_KICK_SPACING)   # stay under anti-nuke
            mark(t.id)   # mark BEFORE: on_member_remove can fire before kick() returns
            try:
                await t.kick(reason=full_reason)
                done.append(f"{t.display_name} (`{t.id}`)")
            except discord.Forbidden:
                clear(t.id)
                failed.append(f"{t.display_name} — Discord refused (my role may be below theirs)")
            except discord.HTTPException as e:
                clear(t.id)
                failed.append(f"{t.display_name} — {e}")

        lines = []
        if done:
            lines.append(f"🤫 Kicked **{len(done)}** quietly — no goodbye, no mod-log embed:\n"
                         + "\n".join(f"• {d}" for d in done))
            lines.append("_Still recorded in `member_events`, the identity ledger, and Discord's "
                         "audit log. None are banned — they can rejoin._")
        if failed:
            lines.append("⚠️ Skipped:\n" + "\n".join(f"• {f}" for f in failed))
        if unknown:
            lines.append(f"❔ Not in the server: {', '.join(unknown)}")
        await interaction.followup.send("\n\n".join(lines)[:1900], ephemeral=True)

    @app_commands.command(name="timeout", description="Time out a member (mute + no reactions) for a duration.")
    @app_commands.describe(
        member="The member to time out",
        duration="How long — e.g. 30m, 2h, 1d, 1h30m (max 28d). Blank = this server's default.",
        reason="Why (shown in the audit log and the public embed)",
    )
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    @app_commands.guild_only()
    async def timeout(self, interaction: discord.Interaction, member: discord.Member,
                      duration: str = "", reason: str = None):
        await interaction.response.defer(ephemeral=True)
        me = interaction.guild.me

        if not me.guild_permissions.moderate_members:
            await interaction.followup.send("❌ I don't have the **Timeout Members** permission.", ephemeral=True)
            return
        err = _can_act(interaction.user, member, me, verb="time out")
        if err:
            await interaction.followup.send(f"❌ {err}", ephemeral=True)
            return
        if member.guild_permissions.administrator:
            await interaction.followup.send(
                f"❌ {member.mention} is an administrator — Discord doesn't apply timeouts to admins.",
                ephemeral=True)
            return

        mcfg = _mod_cfg(interaction.guild.id)
        if mcfg["require_reason"] and not (reason or "").strip():
            await interaction.followup.send(
                "❌ This server requires a reason for moderation actions.", ephemeral=True)
            return

        delta = _parse_duration(duration)
        # blank duration falls back to the server's configured default
        if delta is None and not (duration or "").strip() and mcfg["default_timeout_min"]:
            delta = timedelta(minutes=mcfg["default_timeout_min"])
        if delta is None or delta < timedelta(seconds=10):
            await interaction.followup.send(
                "❌ I couldn't read that duration. Use things like `30m`, `2h`, `1d`, `1h30m` (min 10s).",
                ephemeral=True)
            return
        if delta > MAX_TIMEOUT:
            delta = MAX_TIMEOUT  # Discord caps at 28 days

        audit = f"{interaction.user} ({interaction.user.id})"
        full_reason = (reason or "No reason provided") + f" — by {audit}"
        dmed = (await _dm_action(member, interaction.guild, "timed out", reason,
                                 _fmt_duration(delta))) if mcfg["dm"] else False
        try:
            await member.timeout(delta, reason=full_reason)
        except discord.Forbidden:
            await interaction.followup.send(
                "❌ Discord refused that — usually my role is below theirs, or I'm missing Timeout Members.",
                ephemeral=True)
            return
        except discord.HTTPException as e:
            await interaction.followup.send(f"❌ Timeout failed: {e}", ephemeral=True)
            return

        until = discord.utils.utcnow() + delta
        embed = discord.Embed(
            title="⏳ Member timed out",
            color=0xE8A33D,
            description=f"{member.mention} (`{member.id}`) is timed out for **{_fmt_duration(delta)}** "
                        f"— expires {discord.utils.format_dt(until, 'R')}.",
        )
        embed.add_field(name="Reason", value=reason or "No reason provided", inline=False)
        if mcfg["dm"]:
            embed.add_field(name="Notified", value="DM sent" if dmed else "DMs closed", inline=True)
        embed.set_footer(text=f"Timed out by {interaction.user.display_name}")
        await interaction.followup.send(embed=embed)  # public confirmation
        await _mod_log(self.bot, interaction.guild, mcfg, embed)
        await interaction.followup.send("✅ Done.", ephemeral=True)

    @app_commands.command(name="untimeout", description="Remove a member's timeout early.")
    @app_commands.describe(member="The timed-out member", reason="Why (audit log)")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    @app_commands.guild_only()
    async def untimeout(self, interaction: discord.Interaction, member: discord.Member, reason: str = None):
        await interaction.response.defer(ephemeral=True)
        me = interaction.guild.me

        if not me.guild_permissions.moderate_members:
            await interaction.followup.send("❌ I don't have the **Timeout Members** permission.", ephemeral=True)
            return
        if not member.is_timed_out():
            await interaction.followup.send(f"⚠️ {member.mention} isn't timed out.", ephemeral=True)
            return
        if member.top_role >= me.top_role:
            await interaction.followup.send(
                f"❌ My role isn't high enough to change {member.mention}'s timeout.", ephemeral=True)
            return

        audit = f"{interaction.user} ({interaction.user.id})"
        try:
            await member.timeout(None, reason=(reason or "No reason provided") + f" — by {audit}")
        except discord.HTTPException as e:
            await interaction.followup.send(f"❌ Couldn't remove the timeout: {e}", ephemeral=True)
            return

        embed = discord.Embed(
            title="🔊 Timeout removed",
            color=0x3BA55D,
            description=f"{member.mention} (`{member.id}`) can talk again.",
        )
        embed.add_field(name="Reason", value=reason or "No reason provided", inline=False)
        embed.set_footer(text=f"Removed by {interaction.user.display_name}")
        await interaction.followup.send(embed=embed)  # public confirmation
        await interaction.followup.send("✅ Done.", ephemeral=True)

    @app_commands.command(name="lock", description="Lock a channel for ALL roles — nobody talks until /unlock (exempt roles to allow).")
    @app_commands.describe(
        channel="The channel to lock (default: this one)",
        reason="Why it's being locked (shown in the channel and the audit log)",
        exempt="A role that can still talk while locked (e.g. staff)",
        exempt2="Another role that can still talk",
        exempt3="Another role that can still talk",
    )
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.guild_only()
    async def lock(self, interaction: discord.Interaction,
                   channel: LockableChannel = None, reason: str = None,
                   exempt: discord.Role = None, exempt2: discord.Role = None,
                   exempt3: discord.Role = None):
        """Denies the talk perms (+ connect/speak on voice) to @everyone AND
        every role/member overwrite in the channel — a role's channel allow
        does not survive the lock. Exempt roles get an explicit allow instead,
        so "staff can still talk" is the mod's choice, not an accident.
        Administrators bypass overwrites entirely and always keep talking.

        Every pre-lock tri-state we touch is snapshotted to channel_locks.db
        so /unlock restores what was actually there, not "neutral": a channel
        that already carried a deny must not come out of the cycle open. The
        snapshot is saved BEFORE applying, so a half-applied lock is always
        rolled back by /unlock."""
        await interaction.response.defer(ephemeral=True)
        guild, me = interaction.guild, interaction.guild.me
        channel = channel or interaction.channel

        if not isinstance(channel, LOCKABLE):
            msg = ("❌ Threads can't be locked directly — lock the parent channel instead."
                   if isinstance(channel, discord.Thread)
                   else "❌ I can't lock this channel type.")
            await interaction.followup.send(msg, ephemeral=True)
            return
        if not channel.permissions_for(me).manage_roles:
            await interaction.followup.send(
                f"❌ I need the **Manage Permissions** ability in {channel.mention} to lock it.",
                ephemeral=True)
            return
        if channel_locks.get_lock(guild.id, channel.id):
            await interaction.followup.send(
                f"⚠️ {channel.mention} is already locked. Use `/unlock` first.", ephemeral=True)
            return

        exempts = {r.id: r for r in (exempt, exempt2, exempt3)
                   if r is not None and r != guild.default_role}
        perms = _lock_perm_names(channel)

        # the bot's own allow, so the lock can never silence the announcement
        # below (member overwrites beat role denies, so this always wins)
        ow_m = channel.overwrites_for(me)
        me_prev = {"send_messages": ow_m.send_messages}
        ow_m.send_messages = True

        targets = {}                      # snapshot: key -> {perm: tri-state}
        work = []                         # [(discord target, new overwrite)]

        ow_e = channel.overwrites_for(guild.default_role)
        targets["everyone"] = {p: getattr(ow_e, p) for p in perms}
        for p in perms:
            setattr(ow_e, p, False)

        for tgt, ow in channel.overwrites.items():
            if isinstance(tgt, discord.Role):
                if tgt == guild.default_role or tgt.id in exempts:
                    continue
                key = f"role:{tgt.id}"
            elif isinstance(tgt, discord.Member):
                if tgt.id == me.id:
                    continue
                key = f"member:{tgt.id}"
            else:
                continue   # overwrite for a deleted role/member — leave it be
            snap = {p: getattr(ow, p) for p in perms}
            if all(v is False for v in snap.values()):
                continue   # already fully denied — nothing to change or restore
            targets[key] = snap
            for p in perms:
                setattr(ow, p, False)
            work.append((tgt, ow))

        for r in exempts.values():
            ow = channel.overwrites_for(r)
            targets[f"role:{r.id}"] = {p: getattr(ow, p) for p in perms}
            for p in perms:
                setattr(ow, p, True)
            work.append((r, ow))

        # saved BEFORE applying: a failure partway through leaves a row that
        # /unlock can use to roll everything back to the snapshot
        channel_locks.save_lock(guild.id, channel.id, interaction.user.id,
                                str(interaction.user), reason,
                                channel_locks.pack_prev(targets, me_prev))

        audit = f"/lock by {interaction.user} ({interaction.user.id}): {reason or 'No reason provided'}"
        try:
            await channel.set_permissions(me, overwrite=ow_m, reason=audit)
            await channel.set_permissions(guild.default_role, overwrite=ow_e, reason=audit)
            for tgt, ow in work:
                await channel.set_permissions(
                    tgt, overwrite=None if ow.is_empty() else ow, reason=audit)
        except (discord.Forbidden, discord.HTTPException) as e:
            what = ("Discord refused — I need **Manage Permissions** on that channel."
                    if isinstance(e, discord.Forbidden) else f"Lock failed: {e}")
            await interaction.followup.send(
                f"❌ {what}\nThe lock may be partly applied — run `/unlock` to roll it back.",
                ephemeral=True)
            return

        embed = discord.Embed(
            title="🔒 Channel locked",
            color=0xE8A33D,
            description=f"{channel.mention} is locked — nobody can talk here until a mod runs `/unlock`.",
        )
        embed.add_field(name="Reason", value=reason or "No reason provided", inline=False)
        if exempts:
            embed.add_field(name="Can still talk",
                            value=" ".join(r.mention for r in exempts.values()), inline=False)
        embed.set_footer(text=f"Locked by {interaction.user.display_name}")
        if hasattr(channel, "send"):   # forum channels have no .send
            try:
                await channel.send(embed=embed,
                                   allowed_mentions=discord.AllowedMentions.none())
            except (discord.Forbidden, discord.HTTPException):
                pass
        await _mod_log(self.bot, guild, _mod_cfg(guild.id), embed)
        n_denied = len(work) - len(exempts)
        await interaction.followup.send(
            f"🔒 Locked {channel.mention} — @everyone plus **{n_denied}** role/member "
            f"override{'' if n_denied == 1 else 's'} denied"
            + (f", **{len(exempts)}** exempt." if exempts else "."),
            ephemeral=True)

    @app_commands.command(name="unlock", description="Unlock a locked channel and restore its previous permissions.")
    @app_commands.describe(
        channel="The channel to unlock (default: this one)",
        reason="Why (audit log)",
    )
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.guild_only()
    async def unlock(self, interaction: discord.Interaction,
                     channel: LockableChannel = None, reason: str = None):
        await interaction.response.defer(ephemeral=True)
        guild, me = interaction.guild, interaction.guild.me
        channel = channel or interaction.channel

        if not isinstance(channel, LOCKABLE):
            await interaction.followup.send("❌ I can't unlock this channel type.", ephemeral=True)
            return
        if not channel.permissions_for(me).manage_roles:
            await interaction.followup.send(
                f"❌ I need the **Manage Permissions** ability in {channel.mention} to unlock it.",
                ephemeral=True)
            return

        row = channel_locks.get_lock(guild.id, channel.id)
        perms = _lock_perm_names(channel)
        note = ""
        audit = f"/unlock by {interaction.user} ({interaction.user.id}): {reason or 'No reason provided'}"

        # (target, {perm: tri-state}) pairs to restore
        restores = []
        if row:
            for key, snap in row["prev"]["targets"].items():
                if key == "everyone":
                    tgt = guild.default_role
                elif key.startswith("role:"):
                    tgt = guild.get_role(int(key.split(":", 1)[1]))
                elif key.startswith("member:"):
                    tgt = guild.get_member(int(key.split(":", 1)[1]))
                else:
                    tgt = None
                if tgt is not None:   # a deleted role/member's overwrite died with it
                    restores.append((tgt, snap))
            restores.append((me, row["prev"]["me"]))
        else:
            # locked by hand, or before the state store existed — neutral reset
            restores.append((guild.default_role, {p: None for p in perms}))
            restores.append((me, {"send_messages": None}))
            note = "\n*(No saved lock state for this channel — reset the lock permissions to neutral.)*"

        done, failed = 0, []
        for tgt, snap in restores:
            ow = channel.overwrites_for(tgt)
            for p, v in snap.items():
                setattr(ow, p, v)
            try:
                await channel.set_permissions(
                    tgt, overwrite=None if ow.is_empty() else ow, reason=audit)
                done += 1
            except discord.Forbidden:
                failed.append(getattr(tgt, "name", str(tgt)))
                break   # a permission problem won't fix itself target-to-target
            except discord.HTTPException:
                failed.append(getattr(tgt, "name", str(tgt)))

        if failed and not done:
            await interaction.followup.send(
                "❌ Discord refused — I need **Manage Permissions** on that channel. "
                "The lock record was kept; run `/unlock` again once I have it.",
                ephemeral=True)
            return
        if failed:
            note += ("\n⚠️ Couldn't restore: " + ", ".join(failed[:10])
                     + " — check the channel's permission overrides by hand.")

        channel_locks.clear_lock(guild.id, channel.id)

        embed = discord.Embed(
            title="🔓 Channel unlocked",
            color=0x3BA55D,
            description=f"{channel.mention} is open again.{note}",
        )
        if row:
            embed.add_field(
                name="Was locked",
                value=f"<t:{row['locked_ts']}:R> by **{row['locked_by_name']}** — "
                      f"{row['reason'] or 'no reason given'}",
                inline=False)
        if reason:
            embed.add_field(name="Reason", value=reason, inline=False)
        embed.set_footer(text=f"Unlocked by {interaction.user.display_name}")
        if hasattr(channel, "send"):
            try:
                await channel.send(embed=embed)
            except (discord.Forbidden, discord.HTTPException):
                pass
        await _mod_log(self.bot, guild, _mod_cfg(guild.id), embed)
        await interaction.followup.send(f"🔓 Unlocked {channel.mention}.", ephemeral=True)

    @app_commands.command(
        name="prune-messages",
        description="Bulk-delete the last N messages in this channel (count-based, not by date).",
    )
    @app_commands.describe(amount="How many recent messages to delete (1–1000).")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def prune_messages(self, interaction: discord.Interaction,
                             amount: app_commands.Range[int, 1, 1000]):
        await interaction.response.defer(ephemeral=True)
        channel = interaction.channel
        me = interaction.guild.me

        if not hasattr(channel, "purge"):
            await interaction.followup.send(
                "❌ This channel type doesn't support pruning. Run it in a text/voice channel or thread.",
                ephemeral=True)
            return
        if not channel.permissions_for(me).manage_messages:
            await interaction.followup.send(
                "❌ I need the **Manage Messages** permission in this channel.", ephemeral=True)
            return

        try:
            # let the mod-log credit the invoking mod, not the bot — Discord's
            # audit log names the bot and drops reasons on bulk deletes
            from cogs.mod_log import note_bot_purge
            note_bot_purge(channel.id, interaction.user.id, str(interaction.user))
            deleted = await channel.purge(
                limit=amount,
                reason=f"/prune-messages by {interaction.user} ({interaction.user.id})")
        except discord.Forbidden:
            await interaction.followup.send(
                "❌ Discord refused — I'm missing **Manage Messages** here.", ephemeral=True)
            return
        except discord.HTTPException as e:
            await interaction.followup.send(f"❌ Prune failed: {e}", ephemeral=True)
            return

        n = len(deleted)
        note = ("\n*(Discord only bulk-deletes messages newer than 14 days — older ones were skipped.)*"
                if n < amount else "")
        await interaction.followup.send(
            f"🧹 Deleted **{n}** message{'' if n == 1 else 's'} in {channel.mention}.{note}",
            ephemeral=True)

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            perms = ", ".join(p.replace("_", " ").title() for p in error.missing_permissions) or "required"
            msg = f"❌ You need the **{perms}** permission to use this."
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Moderation(bot))
