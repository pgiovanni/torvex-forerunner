"""Honeypot — a trap channel that punishes whoever touches it.

Designate one channel real members are told never to post in (or that only the
trap role can even see). The first message OR reaction there from a non-staff
member trips it, and the configured punishment fires — timeout, kick, or ban.
Raid-bots and self-bots that blast every channel walk straight into it.

Per-guild and opt-in (utils/honeypot_store): nothing happens until an admin
points it at a channel with /honeypot set-channel. Staff (anyone with a
moderation permission) and bots are always ignored, so a mod who wanders in
isn't nuked. The punishment defaults to timeout — the safe setting — and is
raised to kick/ban deliberately.

Auto-delete (off by default, /honeypot auto-delete or the dashboard): the
tripper's messages are swept out of the trap channel (and their reaction
removed) alongside the punishment, so bait never accumulates in the channel.

Anti-double-fire: a small in-memory set stops a burst (message + reactions from
the same raider in the same second) from stacking three bans on one person.
"""
import os
import sys
import datetime

import discord
from discord import app_commands
from discord.ext import commands

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from utils import honeypot_store as store  # noqa: E402

# Holding any of these means "trusted enough not to be a raider" — exempt.
STAFF_PERMS = ("administrator", "manage_guild", "manage_channels", "manage_messages",
               "kick_members", "ban_members", "moderate_members", "manage_roles")


def _is_staff(member: discord.Member) -> bool:
    p = member.guild_permissions
    return any(getattr(p, name, False) for name in STAFF_PERMS)


