"""`/blocklist` — the operator's list of servers the bot refuses to stay in.

Home-server only, administrator only. Backed by utils/guild_blocklist.py; the
join-time refusal and the startup sweep live in bot.py so they run before any
cog touches a blocked server.

What a block does, in order:
  1. the row is written (a re-add of the bot is refused from now on);
  2. if the bot is in the server it leaves — bot.py records the departure as a
     `blocked` ledger row rather than a plain `remove`, and sends no alert
     because the operator is the one doing it;
  3. everything the message archive holds for that server is purged through
     the mod-log cog's own purge (messages, edits, identity ledger, command
     log, media index, cached files). That is what torvex.app/TrustSafety
     promises: "everything it stored for that server is deleted".
The purge also runs from `on_guild_remove` for the join-then-leave path, so a
blocked server that re-adds the bot never leaves rows behind either.
"""

import asyncio
import os
import sys
import time

import discord
from discord import app_commands
from discord.ext import commands

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from utils import guild_blocklist as store  # noqa: E402

HOME_GUILD_ID = int(os.getenv("HOME_GUILD_ID", "1215140346800119868"))


class GuildBlocklist(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # ────────────────────────────────────────────────────────────── helpers
    async def _purge(self, gid: int) -> dict:
        """Run the mod-log cog's whole-guild purge off the event loop.
        Returns the counts, or {} when the archive cog isn't loaded."""
        modlog = self.bot.get_cog("ModLog")
        if modlog is None or not hasattr(modlog, "_purge_guild"):
            return {}
        try:
            return await asyncio.to_thread(modlog._purge_guild, str(gid)) or {}
        except Exception as e:  # never let a purge failure hide the block itself
            print(f"[WARN] blocklist purge failed for {gid}: {e}")
            return {}

    @staticmethod
    def _fmt_counts(counts: dict) -> str:
        if not counts:
            return "archive purge skipped (mod-log cog not loaded)"
        keep = {k: v for k, v in counts.items() if v}
        return ("purged " + ", ".join(f"{v} {k}" for k, v in keep.items())) if keep \
            else "nothing was stored for it"

    # ───────────────────────────────────────────────────────────── listener
    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild):
        # The join-time refusal in bot.py leaves the server; the purge is ours.
        if store.is_blocked(guild.id):
            counts = await self._purge(guild.id)
            print(f"[GUILD] BLOCKED purge {guild.id} ({guild.name!r}): {self._fmt_counts(counts)}")

    # ───────────────────────────────────────────────────────────── commands
    group = app_commands.Group(
        name="blocklist",
        description="[Operator] Servers the bot refuses to stay in (home server only)",
        default_permissions=discord.Permissions(administrator=True),
        guild_only=True)

    async def _home_only(self, interaction: discord.Interaction) -> bool:
        if interaction.guild_id != HOME_GUILD_ID:
            await interaction.response.send_message("Home-server only.", ephemeral=True)
            return False
        return True

    @group.command(name="add", description="Block a server: the bot leaves it, purges its archive, and refuses to be re-added.")
    @app_commands.describe(guild_id="The server's id", reason="Why — this is the record")
    @app_commands.checks.has_permissions(administrator=True)
    async def add(self, interaction: discord.Interaction, guild_id: str, reason: str):
        if not await self._home_only(interaction):
            return
        gid = store.parse_guild_id(guild_id)
        if gid is None:
            await interaction.response.send_message("That's not a guild id.", ephemeral=True)
            return
        if gid == HOME_GUILD_ID:
            await interaction.response.send_message("Not the home server.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        guild = self.bot.get_guild(gid)
        entry = store.block(gid, reason, added_by=interaction.user.id,
                            guild_name=guild.name if guild else None)
        lines = [f"⛔ **{guild.name if guild else gid}** is blocked — {entry['reason']}"]
        if guild is not None:
            # bot.py reads this to record the departure as `blocked` and to skip
            # the operator alert (you're the one doing it).
            getattr(self.bot, "blocked_leaving", set()).add(gid)
            try:
                await guild.leave()
                lines.append(f"Left the server ({guild.member_count} members). "
                             "Archive purge runs on the way out.")
            except Exception as e:
                lines.append(f"⚠️ Could not leave: {e}")
        else:
            counts = await self._purge(gid)
            lines.append("The bot isn't in that server; " + self._fmt_counts(counts) + ".")
        lines.append("It will be refused if it adds the bot again.")
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    @group.command(name="remove", description="Unblock a server (it can add the bot again).")
    @app_commands.describe(guild_id="The server's id")
    @app_commands.checks.has_permissions(administrator=True)
    async def remove(self, interaction: discord.Interaction, guild_id: str):
        if not await self._home_only(interaction):
            return
        gid = store.parse_guild_id(guild_id)
        if gid is None:
            await interaction.response.send_message("That's not a guild id.", ephemeral=True)
            return
        entry = store.get(gid)
        if not store.unblock(gid):
            await interaction.response.send_message("That server isn't blocked.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"✅ **{entry['guild_name'] or gid}** unblocked. Nothing purged is restored.",
            ephemeral=True)

    @group.command(name="list", description="Every blocked server, newest first.")
    @app_commands.checks.has_permissions(administrator=True)
    async def list_(self, interaction: discord.Interaction):
        if not await self._home_only(interaction):
            return
        rows = store.all_blocked()
        if not rows:
            await interaction.response.send_message("No servers are blocked.", ephemeral=True)
            return
        lines = []
        for r in rows[:40]:
            when = f"<t:{int(r['added_ts'])}:d>"
            who = f" by <@{r['added_by']}>" if r["added_by"] else ""
            lines.append(f"• **{r['guild_name'] or '?'}** `{r['guild_id']}` — {r['reason']} ({when}{who})")
        more = f"\n…and {len(rows) - 40} more" if len(rows) > 40 else ""
        await interaction.response.send_message(
            f"⛔ **{len(rows)} blocked server{'s' if len(rows) != 1 else ''}**\n"
            + "\n".join(lines) + more, ephemeral=True)


async def setup(bot):
    await bot.add_cog(GuildBlocklist(bot))
