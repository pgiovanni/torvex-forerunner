import discord
from discord import app_commands
from discord.ext import commands
import aiohttp
import os

TORVEX_API_URL = os.getenv("TORVEX_API_URL", "http://localhost:5000")
TORVEX_BOT_KEY = os.getenv("TORVEX_BOT_KEY", "")
HEADERS = {"X-Bot-Key": TORVEX_BOT_KEY, "Content-Type": "application/json"}


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
            await interaction.response.send_message(f"❌ Failed to save: {data.get('error', 'Unknown error')}", ephemeral=True)

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
        embed.add_field(name="🔨 Mod Log Channel",     value=ch(config.get("modLogChannelId")),      inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

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

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message("❌ Administrator permission required.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(Setup(bot))
