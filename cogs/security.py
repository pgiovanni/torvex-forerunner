import discord
from discord import app_commands
from discord.ext import commands

SEV_CRITICAL, SEV_HIGH, SEV_MEDIUM = 0, 1, 2
SEV_EMOJI = {SEV_CRITICAL: "🟥", SEV_HIGH: "🟧", SEV_MEDIUM: "🟨"}
_PAGE_BUDGET = 3900  # embed description hard limit is 4096

# Dangerous server-level permission attr -> human label. Looked up via
# getattr(perms, attr, False) so flags missing on older discord.py never fire.
_HIGH = {
    "manage_guild": "Manage Server",
    "manage_roles": "Manage Roles",
    "manage_channels": "Manage Channels",
    "manage_webhooks": "Manage Webhooks",
    "ban_members": "Ban Members",
    "kick_members": "Kick Members",
}
_MEDIUM = {
    "mention_everyone": "Mention Everyone/Here",
    "manage_messages": "Manage Messages",
    "moderate_members": "Timeout Members",
    "manage_nicknames": "Manage Nicknames",
    "manage_events": "Manage Events",
    "manage_threads": "Manage Threads",
    "view_audit_log": "View Audit Log",
    "manage_expressions": "Manage Expressions",
    "manage_emojis_and_stickers": "Manage Expressions",  # alias on older versions
}

# Channel-overwrite perms (server-only perms are ignored inside overwrites by
# Discord) -> (label, base severity from how dangerous the perm itself is).
_CHANNEL_DANGER = {
    "manage_channels": ("Manage Channel", SEV_HIGH),
    "manage_roles": ("Manage Permissions", SEV_HIGH),
    "manage_webhooks": ("Manage Webhooks", SEV_HIGH),
    "manage_messages": ("Manage Messages", SEV_MEDIUM),
    "mention_everyone": ("Mention Everyone/Here", SEV_MEDIUM),
    "manage_threads": ("Manage Threads", SEV_MEDIUM),
}


def _has_external_apps(perms: discord.Permissions) -> bool:
    """Use External Apps — the permission that lets members run user-installed apps."""
    val = getattr(perms, "use_external_apps", None)
    if val is None:
        return bool(perms.value & (1 << 50))  # older discord.py lacks the named flag
    return bool(val)


def _danger_for_role(role: discord.Role):
    """Return (severity, [labels]) for the dangerous perms a role holds, else None."""
    perms = role.permissions
    if perms.administrator:
        return SEV_CRITICAL, ["Administrator (grants ALL permissions)"]
    labels = {}  # label -> severity, deduped by label
    for attr, label in _HIGH.items():
        if getattr(perms, attr, False):
            labels[label] = SEV_HIGH
    for attr, label in _MEDIUM.items():
        if getattr(perms, attr, False):
            labels.setdefault(label, SEV_MEDIUM)
    if not labels:
        return None
    worst = min(labels.values())
    ordered = sorted(labels, key=lambda l: (labels[l], l))
    return worst, ordered


def _audience(target, guild: discord.Guild):
    """How broadly is an overwrite target held? Returns (severity, descriptor).

    A dangerous grant to a role most members have (e.g. a 'Verified' role) is far
    worse than the same grant to one person, so audience size drives severity.
    """
    if isinstance(target, discord.Role):
        if target.is_default():
            return SEV_CRITICAL, "@everyone — all members"
        total = guild.member_count or len(guild.members) or 1
        held = len(target.members)
        frac = held / total
        if frac >= 0.5:
            return SEV_CRITICAL, f"@{target.name} — ~{round(frac * 100)}% of members"
        if frac >= 0.15 or held >= 10:
            return SEV_HIGH, f"@{target.name} — {held} members"
        return SEV_MEDIUM, f"@{target.name} — {held} members"
    name = getattr(target, "display_name", None) or str(target)
    return SEV_MEDIUM, f"{name} (single member)"


def _paginate(blocks, budget=_PAGE_BUDGET):
    """Pack pre-formatted blocks into pages without splitting a block."""
    pages, cur, length = [], [], 0
    for block in blocks:
        add = len(block) + 1
        if cur and length + add > budget:
            pages.append("\n".join(cur))
            cur, length = [], 0
        cur.append(block)
        length += add
    if cur:
        pages.append("\n".join(cur))
    return pages or [""]


