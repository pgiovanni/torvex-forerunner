"""Simple verify — the non-AltGuard, role-swap human check.

On join a member gets an **Unverified** role; a persistent button in the verify
channel swaps it for **Verified**. No fingerprinting, no hosted gate, no device
data — two roles and a button. It's for servers that want a one-click "not a
drive-by bot" check without running the AltGuard gate.

Everything is per-guild and opt-in (utils/simpleverify_store): a server does
nothing until an admin runs /simpleverify setup-roles (or wires roles + channel
by hand) and it's enabled. Unverified members see only the verify channel:
setup-roles hides every other channel from the role and new channels are hidden
as they are created (a channel given its own overwrite for the role is respected). Independent of AltGuard — a guild runs one or the
other, so this never fires in the home guild where the gate already holds joins.

The button is a persistent view (custom_id "sv:verify"), registered at cog_load
so a click works the instant the bot is up, across restarts.
"""
import asyncio
import os
import sys

import discord
from discord import app_commands
from discord.ext import commands

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from utils import simpleverify_store as store  # noqa: E402

VERIFY_CUSTOM_ID = "sv:verify"


def _bot_can_manage(guild: discord.Guild, role: discord.Role) -> bool:
    """We can only move a role that sits below our own top role, and only with
    Manage Roles. Anything else fails at the API — check first, report clearly."""
    me = guild.me
    return bool(me and me.guild_permissions.manage_roles and role and role < me.top_role)


