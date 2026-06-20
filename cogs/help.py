import discord
from discord import app_commands
from discord.ext import commands


INVITE_URL = "https://discord.com/api/oauth2/authorize?client_id=1372003518667558952&permissions=310580210768&scope=bot%20applications.commands"


class Help(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="help", description="Show all available commands.")
    async def help(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="🐸 Peepo's Reclaimer — Commands",
            description="Here's everything the bot can do. Use `/` to get started.",
            color=0x5865F2
        )

        embed.add_field(
            name="⚔️ RPG — `/rpg`",
            value="Fight monsters, level up, earn orbs, craft gear, fish, mine, and more. Full Torvex Lescala RPG experience.",
            inline=False
        )
        embed.add_field(
            name="🐸 Peepo Collectibles — `/peepo`",
            value="Collect and trade rare Peepo emotes. Browse the shop, check your collection, or hit the marketplace.",
            inline=False
        )
        embed.add_field(
            name="💰 Economy — `/economy`",
            value="Earn Peepo Bucks by chatting. Check your balance, view the leaderboard, and climb the ranks.",
            inline=False
        )
        embed.add_field(
            name="⚔️ PvP — `/pvp`",
            value="Challenge other players to battles. Winner takes the glory.",
            inline=False
        )
        embed.add_field(
            name="🎒 Gear — `/gear`",
            value="Browse the item and monster dictionary — weapons, armor, elements, and monsters.",
            inline=False
        )
        embed.add_field(
            name="🎮 Games — `/fun` `/games` `/wordle` `/chess`",
            value="Roast someone, play 8ball, Tic Tac Toe, Connect 4, Wordle, or chess (vs bot or a friend).",
            inline=False
        )
        embed.add_field(
            name="🤝 Social — `/gift` `/trade` `/suggest`",
            value="Gift coins, trade RPG items, or submit a suggestion for the server.",
            inline=False
        )
        embed.add_field(
            name="⚙️ Setup — `/setup` *(Admin only)*",
            value="Configure the bot for your server — set channels for status, RPG, loot drops, suggestions, welcome, and mod logs.",
            inline=False
        )

        embed.set_footer(text="💬 Questions? Join Peepo's Redemption: discord.gg/scpwTFGVkz")

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="invite", description="Add Peepo's Reclaimer to your server.")
    async def invite(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="🐸 Add Peepo's Reclaimer",
            description=f"[Click here to invite the bot to your server]({INVITE_URL})\n\nBrings the full Torvex RPG, Peepo collectibles, economy, games, and more.",
            color=0x5865F2
        )
        embed.set_footer(text="torvex.app — discord.gg/scpwTFGVkz")
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Help(bot))
