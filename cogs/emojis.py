import discord
from discord import app_commands
from discord.ext import commands
import requests
import os

class Emojis(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="backup_emojis", description="Download all server emojis and save them to the emojis/ folder on the bot host.")
    @app_commands.checks.has_permissions(manage_emojis=True)
    async def backup_emojis(self, interaction: discord.Interaction):
        guild = interaction.guild
        os.makedirs("emojis", exist_ok=True)

        await interaction.response.send_message(f"Backing up {len(guild.emojis)} emojis...", ephemeral=True)

        for emoji in guild.emojis:
            ext = "gif" if emoji.animated else "png"
            data = requests.get(str(emoji.url)).content
            with open(f"emojis/{emoji.name}.{ext}", "wb") as f:
                f.write(data)

        await interaction.followup.send(f"Done! {len(guild.emojis)} emojis saved.", ephemeral=True)

async def setup(bot):
    await bot.add_cog(Emojis(bot))
