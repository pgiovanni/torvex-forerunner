import discord
from discord import app_commands
from discord.ext import commands
import aiohttp
import os
import logging

log = logging.getLogger("gifts")

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
                if r.status >= 400:
                    log.error(f"{method} {path} -> {r.status} | {data}")
                return r.status, data
    except Exception as e:
        log.error(f"{method} {path} -> connection error: {e}")
        return 0, {}


async def _ensure_linked(user: discord.User | discord.Member) -> bool:
    status, _ = await _api("POST", "/api/bot/auto-link", json={
        "discordUserId": str(user.id),
        "discordUsername": user.display_name
    })
    return status == 200


class Gifts(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    gift = app_commands.Group(name="gift", description="Gift commands")

    @gift.command(name="coins", description="Gift coins to another player.")
    @app_commands.describe(
        user="The player to receive the coins",
        amount="How many coins to gift"
    )
    async def gift_coins(self, interaction: discord.Interaction, user: discord.Member, amount: int):
        await interaction.response.defer(ephemeral=True)

        if user == interaction.user:
            await interaction.followup.send("You can't gift coins to yourself.", ephemeral=True)
            return
        if user.bot:
            await interaction.followup.send("You can't gift coins to a bot.", ephemeral=True)
            return
        if amount < 1:
            await interaction.followup.send("Amount must be at least 1.", ephemeral=True)
            return

        if not await _ensure_linked(interaction.user):
            await interaction.followup.send("Could not connect to Torvex.", ephemeral=True)
            return
        if not await _ensure_linked(user):
            await interaction.followup.send(f"{user.display_name} is not linked to Torvex.", ephemeral=True)
            return

        status, data = await _api("POST", "/api/bot/game/gift-coins", json={
            "senderDiscordId": str(interaction.user.id),
            "recipientDiscordId": str(user.id),
            "amount": amount
        })

        if status == 200:
            sender_balance = data.get("senderNewBalance", 0)
            recipient_balance = data.get("recipientNewBalance", 0)

            embed = discord.Embed(
                title="Coin Gift",
                description=(
                    f"{interaction.user.mention} gifted **{amount:,}** coins to {user.mention}!"
                ),
                color=0xFFD700
            )
            embed.add_field(
                name=f"{user.display_name}'s new balance",
                value=f"🪙 {recipient_balance:,} coins",
                inline=True
            )
            embed.set_footer(text=f"{interaction.user.display_name} now has {sender_balance:,} coins remaining")

            # Send the public-facing embed to the channel
            await interaction.followup.send(embed=embed, ephemeral=False)
        else:
            error = data.get("error", "Something went wrong.")
            await interaction.followup.send(f"Failed: {error}", ephemeral=True)


async def setup(bot):
    await bot.add_cog(Gifts(bot))
