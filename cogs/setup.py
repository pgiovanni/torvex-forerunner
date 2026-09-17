import discord
from discord import app_commands
from discord.ext import commands
import aiohttp
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from utils.links import config_view, dashboard_url  # noqa: E402
from utils.quiet_removals import is_quiet  # noqa: E402

TORVEX_API_URL = os.getenv("TORVEX_API_URL", "http://localhost:5000")
TORVEX_BOT_KEY = os.getenv("TORVEX_BOT_KEY", "")
HEADERS = {"X-Bot-Key": TORVEX_BOT_KEY, "Content-Type": "application/json"}


def parse_guild_allowlist(*values):
    """Guild ids from the first env value that has any — commas or spaces.

    Same shape as RECON_GUILDS / BACKUP_GUILDS / XP_TRANSFER_GUILDS: the
    operator names their OWN servers in the environment, because this is
    their call to make and not a toggle a guild admin finds on a dashboard.
    """
    for raw in values:
        ids = {int(g) for g in (raw or "").replace(",", " ").split() if g.strip().isdigit()}
        if ids:
            return ids
    return set()


# Where /setup leave-server may be RUN from. Unset = the operator's own guild.
BOT_ADMIN_GUILDS = parse_guild_allowlist(
    os.environ.get("BOT_ADMIN_GUILDS"), os.environ.get("ALTGUARD_GUILD_ID"))


def can_leave(target_id: int, invoked_in: int, operator_guilds: set) -> tuple[bool, str]:
    """(ok, reason) — may this invocation make the bot leave `target_id`?

    Order matters. An empty allowlist means NOBODY, not everybody, so a
    misconfigured env fails closed. The home-guild guard sits last and
    catches the operator too: no picking the wrong row and evicting the bot
    from the server you are standing in, which nothing in Discord's UI can
    undo for you.
    """
    if not operator_guilds or invoked_in not in operator_guilds:
        return False, "operator"
    if not target_id:
        return False, "unknown"
    if target_id in operator_guilds or target_id == invoked_in:
        return False, "home"
    return True, ""


async def _api(method: str, path: str, **kwargs):
    url = f"{TORVEX_API_URL}{path}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.request(method, url, headers=HEADERS, **kwargs) as r:
                try:
                    data = await r.json()
                except Exception:
                    data = {}
                return r.status, data
    except Exception as e:
        return 0, {"error": str(e)}


async def get_guild_config(guild_id: int) -> dict:
    status, data = await _api("GET", f"/api/bot/guild-config/{guild_id}")
    if status == 200:
        return data
    return {}


