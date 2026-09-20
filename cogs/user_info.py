"""/userinfo — one member at a glance.

Shaped after Phoenix's `userinfo`, which is the card Paul pointed at: name and
id, avatar, created/joined with relative ages, the role list, boosting since,
and buttons for avatar / banner / permissions. Sibling of `cogs/server_info.py`
and deliberately built the same way.

Two rules this file exists to respect:

* **It shows only what Discord itself shows.** No warnings, no AltGuard verdict,
  no alt score. Conduct history is moderator-gated on purpose (`/warnings`) and a
  public card that leaked it would undo that in one click.
* **A mention costs nothing but a role list costs 1024 characters.** Discord
  rejects the whole embed when one field runs over, and the 400 surfaces to the
  member as "the application did not respond" — the trap that ate the ban-race
  kit embed on 9/15. `role_field` measures as it builds instead of trusting a
  count.

The permission button is answered by an `on_interaction` listener keyed on its
custom_id prefix, the same pattern as conduct's evidence buttons, so a card
posted before a restart still opens instead of dying with "interaction failed".
"""
import os
import sys
from datetime import timezone

import discord
from discord import app_commands
from discord.ext import commands

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

EMBED_COLOR = 0x3987E5          # matches /server-info
BOT_COLOR = 0x5865F2
MAX_FIELD = 1024                # Discord's per-field cap — the whole point of role_field
PERMS_PREFIX = "ui:perms:"

# Badge flags worth showing. `discord.PublicUserFlags` carries a few more
# (team_user, system, bot_http_interactions) that describe plumbing rather than
# a person, so they stay hidden.
BADGES = [
    ("staff", "Discord Staff"),
    ("partner", "Partner"),
    ("discord_certified_moderator", "Certified Moderator"),
    ("hypesquad", "HypeSquad Events"),
    ("hypesquad_bravery", "HypeSquad Bravery"),
    ("hypesquad_brilliance", "HypeSquad Brilliance"),
    ("hypesquad_balance", "HypeSquad Balance"),
    ("bug_hunter", "Bug Hunter"),
    ("bug_hunter_level_2", "Bug Hunter (gold)"),
    ("early_supporter", "Early Supporter"),
    ("verified_bot_developer", "Early Verified Bot Developer"),
    ("active_developer", "Active Developer"),
]

# Shown on the permissions panel, in the order a moderator cares about them.
# Anything not listed is real but unremarkable (send messages, add reactions).
KEY_PERMS = [
    ("administrator", "Administrator"),
    ("manage_guild", "Manage Server"),
    ("manage_roles", "Manage Roles"),
    ("manage_channels", "Manage Channels"),
    ("manage_webhooks", "Manage Webhooks"),
    ("manage_events", "Manage Events"),
    ("manage_messages", "Manage Messages"),
    ("manage_threads", "Manage Threads"),
    ("manage_nicknames", "Manage Nicknames"),
    ("moderate_members", "Timeout Members"),
    ("kick_members", "Kick Members"),
    ("ban_members", "Ban Members"),
    ("view_audit_log", "View Audit Log"),
    ("mention_everyone", "Mention @everyone"),
    ("mute_members", "Mute Members"),
    ("deafen_members", "Deafen Members"),
    ("move_members", "Move Members"),
    ("priority_speaker", "Priority Speaker"),
]

ORDINALS = {1: "st", 2: "nd", 3: "rd"}


def ordinal(n):
    """11th/12th/13th are the exceptions every naive version gets wrong."""
    if 11 <= (n % 100) <= 13:
        return f"{n:,}th"
    return f"{n:,}{ORDINALS.get(n % 10, 'th')}"


def stamp(dt, style="D"):
    """`<t:...>` so every reader sees it in their own timezone — the house rule
    for anything a player reads."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return f"<t:{int(dt.timestamp())}:{style}>"


def when(dt):
    """Absolute date + relative age on two lines, Phoenix's layout."""
    return f"{stamp(dt, 'D')}\n{stamp(dt, 'R')}" if dt else "—"


