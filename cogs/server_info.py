"""/server-info — the server at a glance.

Shaped after the two references Paul uses (Carl-bot's `stats serverinfo` and
Phoenix's `serverinfo`), taking what each does best: Carl's feature checklist,
boost tier and locked-channel counts, Phoenix's compact stat rows and its
icon/banner buttons.

The reason this exists at all is the member line. `guild.member_count` counts
apps as people, so every headline number the server quotes itself is inflated
by however many bots are installed. Humans and bots are counted separately and
shown separately.

Everything is read from the gateway cache — no API calls, no database — so it
is instant and cannot fail on a slow lookup.
"""
import os
import sys
from datetime import timezone

import discord
from discord import app_commands
from discord.ext import commands

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

EMBED_COLOR = 0x3987E5

# Raw guild feature flags -> what a human calls them. Only the ones worth
# showing; the rest are internal or uninteresting and stay hidden.
FEATURES = {
    "COMMUNITY": "Community",
    "DISCOVERABLE": "Server Discovery",
    "PARTNERED": "Partnered",
    "VERIFIED": "Verified",
    "INVITE_SPLASH": "Invite Splash",
    "VANITY_URL": "Vanity Invite",
    "NEWS": "Announcement Channels",
    "ANIMATED_ICON": "Animated Icon",
    "ANIMATED_BANNER": "Animated Banner",
    "BANNER": "Banner",
    "ROLE_ICONS": "Role Icons",
    "SOUNDBOARD": "Soundboard",
    "AUTO_MODERATION": "AutoMod",
    "MEMBER_VERIFICATION_GATE_ENABLED": "Rules Screening",
    "WELCOME_SCREEN_ENABLED": "Welcome Screen",
    "GUILD_ONBOARDING_EVER_ENABLED": "Onboarding",
    "MONETIZATION_ENABLED": "Monetization",
    "ROLE_SUBSCRIPTIONS_ENABLED": "Role Subscriptions",
    "PRIVATE_THREADS": "Private Threads",
    "TICKETED_EVENTS_ENABLED": "Ticketed Events",
}

VERIFICATION = {
    discord.VerificationLevel.none: "None",
    discord.VerificationLevel.low: "Low — verified email",
    discord.VerificationLevel.medium: "Medium — registered 5+ min",
    discord.VerificationLevel.high: "High — member 10+ min",
    discord.VerificationLevel.highest: "Highest — verified phone",
}
CONTENT_FILTER = {
    discord.ContentFilter.disabled: "Off",
    discord.ContentFilter.no_role: "Members without roles",
    discord.ContentFilter.all_members: "All members",
}


def count_members(members):
    """(humans, bots). Separated because `member_count` counts apps as people
    and quietly inflates every headline figure the server quotes about itself."""
    bots = sum(1 for m in members if m.bot)
    return len(members) - bots, bots


def is_locked(channel, everyone):
    """Locked = @everyone can't get in. For voice that means Connect, for
    everything else View Channel — matching how a member experiences it."""
    ow = channel.overwrites_for(everyone)
    if isinstance(channel, discord.VoiceChannel):
        return ow.connect is False or ow.view_channel is False
    return ow.view_channel is False


def channel_counts(guild):
    everyone = guild.default_role
    out = {}
    for key, kinds in (("text", (discord.TextChannel, discord.ForumChannel)),
                       ("voice", (discord.VoiceChannel, discord.StageChannel))):
        chans = [c for c in guild.channels if isinstance(c, kinds)]
        out[key] = (len(chans), sum(1 for c in chans if is_locked(c, everyone)))
    out["categories"] = (len(guild.categories), 0)
    return out


class LinkButtons(discord.ui.View):
    """Icon / banner / splash as link buttons — Phoenix's touch, and better
    than dumping three URLs into the embed body. Link buttons need no
    callback, so nothing here has to survive a restart."""

    def __init__(self, guild):
        super().__init__(timeout=None)
        for label, asset, emoji in (("View Icon", guild.icon, "🖼️"),
                                    ("View Banner", guild.banner, "🏳️"),
                                    ("View Splash", guild.splash, "✨")):
            if asset:
                self.add_item(discord.ui.Button(label=label, url=asset.url, emoji=emoji))


class ServerInfo(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="server-info",
                          description="Server overview — members (humans vs bots), channels, roles, features.")
    @app_commands.guild_only()
    async def server_info(self, interaction: discord.Interaction):
        g = interaction.guild
        await interaction.response.defer()

        humans, bots = count_members(g.members)
        ch = channel_counts(g)
        created = int(g.created_at.replace(tzinfo=timezone.utc).timestamp())
        statics = [e for e in g.emojis if not e.animated]
        animated = [e for e in g.emojis if e.animated]

        e = discord.Embed(title=g.name, color=EMBED_COLOR, description=g.description or None)
        if g.icon:
            e.set_thumbnail(url=g.icon.url)
        if g.banner:
            e.set_image(url=g.banner.url)

        e.add_field(name="Owner", value=g.owner.mention if g.owner else f"`{g.owner_id}`", inline=True)
        e.add_field(name="Created", value=f"<t:{created}:D>\n<t:{created}:R>", inline=True)
        e.add_field(name="Vanity", value=f"discord.gg/{g.vanity_url_code}" if g.vanity_url_code else "—", inline=True)

        # the headline: apps are not people
        e.add_field(name=f"Members — {humans + bots:,}",
                    value=f"👤 **{humans:,}** humans\n🤖 **{bots:,}** bots", inline=True)
        e.add_field(name=f"Channels — {ch['text'][0] + ch['voice'][0]:,}",
                    value=(f"# {ch['text'][0]} text ({ch['text'][1]} locked)\n"
                           f"🔊 {ch['voice'][0]} voice ({ch['voice'][1]} locked)\n"
                           f"📁 {ch['categories'][0]} categories"), inline=True)
        e.add_field(name="Roles & Emojis",
                    value=(f"🎭 {len(g.roles) - 1:,} roles\n"
                           f"😀 {len(statics):,} static · {len(animated):,} animated\n"
                           f"🏷️ {len(g.stickers):,} stickers"), inline=True)

        boost = f"Level {g.premium_tier}" + (" (maxed)" if g.premium_tier == 3 else "")
        e.add_field(name="Boosts", value=f"{boost}\n💎 {g.premium_subscription_count or 0} boosts", inline=True)
        e.add_field(name="Moderation",
                    value=(f"Verification: {VERIFICATION.get(g.verification_level, '?')}\n"
                           f"Content filter: {CONTENT_FILTER.get(g.explicit_content_filter, '?')}\n"
                           f"2FA for mods: {'Yes' if g.mfa_level else 'No'}"), inline=True)
        # the ternary must bind to max_members alone — written inline it
        # swallowed the whole field whenever max_members was None
        max_members = f"{g.max_members:,}" if g.max_members else "—"
        e.add_field(name="Limits",
                    value=(f"Upload: {g.filesize_limit // 1048576} MB\n"
                           f"Bitrate: {g.bitrate_limit // 1000:.0f} kbps\n"
                           f"Max members: {max_members}"), inline=True)

        have = [FEATURES[f] for f in g.features if f in FEATURES]
        if have:
            e.add_field(name=f"Features — {len(have)}",
                        value=" · ".join(f"✅ {n}" for n in sorted(have)), inline=False)

        e.set_footer(text=f"ID {g.id}")
        await interaction.followup.send(embed=e, view=LinkButtons(g))


async def setup(bot):
    await bot.add_cog(ServerInfo(bot))
