"""Discovery surfaces: /help, /add-bot, and the first message a new server sees.

Everything here exists to answer one question — "how do I configure this thing?"
The old answer was `/setup`, whose API call 500s on a fresh guild, and nothing
mentioned the security suite at all. So the honest path for a new owner was
/help -> /setup -> error -> give up, while the working configuration surface
(the dashboard) was invisible unless someone told you it existed.
"""
import json
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
from utils.command_sections import (  # noqa: E402
    FRIENDLY, SECTIONS, SECTION_ICONS, section_of,
)

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DOCS_URL = os.environ.get("COMMAND_DOCS_URL", DASHBOARD_URL.rstrip("/") + "/docs/commands")

log = logging.getLogger("help")

INVITE_URL = invite_url()   # kept as a module name for anything importing it


class Help(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # ------------------------------------------------------------------ /help
    # Built from the LIVE command tree, not prose: a hand-written list drifted
    # within weeks (it never mentioned /activity, /server-info, /lock, /invite,
    # /ask…). Grouping comes from utils/command_sections.py — the same table
    # the docs generator uses — and cog ownership from commands.json, the same
    # manifest bot.py loads cogs from. Nothing here can name a command that
    # doesn't exist, and a new cog shows up the moment it's registered.
    def _manifest(self):
        try:
            with open(os.path.join(ROOT, "commands.json"), encoding="utf-8") as fh:
                data = json.load(fh)
            return {c["name"]: c for c in data.get("commands", []) if c.get("name")}
        except (OSError, ValueError):
            return {}

    def _roots(self):
        """Top-level slash commands, sorted, context menus excluded."""
        out = []
        for c in self.bot.tree.get_commands():
            if isinstance(c, (app_commands.Command, app_commands.Group)):
                out.append(c)
        return sorted(out, key=lambda c: c.name)

    def _grouped(self):
        man = self._manifest()
        buckets = {}
        for c in self._roots():
            cog = (man.get(c.name) or {}).get("cog", "?")
            buckets.setdefault(section_of(cog), []).append(c)
        order = [t for t, _ in SECTIONS] + ["Other"]
        return [(t, buckets[t]) for t in order if buckets.get(t)]

    @staticmethod
    def _sig(cmd, prefix=""):
        """/root sub <required> [optional] — the shape Discord's picker shows."""
        parts = [f"/{prefix}{cmd.qualified_name}"]
        for p in getattr(cmd, "parameters", []):
            parts.append(f"<{p.name}>" if p.required else f"[{p.name}]")
        return " ".join(parts)

    @staticmethod
    def _leaves(cmd):
        if isinstance(cmd, app_commands.Group):
            out = []
            for sub in cmd.commands:
                out.extend(Help._leaves(sub))
            return out
        return [cmd]

    @app_commands.command(name="help", description="Show all available commands, or details for one.")
    @app_commands.describe(command="A command to explain in detail (leave empty for the overview)")
    async def help(self, interaction: discord.Interaction, command: str = None):
        gid = interaction.guild.id if interaction.guild else None
        if command:
            return await self._help_one(interaction, command.lstrip("/").strip().lower(), gid)

        groups = self._grouped()
        total = sum(len(self._leaves(c)) for _, cs in groups for c in cs)
        embed = discord.Embed(
            title="🐸 Peepo's Reclaimer — Commands",
            description=(f"**{total} commands** in {len(groups)} groups. "
                         f"`/help command:<name>` explains one; the full reference with every "
                         f"option is at **[{DOCS_URL.split('//')[1]}]({DOCS_URL})**.\n"
                         f"⚙️ Configure everything at **[{DASHBOARD_URL.split('//')[1]}]"
                         f"({dashboard_url(gid)})**."),
            color=0x5865F2)
        for title, cmds in groups:
            names = []
            for c in cmds:
                n = len(self._leaves(c))
                names.append(f"`/{c.name}`" + (f" ({n})" if n > 1 else ""))
            value = " · ".join(names)
            if len(value) > 1000:                       # embed field cap is 1024
                value = value[:990].rsplit(" · ", 1)[0] + " · …"
            embed.add_field(name=f"{SECTION_ICONS.get(title, '📦')} {title}",
                            value=value, inline=False)
        embed.set_footer(text=f"(n) = subcommands · Questions? {SUPPORT_INVITE}")
        await interaction.response.send_message(
            embed=embed, view=config_view(gid), ephemeral=True)

    async def _help_one(self, interaction, name, gid):
        root = next((c for c in self._roots() if c.name == name), None)
        if root is None:
            # maybe they typed a subcommand path: "activity user"
            first = name.split(" ")[0]
            root = next((c for c in self._roots() if c.name == first), None)
        if root is None:
            await interaction.response.send_message(
                f"No command called `/{name}`. Try `/help` for the list.", ephemeral=True)
            return
        man = self._manifest().get(root.name, {})
        cog = man.get("cog", "?")
        embed = discord.Embed(
            title=f"/{root.name}",
            description=root.description or "—",
            color=0x5865F2)
        embed.add_field(name="Group", value=f"{FRIENDLY.get(cog, cog)} · {section_of(cog)}",
                        inline=True)
        perms = man.get("required_permissions") or []
        if perms:
            embed.add_field(name="Needs", value=", ".join(p.replace("_", " ").title() for p in perms),
                            inline=True)
        leaves = self._leaves(root)
        lines = []
        for leaf in leaves:
            line = f"`{self._sig(leaf)}`"
            if leaf.description and leaf is not root:
                line += f" — {leaf.description}"
            lines.append(line)
        # parameters of a single command get their own explanations
        if len(leaves) == 1 and getattr(root, "parameters", None):
            for p in root.parameters:
                lines.append(f"• `{p.name}`{'' if p.required else ' *(optional)*'} — {p.description or '—'}")
        text = "\n".join(lines)
        for i in range(0, max(len(text), 1), 1000):
            embed.add_field(name="Usage" if i == 0 else "​", value=text[i:i + 1000] or "—", inline=False)
        embed.set_footer(text=f"Full reference: {DOCS_URL}")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @help.autocomplete("command")
    async def _help_ac(self, interaction: discord.Interaction, current: str):
        cur = current.lstrip("/").lower()
        names = [c.name for c in self._roots()]
        hits = [n for n in names if n.startswith(cur)] + [n for n in names if cur in n and not n.startswith(cur)]
        return [app_commands.Choice(name=f"/{n}", value=n) for n in hits[:25]]

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