def badge_list(user):
    flags = getattr(user, "public_flags", None)
    if flags is None:
        return []
    return [label for attr, label in BADGES if getattr(flags, attr, False)]


def role_field(member, cap=MAX_FIELD):
    """`(text, shown, total)` — role mentions highest-first, truncated to fit.

    Measured, not counted: 26 short roles fit where 12 long ones do not, and
    guessing is how a card 400s. The "+N more" tail is charged for BEFORE the
    loop accepts a mention, because appending it afterwards is exactly what
    pushes a field that just fit back over the edge.
    """
    roles = [r for r in reversed(member.roles) if not r.is_default()]
    if not roles:
        return ("—", 0, 0)
    total = len(roles)
    out, used = [], 0
    for i, role in enumerate(roles):
        mention = role.mention
        remaining = total - i - 1
        tail = f" +{remaining} more" if remaining else ""
        need = len(mention) + (1 if out else 0) + len(tail)
        if used + need > cap:
            break
        used += len(mention) + (1 if out else 0)
        out.append(mention)
    if not out:                          # one role longer than the whole cap
        return (f"{total} roles", 0, total)
    if len(out) < total:
        return (" ".join(out) + f" +{total - len(out)} more", len(out), total)
    return (" ".join(out), len(out), total)


def join_rank(guild, member):
    """"Nth to join", or None when the member cache is incomplete — a rank from
    a partial cache is confidently wrong, which is worse than absent."""
    if member.joined_at is None:
        return None
    if guild.member_count and len(guild.members) < guild.member_count:
        return None
    dated = [m for m in guild.members if m.joined_at is not None]
    dated.sort(key=lambda m: m.joined_at)
    try:
        return dated.index(member) + 1
    except ValueError:
        return None


def perms_custom_id(guild_id, user_id):
    return f"{PERMS_PREFIX}{guild_id}:{user_id}"


def parse_perms_custom_id(raw):
    """`(guild_id, user_id)` or None. Never raises on a custom_id from another
    cog — every component interaction in the guild reaches the listener."""
    if not isinstance(raw, str) or not raw.startswith(PERMS_PREFIX):
        return None
    parts = raw[len(PERMS_PREFIX):].split(":")
    if len(parts) != 2:
        return None
    try:
        return (int(parts[0]), int(parts[1]))
    except ValueError:
        return None


def granted(perms):
    """Key permissions this member actually holds. Administrator is returned
    alone — listing the other eighteen underneath it implies they are separate
    grants that could be revoked one by one, and they cannot."""
    if perms.administrator:
        return ["Administrator"]
    return [label for attr, label in KEY_PERMS if getattr(perms, attr, False)]


def build_card(user, member, fetched=None, rank=None):
    """The embed. Pure — `user` is always present, `member` is None for someone
    who is not in this server (the card then has no Joined or Roles), and
    `fetched` is the API-fetched user that carries the banner.
    """
    name = member.display_name if member else (getattr(user, "global_name", None) or user.name)
    colour = BOT_COLOR if user.bot else EMBED_COLOR
    if member and member.colour.value:
        colour = member.colour.value      # their displayed role colour, as the member list shows it

    e = discord.Embed(title=name, color=colour)
    line = f"{user.mention} · `@{user.name}`"
    if user.bot:
        line += " · 🤖 **App**"
    e.description = line

    avatar = (member or user).display_avatar
    if avatar:
        e.set_thumbnail(url=avatar.url)
    banner = getattr(fetched, "banner", None)
    if banner:
        e.set_image(url=banner.url)

    e.add_field(name="Created", value=when(user.created_at), inline=True)
    if member:
        joined = when(member.joined_at)
        if rank:
            joined += f"\n{ordinal(rank)} to join"
        e.add_field(name="Joined", value=joined, inline=True)
        if member.premium_since:
            e.add_field(name="Boosting since", value=when(member.premium_since), inline=True)

        text, _shown, total = role_field(member)
        e.add_field(name=f"Roles — {total}", value=text, inline=False)

        # Anything that changes how the server currently treats them.
        state = []
        if member.is_timed_out():
            state.append(f"🔇 Timed out until {stamp(member.timed_out_until, 'f')}")
        if getattr(member, "pending", False):
            state.append("⏳ Hasn't finished the rules screen")
        if member.voice and member.voice.channel:
            state.append(f"🔊 In {member.voice.channel.mention}")
        if state:
            e.add_field(name="Status", value="\n".join(state), inline=False)
    else:
        e.add_field(name="Joined", value="Not in this server", inline=True)

    marks = badge_list(user)
    if marks:
        e.add_field(name="Badges", value=" · ".join(marks), inline=False)

    e.set_footer(text=f"ID {user.id}")
    return e


