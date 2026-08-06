import os
import sys

import discord
from discord import app_commands
from discord.ext import commands

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import quarantine_store as qstore
from utils import gate_terms
from utils import quarantine as qt
from utils.security_config import get_config, set_config
from utils.links import config_view
from cogs.moderation import _can_act

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

    # ---------------------------------------------------------- protection opt-in
    # Per-guild enable/config for the multi-guild security suite (anti-nuke +
    # quarantine-lock). A temporary command surface until the web dashboard ships.
    @security.command(name="status", description="Show this server's protection settings.")
    @app_commands.checks.has_permissions(administrator=True)
    async def status(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("❌ Must be used in a server.", ephemeral=True)
            return
        cfg = get_config(interaction.guild.id)
        g = interaction.guild

        def _role(rid):
            r = g.get_role(int(rid)) if rid else None
            return r.mention if r else "*not set*"

        def _chan(cid):
            c = g.get_channel(int(cid)) if cid else None
            return c.mention if c else "*not set*"

        an = "🔴 enforce" if cfg.get("antinuke_enforce") else "🟡 shadow"
        embed = discord.Embed(title="🛡️ Protection settings", color=0x5B8CFF)
        embed.add_field(name="Anti-Nuke",
                        value=(f"✅ on · {an}" if cfg.get("antinuke_enabled") else "⚪ off"), inline=True)
        embed.add_field(name="Quarantine-Lock",
                        value=("✅ on" if cfg.get("qlock_enabled") else "⚪ off"), inline=True)
        embed.add_field(name="​", value="​", inline=True)
        embed.add_field(name="Quarantine role", value=_role(cfg.get("quarantine_role_id")), inline=True)
        embed.add_field(name="Mod-log channel", value=_chan(cfg.get("modlog_channel_id")), inline=True)
        # A quarantine with no visible verify channel is a silent dead end: the
        # member is held and has nowhere to be told why or how to get out.
        vc = cfg.get("verify_channel_id")
        # The greeting can be turned down per guild, so say which mode is on —
        # "channel is set" and "they get told about it" aren't the same thing.
        ping = {"never": " · no greeting ping",
                "dm_failed": " · greeting only if their DM fails"}.get(
                    str(cfg.get("verify_ping") or "always"), "")
        embed.add_field(
            name="Verify channel",
            value=(_chan(vc) + ping if vc else "⚠️ *none — held members see nothing*"), inline=True)
        embed.add_field(name="Whitelist", value=f"{len(cfg.get('whitelist') or [])} id(s)", inline=True)
        embed.set_footer(text="/security setup to enable · /security enforce to act")
        await interaction.response.send_message(
            embed=embed, view=config_view(interaction.guild.id), ephemeral=True)

    async def _provision_quarantine_role(self, guild, role=None):
        """Create (if needed) and correctly place the quarantine role.

        Returns (role, created, notes). Raises discord.Forbidden if we can't
        create it at all.

        Placement is NOT about permission math — the role carries none, and a
        channel deny works from any height. It decides WHO CAN UNDO a
        quarantine: only members whose top role sits above it can take it off.
        Left at the default bottom position, any mod with Manage Roles can free
        someone the gate caught. So it goes directly beneath the bot's own top
        role: as high as we can manage, above every ordinary staff role.

        It is also HOISTED, so held members show as their own group in the member
        list. A quarantine nobody can see is one that gets forgotten about — the
        point is that staff notice at a glance who is being held. Note the
        interaction with placement: hoisted groups are ordered by role position,
        so sitting just under the bot puts that group at the TOP of the sidebar.
        """
        notes, created = [], False
        if role is None:
            role = await guild.create_role(
                name="Quarantined", permissions=discord.Permissions.none(),
                colour=discord.Colour(0x4F545C), hoist=True, mentionable=False,
                reason="AltGuard: quarantine role for security suite")
            created = True
        elif not role.hoist:
            # Auto-configuring means fixing a role that already exists, including
            # one the dashboard created without hoisting it.
            try:
                await role.edit(hoist=True, reason="AltGuard: show held members in the member list")
                notes.append(f"👁️ Hoisted {role.mention} so quarantined members show as their "
                             "own group in the member list.")
            except discord.HTTPException:
                notes.append(f"⚠️ Couldn't hoist {role.mention} — set *Display role members "
                             "separately* by hand if you want held members visible.")

        me = guild.me
        if me.top_role <= role and not me.guild_permissions.administrator:
            notes.append(f"⚠️ My top role sits **below** {role.mention} — move **{me.top_role.name}** "
                         "above it or I can't apply or remove the quarantine.")
            return role, created, notes

        # position 0 is @everyone; never try to occupy it
        target = max(1, me.top_role.position - 1)
        if role.position != target:
            try:
                await role.edit(position=target,
                                reason="AltGuard: place quarantine role above staff roles")
                above = [r.name for r in guild.roles
                         if 0 < r.position < role.position and not r.managed
                         and r.permissions.manage_roles]
                if above:
                    notes.append(f"↕️ Moved {role.mention} above {len(above)} role(s) that hold "
                                 f"Manage Roles, so they can't undo a quarantine.")
            except discord.Forbidden:
                notes.append(f"⚠️ Couldn't reposition {role.mention} (need **Manage Roles**). It still "
                             "works, but any staff role above it can remove it from a held member.")
            except discord.HTTPException:
                notes.append(f"⚠️ Discord refused to move {role.mention}; position it manually just "
                             f"below **{me.top_role.name}**.")
        return role, created, notes

    @security.command(name="setup", description="Enable anti-nuke + quarantine-lock for this server.")
    @app_commands.describe(modlog="Channel for security alerts",
                           verify_channel="The ONE channel a quarantined member can still see",
                           quarantine_role="Existing lockout role (leave blank to auto-create one)")
    @app_commands.checks.has_permissions(administrator=True)
    async def setup_cmd(self, interaction: discord.Interaction, modlog: discord.TextChannel,
                        verify_channel: discord.TextChannel,
                        quarantine_role: discord.Role = None):
        if not interaction.guild:
            await interaction.response.send_message("❌ Must be used in a server.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild

        try:
            role, created, notes = await self._provision_quarantine_role(guild, quarantine_role)
        except discord.Forbidden:
            await interaction.followup.send(
                "❌ I need **Manage Roles** to create a quarantine role — grant it, or pass an "
                "existing role with `quarantine_role:`.", ephemeral=True)
            return

        # Recorded BEFORE the sweep: the sweep reads verify_channel_id to decide
        # which channel stays visible, so setting it after would lock the one
        # channel a held member needs.
        set_config(guild.id, antinuke_enabled=1, qlock_enabled=1,
                   quarantine_role_id=role.id, modlog_channel_id=str(modlog.id),
                   verify_channel_id=verify_channel.id)

        swept = ""
        qlock = self.bot.get_cog("QuarantineLock")
        if qlock is not None:
            fixed, total = await qlock.sweep(guild)
            swept = (f"\n🔒 Locked the role out of **{fixed}/{total}** channels — "
                     f"{verify_channel.mention} left visible (read-only).")

        await interaction.followup.send(
            f"✅ **Protection enabled** for **{guild.name}**.\n"
            f"• Quarantine role: {role.mention}{' *(created)*' if created else ''}\n"
            f"• Verify channel: {verify_channel.mention}\n"
            f"• Mod-log: {modlog.mention}\n"
            f"• Anti-nuke is in **🟡 shadow mode** (alerts only) — watch {modlog.mention} for a bit, then "
            f"run `/security enforce on:True` to let it act.{swept}"
            + ("\n\n" + "\n".join(notes) if notes else ""),
            ephemeral=True)

    @security.command(name="verify-channel",
                      description="Change which channel quarantined members can still see.")
    @app_commands.describe(channel="The one channel that stays visible (read-only) while quarantined")
    @app_commands.checks.has_permissions(administrator=True)
    async def verify_channel_cmd(self, interaction: discord.Interaction,
                                 channel: discord.TextChannel):
        if not interaction.guild:
            await interaction.response.send_message("❌ Must be used in a server.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        cfg = get_config(interaction.guild.id)
        if not cfg.get("quarantine_role_id"):
            await interaction.followup.send(
                "⚠️ No quarantine role set up yet — run `/security setup` first.", ephemeral=True)
            return
        old = cfg.get("verify_channel_id")
        set_config(interaction.guild.id, verify_channel_id=channel.id)
        # Re-sweep so the previous verify channel gets locked back down in the
        # same breath — otherwise it stays permanently open to held members.
        swept = ""
        qlock = self.bot.get_cog("QuarantineLock")
        if qlock is not None:
            fixed, total = await qlock.sweep(interaction.guild)
            swept = f" ({fixed}/{total} channels corrected)"
        await interaction.followup.send(
            f"✅ Quarantined members can now see {channel.mention} and nothing else{swept}."
            + (f"\n🔒 <#{old}> was re-locked." if old and int(old) != channel.id else ""),
            ephemeral=True)

    @security.command(name="enforce", description="Toggle whether anti-nuke actually acts (vs alert-only).")
    @app_commands.describe(on="True = act (strip/timeout/ban) · False = shadow (alert only)")
    @app_commands.checks.has_permissions(administrator=True)
    async def enforce_cmd(self, interaction: discord.Interaction, on: bool):
        if not interaction.guild:
            await interaction.response.send_message("❌ Must be used in a server.", ephemeral=True)
            return
        cfg = get_config(interaction.guild.id)
        if not cfg.get("antinuke_enabled"):
            await interaction.response.send_message(
                "⚠️ Anti-nuke isn't enabled here yet — run `/security setup` first.", ephemeral=True)
            return
        set_config(interaction.guild.id, antinuke_enforce=1 if on else 0)
        msg = ("🔴 **Enforce ON** — anti-nuke will now strip/timeout/ban on a trip."
               if on else "🟡 **Shadow ON** — anti-nuke will only alert, not act.")
        await interaction.response.send_message(msg, ephemeral=True)

    @security.command(name="disable", description="Turn off anti-nuke + quarantine-lock for this server.")
    @app_commands.checks.has_permissions(administrator=True)
    async def disable_cmd(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("❌ Must be used in a server.", ephemeral=True)
            return
        set_config(interaction.guild.id, antinuke_enabled=0, qlock_enabled=0, antinuke_enforce=0)
        await interaction.response.send_message(
            "⚪ Protection **disabled** for this server. The quarantine role and channel locks are left "
            "in place (harmless) — delete the role manually if you want them gone.", ephemeral=True)

    # ──────────────────────────── gate terms of service ──────────────────────
    @security.command(name="terms",
                      description="Read the verification-gate terms and see if this server accepted them.")
    async def terms_cmd(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("❌ Must be used in a server.", ephemeral=True)
            return
        st = gate_terms.status(interaction.guild.id)
        if st["current"]:
            when = f"<t:{int(st['at'] or 0)}:D>" if st["at"] else "—"
            state = (f"🟢 **Accepted** by {st['username'] or st['uid']} {when} "
                     f"(v{st['version']}). `/security revoke-terms` withdraws it.")
        elif st["accepted"]:
            state = (f"🟡 **Out of date** — this server accepted **v{st['version']}**, and the "
                     f"terms are now **v{gate_terms.TERMS_VERSION}**. The owner needs to accept "
                     f"again before the gate runs here.")
        else:
            state = ("⚪ **Not accepted** — the gate will not screen anyone here. The **server "
                     "owner** can accept with `/security accept-terms confirm:True`.")
        await interaction.response.send_message(
            f"{gate_terms.TERMS_TEXT}\n\n{state}", ephemeral=True)

    @security.command(name="accept-terms",
                      description="Server owner: accept the verification-gate terms for this server.")
    @app_commands.describe(confirm="Yes — I've read /security terms and I accept for this server")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def accept_terms_cmd(self, interaction: discord.Interaction, confirm: bool = False):
        # Owner-only on purpose, and for a stronger reason than the archive
        # terms: this consents to collecting device and network data from
        # *members*, who are not party to the agreement and never clicked
        # anything. That signature belongs to whoever owns the community.
        if not interaction.guild:
            await interaction.response.send_message("❌ Must be used in a server.", ephemeral=True)
            return
        if interaction.user.id != interaction.guild.owner_id:
            await interaction.response.send_message(
                "🔒 Only the **server owner** can accept these terms. They cover what is "
                "collected from your members, not how your server is moderated — so it isn't a "
                "Manage Server decision. Anyone can read them with `/security terms`.",
                ephemeral=True)
            return
        if not confirm:
            await interaction.response.send_message(
                f"{gate_terms.TERMS_TEXT}\n\nRe-run with `confirm:True` to accept on behalf of "
                f"**{interaction.guild.name}**.", ephemeral=True)
            return
        gate_terms.accept(interaction.guild.id, interaction.user.id, str(interaction.user))
        await interaction.response.send_message(
            f"✅ **Terms accepted** (v{gate_terms.TERMS_VERSION}) for **{interaction.guild.name}**.\n"
            f"AltGuard can now be switched on here — it still won't screen anyone until you "
            f"enable it and the operator activates the gate for this server.\n"
            f"-# Tell your members what the check collects. `/security revoke-terms` withdraws "
            f"consent and turns the gate off.", ephemeral=True)

    @security.command(name="revoke-terms",
                      description="Server owner: withdraw consent and stop the gate screening this server.")
    @app_commands.describe(confirm="Yes — withdraw consent and switch AltGuard off here")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def revoke_terms_cmd(self, interaction: discord.Interaction, confirm: bool = False):
        if not interaction.guild:
            await interaction.response.send_message("❌ Must be used in a server.", ephemeral=True)
            return
        if interaction.user.id != interaction.guild.owner_id:
            await interaction.response.send_message(
                "🔒 Only the **server owner** can withdraw these terms.", ephemeral=True)
            return
        if not confirm:
            await interaction.response.send_message(
                "This withdraws consent **and switches AltGuard off** for this server — new "
                "members stop being screened immediately. Device records already collected are "
                "retained (they're shared evidence other servers rely on); ask the bot operator "
                "if you need yours removed.\n\nRe-run with `confirm:True`.", ephemeral=True)
            return
        gate_terms.revoke(interaction.guild.id)
        await interaction.response.send_message(
            "↩️ **Consent withdrawn.** AltGuard is off for this server and nobody new is being "
            "screened. Members currently held keep their quarantine until you release them — "
            "`/unquarantine` gives their roles back.", ephemeral=True)

    # ───────────────────────────── manual quarantine ─────────────────────────
    # The gate decides who to hold on the way IN. This is the door staff can
    # close on someone already inside — an argument that's turning into a raid,
    # a compromised account posting links, a member you want contained while you
    # read the logs. It is deliberately independent of verification status: a
    # fully verified member can be quarantined, and a held member's verification
    # is not touched by lifting it.
    def _q_log(self, guild, cfg):
        cid = cfg.get("modlog_channel_id")
        return guild.get_channel(int(cid)) if cid else None

    @app_commands.command(
        name="quarantine",
        description="Hold a member — strip their roles and lock them out, whatever their verification status.")
    @app_commands.describe(member="Who to hold",
                           reason="Why — goes in the log and in their DM",
                           notify="DM them what happened and how to get out (default: yes)")
    @app_commands.default_permissions(manage_roles=True)
    @app_commands.checks.has_permissions(manage_roles=True)
    async def quarantine_cmd(self, interaction: discord.Interaction, member: discord.Member,
                             reason: str = "", notify: bool = True):
        if not interaction.guild:
            await interaction.response.send_message("❌ Must be used in a server.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild, cfg = interaction.guild, get_config(interaction.guild.id)

        err = _can_act(interaction.user, member, guild.me, "quarantine")
        if err:
            await interaction.followup.send(f"❌ {err}", ephemeral=True)
            return
        if member.bot:
            await interaction.followup.send(
                "❌ That's a bot — its role is managed by Discord and can't be stripped. "
                "Kick the app instead.", ephemeral=True)
            return

        why = reason.strip() or f"manual quarantine by {interaction.user}"
        # Warn BEFORE acting: a role we can't strip is one they keep, and if it
        # carries Administrator the "quarantine" would be theatre.
        stuck = qt.blocked_roles(member, qt.role_for(guild))
        ok, removed, error = await qt.apply(member, why)
        if not ok:
            await interaction.followup.send(f"❌ Couldn't quarantine {member.mention} — {error}",
                                            ephemeral=True)
            return

        vc = cfg.get("verify_channel_id")
        dmed = False
        if notify:
            where = f"\n\nYou can still see <#{vc}> — talk to the staff there." if vc else ""
            try:
                await member.send(
                    f"🔒 Your access to **{guild.name}** has been put on hold.\n"
                    f"**Reason:** {why}\n"
                    f"Your roles have been saved and are given back in full when the hold "
                    f"is lifted — nothing is lost.{where}")
                dmed = True
            except (discord.Forbidden, discord.HTTPException):
                pass

        warn = ""
        if stuck:
            warn = ("\n⚠️ Couldn't strip " + ", ".join(r.mention for r in stuck) +
                    " — they sit at or above my top role and carry real permissions. "
                    "**They still hold them.** Move my role higher.")
        await interaction.followup.send(
            f"🔒 Quarantined {member.mention} — stripped **{len(removed)}** role(s), saved for restore.\n"
            f"• Reason: {why}\n"
            f"• {'📨 DMed them' if dmed else ('📪 DMs closed' if notify else '🔕 Not notified')}\n"
            f"-# `/unquarantine` gives every role back exactly as it was."
            + warn, ephemeral=True)

        ch = self._q_log(guild, cfg)
        if ch:
            e = discord.Embed(
                title="🔒 Member quarantined (manual)", color=0xE0A23B,
                description=f"{member.mention} (`{member.id}`) was held by {interaction.user.mention}.")
            e.add_field(name="Reason", value=why[:1024], inline=False)
            e.add_field(name="Roles stripped",
                        value=(", ".join(r.mention for r in removed)[:1024] or "none"), inline=False)
            if stuck:
                e.add_field(name="⚠️ Kept (above my role)",
                            value=", ".join(r.mention for r in stuck)[:1024], inline=False)
            e.set_footer(text="Reverse with /unquarantine — roles are restored exactly.")
            try:
                await ch.send(embed=e, allowed_mentions=discord.AllowedMentions.none())
            except (discord.Forbidden, discord.HTTPException):
                pass

    @app_commands.command(name="unquarantine",
                          description="Lift a hold and give back every role that was stripped.")
    @app_commands.describe(member="Who to release", reason="Why — goes in the log")
    @app_commands.default_permissions(manage_roles=True)
    @app_commands.checks.has_permissions(manage_roles=True)
    async def unquarantine_cmd(self, interaction: discord.Interaction, member: discord.Member,
                               reason: str = ""):
        if not interaction.guild:
            await interaction.response.send_message("❌ Must be used in a server.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild, cfg = interaction.guild, get_config(interaction.guild.id)

        if not (qt.is_held(member) or qstore.is_quarantined(member.id)):
            await interaction.followup.send(
                f"{member.mention} isn't being held here — nothing to lift.", ephemeral=True)
            return

        why = reason.strip() or f"lifted by {interaction.user}"
        # Read the verification state BEFORE lifting: a member the GATE is
        # holding will simply be re-held on their next join or sync, and telling
        # someone their release stuck when it won't is the worst outcome here.
        v = qstore.verification(member.id) or {}
        gate_held = v.get("status") in ("pending", "quarantined")

        ok, restored, error = await qt.lift(member, why)
        if not ok:
            await interaction.followup.send(f"❌ Couldn't lift the hold — {error}", ephemeral=True)
            return

        note = ""
        if gate_held:
            note = ("\n\n⚠️ AltGuard is still holding an **unfinished verification** for them. "
                    "This lift gives their roles back, but the gate can hold them again. "
                    "Use `/altguard-release` instead if you're vouching for them.")
        try:
            await member.send(
                f"🔓 The hold on your access to **{guild.name}** has been lifted — "
                f"your roles are back.")
        except (discord.Forbidden, discord.HTTPException):
            pass

        await interaction.followup.send(
            f"🔓 Lifted the hold on {member.mention}. Restored: "
            f"{', '.join(r.mention for r in restored) if restored else 'no stored roles'}." + note,
            ephemeral=True)

        ch = self._q_log(guild, cfg)
        if ch:
            e = discord.Embed(
                title="🔓 Quarantine lifted", color=0x3BA55D,
                description=f"{member.mention} (`{member.id}`) was released by {interaction.user.mention}.")
            e.add_field(name="Reason", value=why[:1024], inline=False)
            e.add_field(name="Roles restored",
                        value=(", ".join(r.mention for r in restored)[:1024] or "no stored roles"),
                        inline=False)
            try:
                await ch.send(embed=e, allowed_mentions=discord.AllowedMentions.none())
            except (discord.Forbidden, discord.HTTPException):
                pass

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            need = ", ".join(p.replace("_", " ").title() for p in error.missing_permissions)
            msg = f"❌ You need **{need}** to use that."
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Security(bot))
