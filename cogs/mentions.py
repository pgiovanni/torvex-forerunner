"""/mentions — your latest pings, with jump links, and the ones that vanished.

Members asked for it (2026-09-06): Discord's own Mentions inbox is easy to
lose in a busy server, and it forgets a ping the moment the message is
deleted — which is exactly the one you want to see. The archive already
knows both: `message_mentions` (utils/mentions.py, written when mod_log
archives a message) says who was pinged, `messages` says whether it is still
there and who took it down.

What a member sees (ephemeral, only ever their own pings):
  🔗 live       — author, channel, jump link
  🗑️ deleted    — the author took it back: shown WITH the text (ghost ping)
  🛡️ removed    — staff or a purge took it down: listed, text NOT re-shown
  ✏️ edited out — still posted, ping edited away: the original wording
  ↩️ reply      — a reply to one of your messages (optional)

Privacy rules: only channels the member can currently see (deleted channels
and ones they lost access to are skipped); mod-removed text is never
re-surfaced; own messages and bot chatter are excluded unless asked. The
window is the guild's archive window — 30 days on the operator/Pro archive,
the 24 h recent window everywhere else — so nothing is shown that the
archive wouldn't keep anyway.

/snipe (2026-10-18, Paul: "/mentions should also be a /snipe where it just
shows deleted messages"; then: "it lets people peruse the last 13 messages.
/snipe from phoenix bot does that. in public") — the same archive read the
other way round: not "who pinged me" but "what just vanished from THIS
channel". PUBLIC: the card posts in the channel, one deleted message per
page, ◀ ▶ buttons anyone can press walk the last SNIPE_PAGES deletions,
buttons go dead after SNIPE_TTL s. Only messages their author took back (a
staff removal or a purge is never listed — that is the mod-log's business),
bots out unless asked, same window. Text and attachment NAMES only; files
stay in the media cache.
"""
import asyncio
import os
import sys

import discord
from discord import app_commands
from discord.ext import commands

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from utils import mentions as mention_index  # noqa: E402
from utils.security_config import is_enabled  # noqa: E402

COLOR = 0x5865F2
SNIPE_PAGES = 13     # how far back /snipe lets a channel browse
SNIPE_TTL = 180      # seconds the ◀ ▶ buttons stay live
STATE_ICON = {"live": "🔗", "deleted": "🗑️", "removed": "🛡️", "edited_out": "✏️"}
LOOKBACK_DAYS = mention_index.DEFAULT_LOOKBACK_DAYS


def jump_url(guild_id, channel_id, message_id):
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"


def render_line(item, guild_id):
    """One archive row (already classified) -> the embed line for it.
    item keys: state, text, kind, author_id, channel_id, message_id,
    created_ts, deleted_ts, delete_kind."""
    state, kind = item["state"], item["kind"]
    when = f"<t:{int(item['created_ts'])}:R>"
    who = f"<@{item['author_id']}>"
    where = f"<#{item['channel_id']}>"
    how = {"reply": " ↩️ replied to you", "role": " 📣 role ping"}.get(kind, "")
    head = f"{STATE_ICON[state]} {when} · {who} in {where}{how}"
    if state == "live":
        head += f" · [jump]({jump_url(guild_id, item['channel_id'], item['message_id'])})"
        return f"{head}\n> {mention_index.snippet(item['text'])}"
    if state == "deleted":
        gone = f"<t:{int(item['deleted_ts'])}:R>" if item.get("deleted_ts") else ""
        return f"{head}\n> {mention_index.snippet(item['text'])}\n> *deleted {gone}*"
    if state == "removed":
        why = "purged" if item.get("delete_kind") == "bulk" else "removed by staff"
        return f"{head}\n> *{why}*"
    # edited_out
    head += f" · [jump]({jump_url(guild_id, item['channel_id'], item['message_id'])})"
    return f"{head}\n> {mention_index.snippet(item['text'])}\n> *ping edited out*"