class UserButtons(discord.ui.View):
    """Avatar and banner are LINK buttons — no callback, nothing to survive.
    Permissions needs a callback, so its custom_id carries the target and the
    cog's on_interaction answers it; `timeout=None` keeps an old card's button
    live rather than greying it out."""

    def __init__(self, user, member, fetched=None):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(
            label="View Avatar", emoji="🖼️", url=(member or user).display_avatar.url))
        # A server-specific avatar is a different image from the global one, and
        # the card shows the server one — offer the global too rather than lie.
        if getattr(member, "guild_avatar", None):
            self.add_item(discord.ui.Button(
                label="Global Avatar", emoji="👤", url=user.display_avatar.url))
        banner = getattr(fetched, "banner", None)
        if banner:
            self.add_item(discord.ui.Button(label="View Banner", emoji="🏞️", url=banner.url))
        if member:
            self.add_item(discord.ui.Button(
                label="View Permissions", emoji="🔐", style=discord.ButtonStyle.secondary,
                custom_id=perms_custom_id(member.guild.id, member.id)))


class UserInfo(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(
        name="userinfo",
        description="Everything public about a member — account age, join date, roles, badges.")
    @app_commands.describe(user="Who to look up. Leave blank for yourself.")
    @app_commands.guild_only()
    async def userinfo(self, interaction: discord.Interaction, user: discord.User = None):
        await interaction.response.defer()
        target = user or interaction.user
        member = interaction.guild.get_member(target.id)

        # The cached User has no banner — only a fetch carries it. One API call,
        # and a failure costs the banner, never the card.
        fetched = None
        try:
            fetched = await self.bot.fetch_user(target.id)
        except discord.HTTPException:
            pass

        rank = join_rank(interaction.guild, member) if member else None
        await interaction.followup.send(embed=build_card(target, member, fetched, rank),
                                        view=UserButtons(target, member, fetched))

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        """Answer View Permissions. Keyed on the custom_id prefix rather than a
        registered View, so a card from before the last restart still works."""
        if interaction.type != discord.InteractionType.component:
            return
        parsed = parse_perms_custom_id((interaction.data or {}).get("custom_id"))
        if parsed is None or interaction.guild is None:
            return
        guild_id, user_id = parsed
        if guild_id != interaction.guild.id:
            return
        member = interaction.guild.get_member(user_id)
        if member is None:
            return await interaction.response.send_message(
                "They've left the server since this card was posted.", ephemeral=True)

        held = granted(member.guild_permissions)
        body = "\n".join(f"✅ {p}" for p in held) if held else "No special permissions."
        e = discord.Embed(title=f"🔐 {member.display_name}", color=EMBED_COLOR, description=body)
        # Server-wide, not per-channel: a channel overwrite can add or remove any
        # of these in one place, and implying otherwise misleads a moderator.
        e.set_footer(text="Server-wide permissions — a channel's own overwrites can differ.")
        try:
            await interaction.response.send_message(embed=e, ephemeral=True)
        except discord.HTTPException:
            pass


async def setup(bot):
    await bot.add_cog(UserInfo(bot))