class Security(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    security = app_commands.Group(name="security", description="Server security tools (Admin only)")

    @security.command(name="audit", description="Scan roles AND channel overrides for dangerous permissions.")
    @app_commands.checks.has_permissions(administrator=True)
    async def audit(self, interaction: discord.Interaction):
        guild = interaction.guild
        if not guild:
            await interaction.response.send_message("❌ Must be used in a server.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        no_pings = discord.AllowedMentions.none()  # never notify anyone

        # ---- Roles ----
        role_findings = []  # (severity, role, labels)
        for role in guild.roles:
            result = _danger_for_role(role)
            if result:
                role_findings.append((result[0], role, result[1]))
        role_findings.sort(key=lambda f: (f[0], -f[1].position))

        # ---- Channel permission overwrites (the `allow` side) ----
        channel_findings = []  # (severity, channel, audience_desc, labels)
        for channel in guild.channels:
            for target, overwrite in channel.overwrites.items():
                allow, _deny = overwrite.pair()
                hits = [(label, sev) for attr, (label, sev) in _CHANNEL_DANGER.items()
                        if getattr(allow, attr, False)]
                if not hits:
                    continue
                aud_sev, aud_desc = _audience(target, guild)
                sev = min(aud_sev, min(s for _, s in hits))  # worse of audience vs perm
                labels = [l for l, _ in sorted(hits, key=lambda x: x[1])]
                channel_findings.append((sev, channel, aud_desc, labels))
        channel_findings.sort(key=lambda f: (f[0], f[1].name))

        # Any NON-admin role granting Use External Apps reopens the user-install hole
        # (admin roles always have it implicitly; stripping admin for this isn't the fix).
        ext_roles = [r for r in guild.roles
                     if not r.permissions.administrator and _has_external_apps(r.permissions)]

        if not role_findings and not channel_findings and not ext_roles:
            embed = discord.Embed(
                title="🛡️ Permission Security Audit",
                description="✅ No roles or channel overrides grant dangerous permissions. Looking clean!",
                color=0x2ECC71,
            )
            await interaction.edit_original_response(embed=embed, allowed_mentions=no_pings)
            return

        # ---- Counts across both layers ----
        counts = {SEV_CRITICAL: 0, SEV_HIGH: 0, SEV_MEDIUM: 0}
        for sev, *_ in role_findings:
            counts[sev] += 1
        for sev, *_ in channel_findings:
            counts[sev] += 1
        for r in ext_roles:
            counts[SEV_CRITICAL if r.is_default() else SEV_HIGH] += 1
        worst = SEV_CRITICAL if counts[SEV_CRITICAL] else (SEV_HIGH if counts[SEV_HIGH] else SEV_MEDIUM)
        color = {SEV_CRITICAL: 0xE74C3C, SEV_HIGH: 0xE67E22, SEV_MEDIUM: 0xF1C40F}[worst]

        # ---- Build display blocks ----
        blocks = [
            f"**Totals** — 🟥 {counts[SEV_CRITICAL]} critical · "
            f"🟧 {counts[SEV_HIGH]} high · 🟨 {counts[SEV_MEDIUM]} medium",
            f"__**Role permissions** ({len(role_findings)})__",
        ]
        if role_findings:
            for sev, role, labels in role_findings:
                tags = []
                if role.is_default():
                    tags.append("**@everyone — every member has this**")
                if getattr(role, "managed", False):
                    tags.append("bot/integration role")
                if role.mentionable:
                    tags.append("mentionable")
                who = "all members" if role.is_default() else f"{len(role.members)} member(s)"
                header = f"{SEV_EMOJI[sev]} **{role.name}** — {who}"
                if tags:
                    header += f"  ({', '.join(tags)})"
                blocks.append(f"{header}\n⤷ {', '.join(labels)}")
        else:
            blocks.append("✅ None.")

        blocks.append(f"__**Channel overrides** ({len(channel_findings)})__")
        if channel_findings:
            for sev, channel, aud_desc, labels in channel_findings:
                blocks.append(f"{SEV_EMOJI[sev]} **#{channel.name}** → {aud_desc}\n⤷ {', '.join(labels)}")
        else:
            blocks.append("✅ None.")

        blocks.append("__**External app exposure** (public user-app responses — the raid vector)__")
        if ext_roles:
            for r in sorted(ext_roles, key=lambda x: (not x.is_default(), -len(x.members))):
                who = "every member" if r.is_default() else f"{len(r.members)} members"
                emoji = "🟥" if r.is_default() else "🟧"
                blocks.append(f"{emoji} **{r.name}** ({who}) — user-installed app responses post "
                              "PUBLICLY here, so a `/raid`-style app can flood the channel for everyone.")
            blocks.append("⤷ Removing **Use External Apps** forces those responses to private/ephemeral "
                          "(only the invoker sees them) — the strongest native control. It does NOT fully "
                          "block the app (Discord has no full block); admins always bypass it.\n"
                          "**Server Settings → Roles → [role] → Use External Apps.**")
        else:
            blocks.append("✅ No non-admin role grants Use External Apps — regular members' user-app responses "
                          "are forced private (ephemeral), so a user-install app **can't publicly flood your "
                          "channels**. (The app still runs privately for the user; Discord can't fully block it.)")

        pages = _paginate(blocks)
        total = len(pages)

        embeds = []
        for i, text in enumerate(pages, 1):
            title = "🛡️ Permission Security Audit"
            if total > 1:
                title += f"  (page {i}/{total})"
            embeds.append(discord.Embed(title=title, description=text, color=color))

        # Remediation guidance lands on the final page.
        last = embeds[-1]
        last.add_field(
            name="🔧 Recommended",
            value=(
                "• Remove **Administrator** from any role that isn't owner/trusted staff.\n"
                "• Keep Manage Server/Roles/Channels/Webhooks + Ban/Kick on as few roles as possible.\n"
                "• A dangerous grant to a near-everyone role (e.g. **Verified**) is as bad as giving it to @everyone."
            ),
            inline=False,
        )

        await interaction.edit_original_response(embed=embeds[0], allowed_mentions=no_pings)
        for embed in embeds[1:]:
            await interaction.followup.send(embed=embed, ephemeral=True, allowed_mentions=no_pings)

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message("❌ Administrator permission required.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(Security(bot))
