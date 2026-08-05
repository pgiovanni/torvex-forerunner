"""Discovery surfaces: /help, /add-bot, and the first message a new server sees.

Everything here exists to answer one question — "how do I configure this thing?"
The old answer was `/setup`, whose API call 500s on a fresh guild, and nothing
mentioned the security suite at all. So the honest path for a new owner was
/help -> /setup -> error -> give up, while the working configuration surface
(the dashboard) was invisible unless someone told you it existed.
"""
import logging
import os
import sys

import discord
from discord import app_commands
from discord.ext import commands

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from utils.links import (  # noqa: E402
    DASHBOARD_URL, SUPPORT_INVITE, config_view, dashboard_url, invite_url,
)

log = logging.getLogger("help")

INVITE_URL = invite_url()   # kept as a module name for anything importing it


class Help(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="help", description="Show all available commands.")
    async def help(self, interaction: discord.Interaction):
        gid = interaction.guild.id if interaction.guild else None
        embed = discord.Embed(
            title="🐸 Peepo's Reclaimer — Commands",
            description=f"Here's everything the bot can do. Use `/` to get started.\n\n"
                        f"⚙️ **Configure everything at [{DASHBOARD_URL.split('//')[1]}]"
                        f"({dashboard_url(gid)})** — every setting below, in one place.",
            color=0x5865F2
        )

        # Security first: it's the reason most servers add the bot, and it was
        # the section /help never had.
        embed.add_field(
            name="🛡️ Security — `/security` *(Admin)*",
            value="**Free.** Anti-Nuke (mass ban/delete protection), AltGuard (alt + VPN "
                  "detection), LinkGuard (IP-grabber and canary links), and quarantine "
                  "lockdown.\n`/security setup` wires it up in one command · `/security status` "
                  "to check · `/security audit` scans your roles for dangerous permissions.",
            inline=False
        )
        embed.add_field(
            name="📜 Mod Logs — `/msglog` *(Manage Server)*",
            value="Deletions with who-deleted-them, edit history, member and role changes. "
                  "`/msglog enable` to start · `/msglog terms` for what gets stored.",
            inline=False
        )
        embed.add_field(
            name="🔨 Moderation — `/ban` `/kick` `/timeout` `/prune-messages`",
            value="Standard moderation, gated on real Discord permissions.",
            inline=False
        )
        embed.add_field(
            name="🎭 Reaction Roles — `/rolemenu` *(Manage Roles)*",
            value="Button panels members click to give themselves roles.\n"
                  "**`/rolemenu template`** builds a whole set in one go — pronouns, age, "
                  "regions, colours, notifications, platforms, DM preference — and creates any "
                  "roles you don't have yet. Any emoji works, including your own.",
            inline=False
        )
        embed.add_field(
            name="⚙️ Automation — `/automation` *(Admin)*",
            value="Roles granted automatically on join, plus welcome and goodbye messages. "
                  "Separate from reaction roles: this is what the bot does *to* a member, "
                  "not what they pick for themselves.",
            inline=False
        )
        embed.add_field(
            name="⚔️ RPG — `/rpg`",
            value="Fight monsters, level up, earn orbs, craft gear, fish, mine, and more. "
                  "Full Torvex Lescala RPG experience.",
            inline=False
        )
        embed.add_field(
            name="🐸 Peepo Collectibles — `/peepo`",
            value="Collect and trade rare Peepo emotes. Browse the shop, check your "
                  "collection, or hit the marketplace.",
            inline=False
        )
        embed.add_field(
            name="💰 Economy & Levels — `/economy` `/rank` `/chat-levels`",
            value="Earn Peepo Bucks by chatting, climb the leaderboard, and unlock level roles.",
            inline=False
        )
        embed.add_field(
            name="🎮 Games — `/fun` `/games` `/wordle` `/chess` `/pvp` `/gear`",
            value="Roast someone, play 8ball, Tic Tac Toe, Connect 4, Wordle, chess, or duel "
                  "another member. `/gear` browses the item and monster dictionary.",
            inline=False
        )
        embed.add_field(
            name="🤝 Social — `/gift` `/trade` `/suggest`",
            value="Gift coins, trade RPG items, or submit a suggestion for the server.",
            inline=False
        )
        embed.add_field(
            name="⚙️ Channels — `/setup` *(Admin)*",
            value="Set channels for status, RPG, loot drops, suggestions, welcome and mod logs. "
                  f"The [dashboard]({dashboard_url(gid)}) does the same thing with menus.",
            inline=False
        )

        embed.set_footer(text=f"Questions? Join Peepo's Redemption — {SUPPORT_INVITE}")
        await interaction.response.send_message(
            embed=embed, view=config_view(gid), ephemeral=True)

    @app_commands.command(name="dashboard", description="Open the web dashboard to configure the bot.")
    async def dashboard_cmd(self, interaction: discord.Interaction):
        """A command people guess at. Cheap to provide, and it means nobody has
        to already know the URL to find the configuration surface."""
        gid = interaction.guild.id if interaction.guild else None
        embed = discord.Embed(
            title="⚙️ Torvex Dashboard",
            description=f"Configure every plugin from the web: **[{DASHBOARD_URL.split('//')[1]}]"
                        f"({dashboard_url(gid)})**\n\n"
                        "Log in with Discord and pick this server. You need **Manage Server** "
                        "here to change anything — the dashboard re-checks that with Discord on "
                        "every request, so it can't be faked.",
            color=0x5865F2)
        embed.add_field(
            name="What you can set up",
            value="AltGuard verification gate · Anti-Nuke · LinkGuard · Mod Logs · "
                  "Quarantine Lock · welcome and goodbye messages · join roles",
            inline=False)
        await interaction.response.send_message(
            embed=embed, view=config_view(gid), ephemeral=True)

    @app_commands.command(name="add-bot", description="Add Peepo's Reclaimer to your server.")
    async def add_bot(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="🐸 Add Peepo's Reclaimer",
            description=f"[Invite the bot to your server]({invite_url()})\n\n"
                        f"Free security suite (Anti-Nuke, AltGuard, LinkGuard), mod logs, "
                        f"levels, the full Torvex RPG, Peepo collectibles, economy and games.\n\n"
                        f"Once it's in, configure it at **[{DASHBOARD_URL.split('//')[1]}]"
                        f"({DASHBOARD_URL})** or run `/security setup`.",
            color=0x5865F2
        )
        embed.set_footer(text=f"torvex.app — {SUPPORT_INVITE}")
        await interaction.response.send_message(
            embed=embed, view=config_view(invite=True), ephemeral=True)

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        """Greet a new server with the config link once.

        Sent where staff will actually see it: the system channel if we can post
        there, otherwise the first channel we can, otherwise the owner's DMs.
        Nothing is configured by this — everything ships OFF — so this is the
        only prompt an owner gets that the bot needs setting up at all.
        """
        embed = discord.Embed(
            title="🐸 Thanks for adding Peepo's Reclaimer",
            description=f"Everything ships **off** until you turn it on.\n\n"
                        f"⚙️ **[Configure it here]({dashboard_url(guild.id)})** — or run "
                        f"`/security setup` for the security suite and `/help` for the rest.",
            color=0x5865F2)
        embed.add_field(
            name="Good first steps",
            value="• `/security setup` — Anti-Nuke, quarantine role and channel lockdown\n"
                  "• `/security audit` — find roles with dangerous permissions\n"
                  "• `/msglog enable` — deletion and edit logging",
            inline=False)
        embed.set_footer(text=f"Need a hand? {SUPPORT_INVITE}")
        view = config_view(guild.id)

        targets = []
        if guild.system_channel is not None:
            targets.append(guild.system_channel)
        targets += [c for c in guild.text_channels if c not in targets]
        for ch in targets[:5]:
            perms = ch.permissions_for(guild.me)
            if perms.send_messages and perms.embed_links:
                try:
                    await ch.send(embed=embed, view=view)
                    return
                except discord.HTTPException:
                    continue
        try:
            await guild.owner.send(embed=embed, view=view)
        except (discord.HTTPException, AttributeError):
            log.info("joined %s (%s) with nowhere to post the welcome", guild.name, guild.id)


async def setup(bot):
    await bot.add_cog(Help(bot))