class Setup(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    setup = app_commands.Group(name="setup", description="Configure the bot for this server (Admin only)")

    async def _save(self, interaction: discord.Interaction, **fields):
        if not interaction.guild:
            await interaction.response.send_message("❌ Must be used in a server.", ephemeral=True)
            return
        status, data = await _api("POST", f"/api/bot/guild-config/{interaction.guild.id}", json=fields)
        if status == 200:
            field_name = list(fields.keys())[0]
            channel_id = list(fields.values())[0]
            channel = interaction.guild.get_channel(int(channel_id)) if channel_id else None
            await interaction.response.send_message(
                f"✅ **{field_name.replace('ChannelId', '').replace('Id', '')} channel** set to {channel.mention if channel else 'none'}.",
                ephemeral=True
            )
        else:
            # This is the known dead end for a brand-new server: the web API
            # 500s on a guild it has no config row for. Never leave an admin
            # holding just an error code — hand them the surface that works.
            err = data.get("error") or f"HTTP {status}" if status else "couldn't reach the API"
            await interaction.response.send_message(
                f"❌ Couldn't save that here ({err}).\n"
                f"⚙️ Use the dashboard instead — it configures the same settings: "
                f"{dashboard_url(interaction.guild.id)}",
                view=config_view(interaction.guild.id), ephemeral=True)

    @setup.command(name="view", description="View current channel configuration for this server.")
    @app_commands.checks.has_permissions(administrator=True)
    async def view(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("❌ Must be used in a server.", ephemeral=True)
            return
        config = await get_guild_config(interaction.guild.id)

        def ch(channel_id):
            if not channel_id:
                return "*not set*"
            c = interaction.guild.get_channel(int(channel_id))
            return c.mention if c else f"<#{channel_id}> *(deleted?)*"

        embed = discord.Embed(title="⚙️ Server Bot Configuration", color=0x5865F2)
        embed.add_field(name="🔴 Status Channel",      value=ch(config.get("statusChannelId")),      inline=False)
        embed.add_field(name="📦 Loot Drop Channel",   value=ch(config.get("lootDropChannelId")),    inline=False)
        embed.add_field(name="⚔️ RPG Channel",         value=ch(config.get("rpgChannelId")),         inline=False)
        embed.add_field(name="💡 Suggestions Channel", value=ch(config.get("suggestionsChannelId")), inline=False)
        embed.add_field(name="👋 Welcome Channel",     value=ch(config.get("welcomeChannelId")),     inline=False)
        embed.add_field(name="🫡 Leaves Channel",       value=ch(os.getenv("GOODBYE_CHANNEL_ID")),    inline=False)
        embed.add_field(name="🔨 Mod Log Channel",     value=ch(config.get("modLogChannelId")),      inline=False)
        if not config:
            embed.description = ("⚠️ Couldn't read this server's config from the web API. "
                                 "The dashboard below configures the same settings.")
        embed.set_footer(text="Security, mod logs and more: /help")
        await interaction.response.send_message(
            embed=embed, view=config_view(interaction.guild.id), ephemeral=True)

    @setup.command(name="status-channel", description="Channel for bot online/offline notices.")
    @app_commands.describe(channel="The channel to post bot status updates")
    @app_commands.checks.has_permissions(administrator=True)
    async def status_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await self._save(interaction, statusChannelId=str(channel.id))

    @setup.command(name="loot-channel", description="Channel for crate opens, rare drops, and boss kill announcements.")
    @app_commands.describe(channel="The channel to post loot drop announcements")
    @app_commands.checks.has_permissions(administrator=True)
    async def loot_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await self._save(interaction, lootDropChannelId=str(channel.id))

    @setup.command(name="rpg-channel", description="Channel where RPG fight results are posted.")
    @app_commands.describe(channel="The channel for RPG combat output")
    @app_commands.checks.has_permissions(administrator=True)
    async def rpg_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await self._save(interaction, rpgChannelId=str(channel.id))

    @setup.command(name="suggestions-channel", description="Channel where /suggest posts land.")
    @app_commands.describe(channel="The channel for suggestions")
    @app_commands.checks.has_permissions(administrator=True)
    async def suggestions_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await self._save(interaction, suggestionsChannelId=str(channel.id))

    @setup.command(name="welcome-channel", description="Channel for new member welcome messages.")
    @app_commands.describe(channel="The channel for welcome messages")
    @app_commands.checks.has_permissions(administrator=True)
    async def welcome_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await self._save(interaction, welcomeChannelId=str(channel.id))

    @setup.command(name="mod-log", description="Channel for mod action logs.")
    @app_commands.describe(channel="The channel for mod logs")
    @app_commands.checks.has_permissions(administrator=True)
    async def mod_log(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await self._save(interaction, modLogChannelId=str(channel.id))

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        """Post a goodbye message to the leaves channel when a member leaves.
        Channel is set via the GOODBYE_CHANNEL_ID env var — independent of the Torvex
        guild-config API so it works even while that service is down."""
        if member.bot or is_quiet(member.id):
            return
        ch_id = os.getenv("GOODBYE_CHANNEL_ID")
        if not ch_id:
            return
        channel = member.guild.get_channel(int(ch_id))
        if channel is None:
            return
        try:
            await channel.send(
                f"<@{member.id}> goodbye {member.name}",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            pass

    # ── Operator: pull the bot out of somebody else's server ──────────────
    # A bot cannot be removed from Discord's own UI by anyone but an admin of
    # the server it is in. When the operator has walked away from a server,
    # the alternatives are to wait to be kicked — which leaves the bot, and
    # its logging, sitting in a room they have left, on someone else's clock
    # — or this. It leaves silently: no farewell post, nothing for anyone
    # there to react to.
    @setup.command(name="leave-server",
                   description="Make the bot leave another server, quietly (operator only).")
    @app_commands.describe(
        server="Server to leave — start typing to pick it from the list.",
        confirm="Tick to confirm. The bot leaves immediately.")
    @app_commands.checks.has_permissions(administrator=True)
    async def leave_server(self, interaction: discord.Interaction, server: str, confirm: bool):
        if not interaction.guild:
            await interaction.response.send_message("❌ Must be used in a server.", ephemeral=True)
            return

        target_id = int(server) if str(server).isdigit() else 0
        ok, reason = can_leave(target_id, interaction.guild.id, BOT_ADMIN_GUILDS)
        if not ok:
            msg = {
                "operator": "❌ That one is operator-only — run it from your own server.",
                "unknown": "❌ Pick a server from the list (or paste its id).",
                "home": "❌ That's one of your own servers. Not leaving that one.",
            }[reason]
            await interaction.response.send_message(msg, ephemeral=True)
            return

        guild = self.bot.get_guild(target_id)
        if guild is None:
            await interaction.response.send_message(
                f"❌ Not in a server with id `{target_id}` — nothing to leave.", ephemeral=True)
            return

        if not confirm:
            # Say what it costs BEFORE doing it: getting back in needs somebody
            # with Manage Server over there, which is exactly the person the
            # operator is usually walking away from.
            await interaction.response.send_message(
                f"⚠️ **{guild.name}** ({guild.member_count} members) — run it again with "
                f"`confirm: True` to leave. Re-adding the bot later needs someone with "
                f"**Manage Server** there to invite it back.", ephemeral=True)
            return

        name, count = guild.name, guild.member_count
        try:
            await guild.leave()
        except discord.HTTPException as e:
            await interaction.response.send_message(
                f"❌ Couldn't leave **{name}** — {e}", ephemeral=True)
            return

        # on_guild_remove writes the ledger row; no announcement anywhere.
        await interaction.response.send_message(
            f"✅ Left **{name}** (`{target_id}`, {count} members). Quietly — nothing was posted there.",
            ephemeral=True)

    @leave_server.autocomplete("server")
    async def leave_server_autocomplete(self, interaction: discord.Interaction, current: str):
        # Only ever offered to someone who could actually run it, and the
        # operator's own servers are left out of the list entirely rather than
        # shown and then refused.
        if not interaction.guild or interaction.guild.id not in BOT_ADMIN_GUILDS:
            return []
        q = (current or "").lower()
        out = []
        for g in sorted(self.bot.guilds, key=lambda x: x.name.lower()):
            if g.id in BOT_ADMIN_GUILDS:
                continue
            if q and q not in g.name.lower() and q not in str(g.id):
                continue
            out.append(app_commands.Choice(name=f"{g.name} ({g.member_count})"[:100], value=str(g.id)))
            if len(out) == 25:
                break
        return out

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message("❌ Administrator permission required.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(Setup(bot))
