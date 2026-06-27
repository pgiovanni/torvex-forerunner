import discord
from discord import app_commands
from discord.ext import commands


def _can_act(invoker: discord.Member, target: discord.Member, me: discord.Member):
    """Hierarchy/sanity gate for acting on an in-server member. Returns an error
    string if the action is NOT allowed, else None."""
    guild = invoker.guild
    if target.id == invoker.id:
        return "You can't ban yourself."
    if target.id == me.id:
        return "I can't ban myself."
    if target.id == guild.owner_id:
        return "You can't ban the server owner."
    # invoker must outrank the target (owner bypasses the role check)
    if invoker.id != guild.owner_id and target.top_role >= invoker.top_role:
        return f"You can't ban {target.mention} — their highest role is above or equal to yours."
    # the bot must outrank the target to carry it out
    if target.top_role >= me.top_role:
        return (f"My role isn't high enough to ban {target.mention}. "
                "Move my role above theirs in **Server Settings → Roles**.")
    return None


class Moderation(commands.Cog):
    """Native moderation commands (ban / unban / prune-messages) — replacing MEE6."""

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

        audit = f"{interaction.user} ({interaction.user.id})"
        full_reason = (reason or "No reason provided") + f" — by {audit}"
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
        embed.set_footer(text=f"Banned by {interaction.user.display_name}")
        await interaction.followup.send(embed=embed)  # public confirmation
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

    @app_commands.command(
        name="prune-messages",
        description="Bulk-delete the last N messages in this channel (count-based, not by date).",
    )
    @app_commands.describe(amount="How many recent messages to delete (1–1000).")
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.checks.has_permissions(manage_messages=True)
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
