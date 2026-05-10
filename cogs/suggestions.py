import discord
from discord import app_commands
from discord.ext import commands
from cogs.setup import get_guild_config


class Suggestions(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="suggest", description="Submit a suggestion for the server.")
    @app_commands.describe(suggestion="Your suggestion — be as detailed as possible")
    async def suggest(self, interaction: discord.Interaction, suggestion: str):
        if not interaction.guild:
            await interaction.response.send_message("❌ Must be used in a server.", ephemeral=True)
            return

        config = await get_guild_config(interaction.guild.id)
        channel_id = config.get("suggestionsChannelId")
        channel = interaction.guild.get_channel(int(channel_id)) if channel_id else None
        if channel is None:
            await interaction.response.send_message(
                "❌ No suggestions channel configured. Ask an admin to run `/setup suggestions-channel`.",
                ephemeral=True
            )
            return

        embed = discord.Embed(
            title="💡 New Suggestion",
            description=suggestion,
            color=0x5865F2
        )
        embed.set_author(name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url)
        embed.set_footer(text=f"From: {interaction.guild.name if interaction.guild else 'DM'}")

        msg = await channel.send(embed=embed)
        await msg.add_reaction("✅")
        await msg.add_reaction("❌")

        await interaction.response.send_message(
            "✅ Your suggestion has been submitted! Thanks.\n"
            "💬 Have more questions? Join Peepo's Redemption: https://discord.gg/scpwTFGVkz",
            ephemeral=True
        )


async def setup(bot):
    await bot.add_cog(Suggestions(bot))