def render_snipe_line(item):
    """One self-deleted archive row -> the embed line for /snipe."""
    gone = f"<t:{int(item['deleted_ts'])}:R>" if item.get("deleted_ts") else ""
    posted = f"<t:{int(item['created_ts'])}:R>" if item.get("created_ts") else ""
    head = f"🗑️ {gone} · <@{item['author_id']}>" + (f" (posted {posted})" if posted else "")
    body = mention_index.snippet(item.get("content"))
    tail = mention_index.extras(item)
    if body == "*no text*" and tail:
        body = " · ".join(tail)
    elif tail:
        body += "\n> " + " · ".join(tail)
    return f"{head}\n> {body}"


def snipe_page(items, idx, channel_name, window):
    """Embed for page `idx` (0-based) of a /snipe browse."""
    n = len(items)
    e = discord.Embed(title=f"🎯 Deleted in #{channel_name} · {idx + 1}/{n}",
                      description=render_snipe_line(items[idx])[:4000], color=COLOR)
    foot = f"{window} · only messages their author deleted"
    if n > 1:
        foot += " · ◀ ▶ to browse"
    e.set_footer(text=foot)
    return e


class SnipeView(discord.ui.View):
    """◀ ▶ over the fetched deletions. Anyone in the channel may page — the
    card is public and the whole point is perusing. Dies after SNIPE_TTL."""

    def __init__(self, items, channel_name, window):
        super().__init__(timeout=SNIPE_TTL)
        self.items, self.channel_name, self.window = items, channel_name, window
        self.idx = 0
        self.message = None
        self._sync()

    def _sync(self):
        self.prev.disabled = self.idx <= 0
        self.next.disabled = self.idx >= len(self.items) - 1

    async def _show(self, interaction):
        self._sync()
        await interaction.response.edit_message(
            embed=snipe_page(self.items, self.idx, self.channel_name, self.window), view=self)

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction: discord.Interaction, _button: discord.ui.Button):
        self.idx = max(0, self.idx - 1)
        await self._show(interaction)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, _button: discord.ui.Button):
        self.idx = min(len(self.items) - 1, self.idx + 1)
        await self._show(interaction)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class Mentions(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def _modlog(self):
        return self.bot.get_cog("ModLog")

    def _query(self, guild, member, limit, include_replies, include_roles, include_bots):
        """Blocking: read the index, drop what the member can't see, classify."""
        ml = self._modlog()
        ml._flush()  # rows from the last few seconds are still in memory
        from cogs.mod_log import retention_tier  # loaded already; avoids an import cycle at boot
        tier = retention_tier(guild.id)
        days = LOOKBACK_DAYS if tier != "recent" else 1
        since = mention_index.lookback_ts(days)
        role_ids = [r.id for r in member.roles if not r.is_default()]
        out = []
        with ml._conn() as c:
            rows = mention_index.fetch_mentions(
                c, guild.id, member.id, role_ids, since, limit,
                include_bots=include_bots, include_replies=include_replies,
                include_roles=include_roles)
            for r in rows:
                ch = guild.get_channel_or_thread(int(r["channel_id"]))
                if ch is None or not ch.permissions_for(member).view_channel:
                    continue  # gone, or not theirs to see
                state, text = mention_index.classify(r, member.id, c)
                r["state"], r["text"] = state, text
                out.append(r)
                if len(out) >= limit:
                    break
        return out, days

    @app_commands.command(name="mentions",
                          description="Your latest pings with jump links — including the ones that were deleted.")
    @app_commands.describe(
        limit="How many to show (default 10, max 20)",
        replies="Include replies to your messages (default yes)",
        roles="Include pings of roles you hold (default no — busy servers ping roles a lot)",
        bots="Include pings from bots (default no)")
    @app_commands.guild_only()
    @app_commands.checks.cooldown(1, 10, key=lambda i: (i.guild_id, i.user.id))
    async def mentions(self, interaction: discord.Interaction,
                       limit: app_commands.Range[int, 1, 20] = 10,
                       replies: bool = True, roles: bool = False, bots: bool = False):
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild, member = interaction.guild, interaction.user
        if self._modlog() is None or not is_enabled(guild.id, "msglog"):
            await interaction.followup.send(
                "This server doesn't keep a message log, so there's nothing to look up. "
                "A server manager can turn it on with `/msglog enable`.", ephemeral=True)
            return
        items, days = await asyncio.to_thread(
            self._query, guild, member, limit, replies, roles, bots)
        window = f"last {days} days" if days > 1 else "last 24 hours"
        if not items:
            await interaction.followup.send(
                f"No pings for you in the {window}"
                + ("" if replies else " (replies excluded)") + ".", ephemeral=True)
            return
        lines = [render_line(it, guild.id) for it in items]
        gone = sum(1 for it in items if it["state"] != "live")
        embed = discord.Embed(
            title=f"📣 Your latest mentions — {len(items)}",
            description="\n".join(lines)[:4000], color=COLOR)
        foot = f"{window} · 🗑️ = deleted after pinging you"
        if gone:
            foot = f"{gone} no longer there · " + foot
        if not roles:
            foot += " · roles:True to include role pings"
        embed.set_footer(text=foot)
        await interaction.followup.send(embed=embed, ephemeral=True,
                                        allowed_mentions=discord.AllowedMentions.none())

    def _query_deleted(self, guild, channel_id, limit, author_id, include_bots):
        """Blocking: flush, pick the window, read the channel's self-deletes."""
        ml = self._modlog()
        ml._flush()
        from cogs.mod_log import retention_tier
        tier = retention_tier(guild.id)
        days = LOOKBACK_DAYS if tier != "recent" else 1
        since = mention_index.lookback_ts(days)
        with ml._conn() as c:
            rows = mention_index.fetch_deleted(
                c, guild.id, channel_id, since, limit,
                author_id=author_id, include_bots=include_bots)
        return rows, days

    @app_commands.command(name="snipe",
                          description="Browse what was recently deleted in this channel.")
    @app_commands.describe(
        user="Only this member's deleted messages",
        bots="Include messages bots deleted (default no)")
    @app_commands.guild_only()
    @app_commands.checks.cooldown(1, 10, key=lambda i: (i.guild_id, i.user.id))
    async def snipe(self, interaction: discord.Interaction,
                    user: discord.Member | None = None, bots: bool = False):
        guild, channel = interaction.guild, interaction.channel
        # Refusals are ephemeral and go out BEFORE the public defer — a public
        # "thinking…" that turns into a private answer leaves a stuck placeholder.
        if self._modlog() is None or not is_enabled(guild.id, "msglog"):
            await interaction.response.send_message(
                "This server doesn't keep a message log, so there's nothing to snipe. "
                "A server manager can turn it on with `/msglog enable`.", ephemeral=True)
            return
        perms = channel.permissions_for(interaction.user)
        if not (perms.view_channel and perms.read_message_history):
            await interaction.response.send_message(
                "You can't read this channel's history.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)  # public: the card posts in the channel
        items, days = await asyncio.to_thread(
            self._query_deleted, guild, interaction.channel_id, SNIPE_PAGES,
            user.id if user else None, bots)
        window = f"last {days} days" if days > 1 else "last 24 hours"
        if not items:
            who = f" by {user.mention}" if user else ""
            await interaction.followup.send(
                f"Nothing deleted{who} in this channel in the {window}"
                + (" (bot messages excluded)" if not bots else "") + ".",
                allowed_mentions=discord.AllowedMentions.none())
            return
        name = getattr(channel, "name", None) or "this channel"
        view = SnipeView(items, name, window)
        view.message = await interaction.followup.send(
            embed=snipe_page(items, 0, name, window), view=view, wait=True,
            allowed_mentions=discord.AllowedMentions.none())

    @mentions.error
    @snipe.error
    async def _err(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CommandOnCooldown):
            try:
                await interaction.response.send_message(
                    f"Slow down. Try again in {error.retry_after:.0f}s.", ephemeral=True)
            except discord.errors.NotFound:
                pass


async def setup(bot):
    await bot.add_cog(Mentions(bot))
