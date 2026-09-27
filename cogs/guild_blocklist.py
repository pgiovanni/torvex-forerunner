"""Server blocklist enforcement — listener-only cog, no commands.

The list itself is managed on the dashboard (forerunner.torvex.app
/operator/blocklist; operator = the home guild's owner). Operator-level actions
never get a slash command (Paul, 9/27: "don't add commands for owner level
actions. put it in a dashboard"). Store: utils/guild_blocklist.py, shared with
the dashboard through /var/lib/torvex.

Three enforcement points:
  * bot.py `on_guild_join` — a blocked server that adds the bot is left in the
    same event, with one operator email;
  * bot.py `on_ready` — sweep of every current guild at startup;
  * this cog's minute loop — picks up a block the dashboard wrote while the
    bot is inside that server, so no restart is ever needed.
And one clean-up point: `on_guild_remove` here purges everything the message
archive holds for a blocked server (messages, edits, identity ledger, command
log, media index, cached files) through the mod-log cog's own purge. That is
what torvex.app/TrustSafety promises — "everything it stored for that server
is deleted" — and it runs on both the join-then-leave and the dashboard paths.
"""

import asyncio
import os
import sys

import discord
from discord.ext import commands, tasks

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from utils import guild_blocklist as store  # noqa: E402

SWEEP_SECONDS = 60


class GuildBlocklist(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.sweep.start()

    def cog_unload(self):
        self.sweep.cancel()

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

    async def _leave_blocked(self, guild: discord.Guild, entry: dict):
        print(f"[GUILD] BLOCKED {guild.id} ({guild.name!r}) — {entry['reason']!r}; leaving")
        # bot.py reads this to record the departure as `blocked` and to skip the
        # operator alert — the operator is the one who blocked it.
        getattr(self.bot, "blocked_leaving", set()).add(guild.id)
        try:
            await guild.leave()
        except Exception as e:
            print(f"[WARN] could not leave blocked guild {guild.id}: {e}")

    # ─────────────────────────────────────────────────────────────── sweep
    @tasks.loop(seconds=SWEEP_SECONDS)
    async def sweep(self):
        """A block the dashboard wrote while the bot is inside: leave now."""
        try:
            blocked = {e["guild_id"]: e for e in await asyncio.to_thread(store.all_blocked)}
        except Exception as e:
            print(f"[WARN] blocklist read failed: {e}")
            return
        if not blocked:
            return
        for guild in list(self.bot.guilds):
            entry = blocked.get(guild.id)
            if entry is not None:
                await self._leave_blocked(guild, entry)

    @sweep.before_loop
    async def _wait_ready(self):
        await self.bot.wait_until_ready()

    # ───────────────────────────────────────────────────────────── listener
    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild):
        # Every exit path from a blocked server ends here; the purge is ours.
        if store.is_blocked(guild.id):
            counts = await self._purge(guild.id)
            print(f"[GUILD] BLOCKED purge {guild.id} ({guild.name!r}): {self._fmt_counts(counts)}")


async def setup(bot):
    await bot.add_cog(GuildBlocklist(bot))