class Honeypot(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # (guild_id, member_id) already handled this run — cleared naturally as
        # the process lives; a trip is terminal for the member so no TTL needed.
        self._handled = set()

    # ─────────────────────────────────────────────────────────────── triggers
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author.bot:
            return
        cfg = store.get(message.guild.id)
        if not store.is_armed(cfg) or message.channel.id != int(cfg["channel_id"]):
            return
        member = message.guild.get_member(message.author.id)
        if member is None or _is_staff(member):
            return
        await self._trip(cfg, member, message.channel, trigger="posted in the honeypot channel",
                         message=message)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.guild_id is None or payload.user_id == self.bot.user.id:
            return
        cfg = store.get(payload.guild_id)
        if not store.is_armed(cfg) or payload.channel_id != int(cfg["channel_id"]):
            return
        guild = self.bot.get_guild(payload.guild_id)
        member = guild.get_member(payload.user_id) if guild else None
        if member is None or member.bot or _is_staff(member):
            return
        channel = guild.get_channel(payload.channel_id)
        await self._trip(cfg, member, channel, trigger="reacted in the honeypot channel",
                         reaction=payload)

    # ──────────────────────────────────────────────────────────── enforcement
    async def _trip(self, cfg, member: discord.Member, channel, trigger: str,
                    message: discord.Message = None, reaction: discord.RawReactionActionEvent = None):
        key = (member.guild.id, member.id)
        sweep = bool(cfg.get("delete_messages"))
        if key in self._handled:
            # Already punished this run (or the punishment failed) — still keep
            # the channel clean if auto-delete is on, but never re-punish.
            if sweep:
                await self._sweep(member, channel, message, reaction)
            return
        self._handled.add(key)
        deleted = await self._sweep(member, channel, message, reaction) if sweep else 0

        guild = member.guild
        action = cfg.get("action", "timeout")
        me = guild.me
        done, failed = None, None

        try:
            if action == "ban":
                if me.guild_permissions.ban_members and (member.top_role < me.top_role):
                    await member.ban(reason=f"Honeypot: {trigger}", delete_message_days=1)
                    done = "🔨 Banned"
                else:
                    failed = "I lack **Ban Members** or my role is below theirs."
            elif action == "kick":
                if me.guild_permissions.kick_members and (member.top_role < me.top_role):
                    await member.kick(reason=f"Honeypot: {trigger}")
                    done = "👢 Kicked"
                else:
                    failed = "I lack **Kick Members** or my role is below theirs."
            else:  # timeout (default)
                if me.guild_permissions.moderate_members:
                    mins = int(cfg.get("timeout_minutes") or store.DEFAULT_TIMEOUT_MIN)
                    until = discord.utils.utcnow() + datetime.timedelta(minutes=mins)
                    await member.timeout(until, reason=f"Honeypot: {trigger}")
                    done = f"⏳ Timed out for {mins} min"
                else:
                    failed = "I lack **Timeout Members**."
        except discord.Forbidden:
            failed = "Discord refused the action (permissions / role hierarchy)."
        except discord.HTTPException:
            failed = "Discord returned an error carrying out the action."

        if done is not None and deleted:
            done += f" · 🧹 deleted {deleted} message{'s' if deleted != 1 else ''}"
        await self._log(cfg, guild, member, channel, trigger, done, failed)

    async def _sweep(self, member: discord.Member, channel, message, reaction) -> int:
        """Auto-delete: the triggering message, the tripper's other recent messages
        in the trap channel, and (on a reaction trip) the reaction itself. Best
        effort — a missing Manage Messages must never block the punishment."""
        if channel is None or not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return 0
        perms = channel.permissions_for(member.guild.me)
        if not perms.manage_messages:
            return 0
        deleted = 0
        try:
            if reaction is not None:
                msg = await channel.fetch_message(reaction.message_id)
                await msg.remove_reaction(reaction.emoji, member)
            if message is not None:
                await message.delete()
                deleted += 1
            if perms.read_message_history:
                skip = message.id if message is not None else None
                purged = await channel.purge(
                    limit=100, check=lambda m: m.author.id == member.id and m.id != skip,
                    reason="Honeypot auto-delete")
                deleted += len(purged)
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            pass
        return deleted

    async def _log(self, cfg, guild, member, channel, trigger, done, failed):
        lc = guild.get_channel(int(cfg["log_channel_id"])) if cfg.get("log_channel_id") else None
        if lc is None:
            return
        ok = done is not None
        e = discord.Embed(
            title="🍯 Honeypot tripped",
            colour=discord.Colour.red() if ok else discord.Colour.orange(),
            timestamp=discord.utils.utcnow())
        e.add_field(name="Member", value=f"{member.mention}\n`{member}` · `{member.id}`", inline=False)
        e.add_field(name="Trigger", value=f"{trigger} ({channel.mention if channel else 'unknown'})",
                    inline=False)
        e.add_field(name="Result", value=done if ok else f"⚠️ **Not actioned** — {failed}", inline=False)
        try:
            await lc.send(embed=e, allowed_mentions=discord.AllowedMentions.none())
        except (discord.Forbidden, discord.HTTPException):
            pass

    # ───────────────────────────────────────────────────────────────── commands
    group = app_commands.Group(
        name="honeypot", description="Trap channel that punishes intruders (Admin only)",
        default_permissions=discord.Permissions(administrator=True))

    @group.command(name="set-channel",
                   description="Point the honeypot at a channel and arm it. Anyone who posts/reacts is punished.")
    @app_commands.checks.has_permissions(administrator=True)
    async def set_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        store.set_channel(interaction.guild.id, channel.id)
        store.set_enabled(interaction.guild.id, True)
        cfg = store.get(interaction.guild.id)
        act = cfg["action"] + (f" ({cfg['timeout_minutes']} min)" if cfg["action"] == "timeout" else "")
        await interaction.response.send_message(
            f"🍯 Honeypot **armed** on {channel.mention}. Non-staff who post or react there get "
            f"**{act}**.\nChange the punishment with `/honeypot punishment`. Make sure no real "
            f"member is ever told to use that channel.", ephemeral=True)

    @group.command(name="punishment", description="What happens when someone trips the honeypot.")
    @app_commands.describe(action="Timeout, kick, or ban.",
                           timeout_minutes="For timeout only: how long (1–40320 min). Default 60.")
    @app_commands.choices(action=[
        app_commands.Choice(name="Timeout — temporary mute", value="timeout"),
        app_commands.Choice(name="Kick — removable, can rejoin", value="kick"),
        app_commands.Choice(name="Ban — permanent", value="ban"),
    ])
    @app_commands.checks.has_permissions(administrator=True)
    async def punishment(self, interaction: discord.Interaction,
                         action: app_commands.Choice[str], timeout_minutes: int = None):
        store.set_action(interaction.guild.id, action.value, timeout_minutes=timeout_minutes)
        cfg = store.get(interaction.guild.id)
        detail = f" ({cfg['timeout_minutes']} min)" if action.value == "timeout" else ""
        await interaction.response.send_message(
            f"Honeypot punishment set to **{action.value}{detail}**.", ephemeral=True)

    @group.command(name="log-channel", description="Where honeypot trips are reported (optional).")
    @app_commands.checks.has_permissions(administrator=True)
    async def log_channel(self, interaction: discord.Interaction, channel: discord.TextChannel = None):
        store.set_log_channel(interaction.guild.id, channel.id if channel else None)
        where = channel.mention if channel else "*none — trips won't be logged*"
        await interaction.response.send_message(f"Honeypot log channel set to {where}.", ephemeral=True)

    @group.command(name="status", description="Show the honeypot settings for this server.")
    @app_commands.checks.has_permissions(administrator=True)
    async def status(self, interaction: discord.Interaction):
        cfg = store.get(interaction.guild.id)
        g = interaction.guild
        armed = store.is_armed(cfg)
        ch = g.get_channel(int(cfg["channel_id"])) if cfg.get("channel_id") else None
        lc = g.get_channel(int(cfg["log_channel_id"])) if cfg.get("log_channel_id") else None
        act = cfg["action"] + (f" ({cfg['timeout_minutes']} min)" if cfg["action"] == "timeout" else "")
        e = discord.Embed(
            title="🍯 Honeypot",
            description="🟢 **Armed**" if armed else "⚪ **Disarmed** — set a channel to arm it.",
            colour=discord.Colour.red() if armed else discord.Colour.light_grey())
        e.add_field(name="Trap channel", value=ch.mention if ch else "*not set*")
        e.add_field(name="Punishment", value=f"**{act}**")
        e.add_field(name="Log channel", value=lc.mention if lc else "*not set*", inline=False)
        e.add_field(name="Auto-delete", value="🧹 **On** — tripper's messages are removed"
                    if cfg.get("delete_messages") else "Off — messages stay as evidence")
        await interaction.response.send_message(embed=e, ephemeral=True)

    @group.command(name="auto-delete",
                   description="Also delete the tripper's messages from the trap channel when it fires.")
    @app_commands.describe(enabled="On: sweep their messages/reaction out of the channel. Off: keep them as evidence.")
    @app_commands.checks.has_permissions(administrator=True)
    async def auto_delete(self, interaction: discord.Interaction, enabled: bool):
        store.set_delete_messages(interaction.guild.id, enabled)
        cfg = store.get(interaction.guild.id)
        ch = interaction.guild.get_channel(int(cfg["channel_id"])) if cfg.get("channel_id") else None
        note = ""
        if enabled and ch is not None and not ch.permissions_for(interaction.guild.me).manage_messages:
            note = f"\n⚠️ I don't have **Manage Messages** in {ch.mention} — nothing will be deleted until I do."
        await interaction.response.send_message(
            f"🧹 Honeypot auto-delete **{'on' if enabled else 'off'}**." + note, ephemeral=True)

    @group.command(name="disarm", description="Turn the honeypot off (settings are kept).")
    @app_commands.checks.has_permissions(administrator=True)
    async def disarm(self, interaction: discord.Interaction):
        store.disable(interaction.guild.id)
        await interaction.response.send_message(
            "🍯 Honeypot **disarmed**. The channel is no longer trapped; your settings are kept.",
            ephemeral=True)

    async def cog_app_command_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            msg = "You need **Administrator** to configure the honeypot."
        else:
            msg = "Something went wrong running that."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Honeypot(bot))