class VerifyButton(discord.ui.Button):
    def __init__(self):
        super().__init__(style=discord.ButtonStyle.success, label="Verify",
                         emoji="✅", custom_id=VERIFY_CUSTOM_ID)

    async def callback(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            return
        cfg = store.get(guild.id)
        if not store.is_ready(cfg):
            await interaction.response.send_message(
                "Verification isn't set up here yet — an admin needs to finish it.", ephemeral=True)
            return
        member = interaction.user
        verified = guild.get_role(int(cfg["verified_role_id"]))
        unverified = guild.get_role(int(cfg["unverified_role_id"]))
        if verified is None or unverified is None:
            await interaction.response.send_message(
                "The verify roles are missing — tell an admin to run setup again.", ephemeral=True)
            return
        if verified in member.roles:
            await interaction.response.send_message("You're already verified. ✅", ephemeral=True)
            return
        if not _bot_can_manage(guild, verified) or not _bot_can_manage(guild, unverified):
            await interaction.response.send_message(
                "I can't change your roles — my role must sit **above** the verify roles and I need "
                "**Manage Roles**. Tell an admin.", ephemeral=True)
            return
        try:
            await member.add_roles(verified, reason="Simple verify: passed")
            if unverified in member.roles:
                await member.remove_roles(unverified, reason="Simple verify: passed")
        except discord.Forbidden:
            await interaction.response.send_message(
                "Discord refused the role change — an admin needs to check my permissions.", ephemeral=True)
            return
        await interaction.response.send_message("You're verified — welcome in. ✅", ephemeral=True)


def _panel_view() -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    v.add_item(VerifyButton())
    return v


class SimpleVerify(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        # One persistent view serves every guild (the button resolves its guild
        # from the interaction), so register it once — clicks work before the
        # gateway delivers the first one, and survive restarts.
        self.bot.add_view(_panel_view())

    # ───────────────────────────────────────────────────────── auto-Unverified
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot:
            return
        cfg = store.get(member.guild.id)
        if not store.is_ready(cfg):
            return
        role = member.guild.get_role(int(cfg["unverified_role_id"]))
        if role and _bot_can_manage(member.guild, role) and role not in member.roles:
            try:
                await member.add_roles(role, reason="Simple verify: held on join")
            except (discord.Forbidden, discord.HTTPException):
                pass

    # ───────────────────────────────────────────────────────────────── commands
    group = app_commands.Group(
        name="simpleverify", description="Role-swap verify, no AltGuard gate (Admin only)",
        default_permissions=discord.Permissions(administrator=True))

    # ───────────────────────────────────────────────────────── channel lockdown
    async def _lock_unverified(self, guild, role, verify_channel_id):
        """Deny View Channel for the Unverified role on every channel except the
        verify channel — the half of "unverified members see only #verify" that
        the roles alone can't do. A channel that already carries an explicit
        overwrite for the role is left alone, so an admin who deliberately opens
        #rules or #welcome to unverified members keeps that choice.
        Returns (locked, skipped)."""
        locked = skipped = 0
        for ch in guild.channels:
            if verify_channel_id and ch.id == int(verify_channel_id):
                continue
            if role in ch.overwrites:
                skipped += 1
                continue
            try:
                await ch.set_permissions(role, view_channel=False,
                                         reason="Simple verify: unverified members see only the verify channel")
                locked += 1
            except (discord.Forbidden, discord.HTTPException):
                skipped += 1
            await asyncio.sleep(0.3)
        return locked, skipped

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel):
        """A channel created after setup must not leak to unverified members."""
        guild = channel.guild
        cfg = store.get(guild.id)
        if not store.is_ready(cfg):
            return
        if cfg.get("channel_id") and int(cfg["channel_id"]) == channel.id:
            return
        role = guild.get_role(int(cfg["unverified_role_id"]))
        if role is None or role in channel.overwrites or not _bot_can_manage(guild, role):
            return
        try:
            await channel.set_permissions(role, view_channel=False,
                                          reason="Simple verify: unverified members see only the verify channel")
        except (discord.Forbidden, discord.HTTPException):
            pass

    @group.command(name="lockdown",
                   description="Hide every channel except the verify channel from Unverified members.")
    @app_commands.checks.has_permissions(administrator=True)
    async def lockdown(self, interaction: discord.Interaction):
        guild = interaction.guild
        cfg = store.get(guild.id)
        role = guild.get_role(int(cfg["unverified_role_id"])) if cfg.get("unverified_role_id") else None
        if role is None:
            await interaction.response.send_message(
                "No Unverified role yet — run `/simpleverify setup-roles` first.", ephemeral=True)
            return
        if not _bot_can_manage(guild, role):
            await interaction.response.send_message(
                "My role must sit **above** the Unverified role and I need **Manage Roles**.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        locked, skipped = await self._lock_unverified(guild, role, cfg.get("channel_id"))
        await interaction.followup.send(
            f"🔒 Hidden from **Unverified** on **{locked}** channel(s); {skipped} left as they were "
            "(already had an overwrite for the role). New channels are hidden automatically.", ephemeral=True)

    @group.command(name="setup-roles",
                   description="Create/wire the Unverified + Verified roles and turn verify on.")
    @app_commands.describe(verify_channel="Channel members verify in (the button gets posted here).")
    @app_commands.checks.has_permissions(administrator=True)
    async def setup_roles(self, interaction: discord.Interaction,
                          verify_channel: discord.TextChannel = None):
        guild = interaction.guild
        if not guild.me.guild_permissions.manage_roles:
            await interaction.response.send_message(
                "I need **Manage Roles** to create the verify roles.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)

        # Reuse a role that already carries the name (idempotent re-runs), else make it.
        def find(name):
            return discord.utils.find(lambda r: r.name.lower() == name.lower(), guild.roles)

        try:
            unverified = find("Unverified") or await guild.create_role(
                name="Unverified", colour=discord.Colour(0x607d8b), hoist=False, mentionable=False,
                reason="Simple verify setup")
            verified = find("Verified") or await guild.create_role(
                name="Verified", colour=discord.Colour(0x57f287), hoist=False, mentionable=False,
                reason="Simple verify setup")
        except discord.Forbidden:
            await interaction.followup.send("Discord refused role creation — check my permissions.",
                                            ephemeral=True)
            return

        store.set_roles(guild.id, unverified.id, verified.id)
        lines = [f"• Unverified → {unverified.mention}", f"• Verified → {verified.mention}"]

        if verify_channel:
            store.set_channel(guild.id, verify_channel.id)
            try:
                # Unverified can see + talk in the verify channel; verified members
                # don't need it cluttering their list once they're through.
                await verify_channel.set_permissions(
                    unverified, view_channel=True, send_messages=True,
                    reason="Simple verify: verify channel access")
                await verify_channel.set_permissions(
                    verified, view_channel=False, reason="Simple verify: hide once verified")
                lines.append(f"• Verify channel → {verify_channel.mention} (permissions set)")
            except discord.Forbidden:
                lines.append(f"• Verify channel → {verify_channel.mention} "
                             f"(⚠️ I couldn't set its permissions — do it by hand)")
            locked, _skipped = await self._lock_unverified(guild, unverified, verify_channel.id)
            lines.append(f"• Hidden from Unverified on {locked} other channel(s); new channels follow")

        store.set_enabled(guild.id, True)
        ready = store.is_ready(store.get(guild.id))
        note = ("\n\n**Verify is ON.** " if ready else
                "\n\n**Almost there** — set a verify channel to finish: `/simpleverify set-channel`. ")
        note += ("New members now get **Unverified** on join and can only see the verify channel "
                 "(a channel you deliberately opened to them stays open). Post the button with "
                 "`/simpleverify panel`; re-run the hide with `/simpleverify lockdown` any time.")
        await interaction.followup.send("Set up:\n" + "\n".join(lines) + note, ephemeral=True)

    @group.command(name="set-roles", description="Use your own existing Unverified + Verified roles.")
    @app_commands.describe(unverified="Role given on join.", verified="Role given after verifying.")
    @app_commands.checks.has_permissions(administrator=True)
    async def set_roles(self, interaction: discord.Interaction,
                        unverified: discord.Role, verified: discord.Role):
        if unverified == verified:
            await interaction.response.send_message("Those need to be two different roles.", ephemeral=True)
            return
        warn = ""
        for r in (unverified, verified):
            if not _bot_can_manage(interaction.guild, r):
                warn += f"\n⚠️ My role must sit **above** {r.mention} or I can't assign it."
        store.set_roles(interaction.guild.id, unverified.id, verified.id)
        store.set_enabled(interaction.guild.id, True)
        await interaction.response.send_message(
            f"Verify roles set — Unverified {unverified.mention}, Verified {verified.mention}.{warn}",
            ephemeral=True)

    @group.command(name="set-channel", description="Set the channel the verify button lives in.")
    @app_commands.checks.has_permissions(administrator=True)
    async def set_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        store.set_channel(interaction.guild.id, channel.id)
        await interaction.response.send_message(
            f"Verify channel set to {channel.mention}. Post the button with `/simpleverify panel`.",
            ephemeral=True)

    @group.command(name="panel", description="Post the Verify button in the verify channel.")
    @app_commands.describe(message="Optional text shown above the button.")
    @app_commands.checks.has_permissions(administrator=True)
    async def panel(self, interaction: discord.Interaction, message: str = None):
        cfg = store.get(interaction.guild.id)
        channel = interaction.guild.get_channel(int(cfg["channel_id"])) if cfg.get("channel_id") else None
        if channel is None:
            await interaction.response.send_message(
                "Set a verify channel first: `/simpleverify set-channel`.", ephemeral=True)
            return
        body = message or "Click **Verify** to get access to the server."
        try:
            sent = await channel.send(content=body, view=_panel_view(),
                                      allowed_mentions=discord.AllowedMentions.none())
        except discord.Forbidden:
            await interaction.response.send_message(
                f"I can't post in {channel.mention} — I need **Send Messages** there.", ephemeral=True)
            return
        store.set_panel_message(interaction.guild.id, sent.id)
        await interaction.response.send_message(f"Verify panel posted in {channel.mention}. ✅", ephemeral=True)

    @group.command(name="status", description="Show this server's verify settings.")
    @app_commands.checks.has_permissions(administrator=True)
    async def status(self, interaction: discord.Interaction):
        cfg = store.get(interaction.guild.id)
        g = interaction.guild

        def rolestr(rid):
            r = g.get_role(int(rid)) if rid else None
            return r.mention if r else (f"`{rid}` (missing)" if rid else "*not set*")

        ch = g.get_channel(int(cfg["channel_id"])) if cfg.get("channel_id") else None
        ready = store.is_ready(cfg)
        e = discord.Embed(
            title="Simple verify",
            description=("🟢 **On** — new members are held until they verify." if ready
                         else "⚪ **Off / incomplete** — finish setup to arm it."),
            colour=discord.Colour.green() if ready else discord.Colour.light_grey())
        e.add_field(name="Unverified role", value=rolestr(cfg.get("unverified_role_id")))
        e.add_field(name="Verified role", value=rolestr(cfg.get("verified_role_id")))
        e.add_field(name="Verify channel", value=ch.mention if ch else "*not set*", inline=False)
        await interaction.response.send_message(embed=e, ephemeral=True)

    @group.command(name="disable", description="Turn simple verify off (settings are kept).")
    @app_commands.checks.has_permissions(administrator=True)
    async def disable(self, interaction: discord.Interaction):
        store.disable(interaction.guild.id)
        await interaction.response.send_message(
            "Simple verify is **off**. New members won't be held; your roles and channel are kept.",
            ephemeral=True)

    async def cog_app_command_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            msg = "You need **Administrator** to configure verify."
        else:
            msg = "Something went wrong running that."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


async def setup(bot):
    await bot.add_cog(SimpleVerify(bot))
