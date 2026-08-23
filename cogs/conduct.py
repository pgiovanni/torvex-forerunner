"""Conduct record — /warn, /note, /warnings, /evidence and the clear commands.

Permission ladder, chosen to sit alongside the existing moderation cog rather
than invent a new tier:

  /warn /note /warnings /evidence /clear-warning   Timeout Members
  /clear-warnings (bulk wipe of one member)        Manage Server
  /conduct-forget (true erasure)                   Administrator

Timeout Members is the lowest Discord permission that actually means "this
person is a moderator", and a warning is *less* severe than a timeout — gating
it any higher than /timeout would be backwards, and would force an owner to hand
a junior mod kick or ban just so they could write a warning down.

Two exceptions to that ladder, both deliberate:

  * /warnings has no default_permissions, because anyone may read their OWN
    record. Viewing someone else's is checked in code. A record a member can
    never see is a secret file, not a moderation tool.
  * Bulk clear is a tier up. Clearing one entry is routine; wiping a member's
    whole history is the action that can quietly rewrite what happened.

Nothing here truly deletes except /conduct-forget. Clears are stamped with who
and why and stay visible under `show_cleared`.
"""
import os
import sys

import discord
from discord import app_commands
from discord.ext import commands

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from utils.security_config import get_config  # noqa: E402
from utils import conduct as store  # noqa: E402

WARN_COLOR = 0xE67E22    # amber — a warning is not a ban
NOTE_COLOR = 0x2ECC71    # green — positives read as positives at a glance
CLEAR_COLOR = 0x95A5A6
MAX_EVIDENCE = 3         # per entry; Discord caps attachment options anyway
PAGE = 10                # entries shown in one /warnings embed


def _cfg(guild_id) -> dict:
    c = get_config(guild_id)
    return {
        "dm": bool(c.get("conduct_dm_on_warn", 1)),
        "public": bool(c.get("conduct_public_warn", 1)),
        "require_reason": bool(c.get("conduct_require_reason", 1)),
        "evidence": bool(c.get("conduct_evidence", 1)),
        "max_mb": int(c.get("conduct_evidence_max_mb", 25) or 25),
        "max_gb": int(c.get("conduct_evidence_max_gb", 2) or 2),
        # Warnings are moderation records, so they belong with the message log,
        # not the security/AltGuard alert channel (Paul, 8/23). Order: explicit
        # conduct channel -> moderation-actions channel -> message log -> security log.
        "log_channel_id": (c.get("conduct_log_channel_id")
                           or c.get("mod_log_channel_id")
                           or c.get("msglog_channel_id")
                           or c.get("modlog_channel_id")),
    }


async def _log(guild, cfg, embed, files=None):
    """Post to the conduct/mod log if one is configured. Never raises."""
    cid = cfg.get("log_channel_id")
    if not cid:
        return
    ch = guild.get_channel(int(cid))
    if ch is None:
        return
    try:
        await ch.send(embed=embed, files=files or [],
                      allowed_mentions=discord.AllowedMentions.none())
    except (discord.Forbidden, discord.HTTPException):
        pass


def notify_plan(kind, cfg, notify="default"):
    """(dm?, ping?, silent?) for one record.

    `notify` is the per-call choice on /warn — "default" | "both" | "dm" |
    "ping" | "none" — and it overrides the server toggles in BOTH directions: a
    mod can quietly record a repeat offender, or ping someone in a server that
    normally keeps warnings to DMs.

    Pure so the matrix is testable. Two invariants live here: a NOTE never pings
    publicly (positives and quiet observations are not announcements), and
    `silent` is derived from what actually happens rather than passed in, so the
    mod-log footer can never claim "silent" while a DM went out.
    """
    if notify == "default":
        do_dm = bool(cfg.get("dm"))
        do_ping = bool(cfg.get("public"))
    else:
        do_dm = notify in ("both", "dm")
        do_ping = notify in ("both", "ping")
    do_ping = do_ping and kind == "warn"
    return do_dm, do_ping, (not do_dm and not do_ping)


def _ordinal(n) -> str:
    n = int(n)
    suf = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suf}"


async def _dm(user, guild, kind, reason, entry_id, nth=None):
    """Best-effort notice to the member. Closed DMs must never block the record."""
    try:
        if kind == "warn":
            e = discord.Embed(
                title=f"You were warned in {guild.name}",
                description=reason, color=WARN_COLOR)
            e.set_footer(text=(f"Your {nth} standing warning here · " if nth else "")
                         + f"Warning #{entry_id} · reply to a moderator if you think this is wrong")
        else:
            e = discord.Embed(
                title=f"A note was added to your record in {guild.name}",
                description=reason, color=NOTE_COLOR)
            e.set_footer(text=f"Note #{entry_id}")
        await user.send(embed=e)
        return True
    except (discord.Forbidden, discord.HTTPException, AttributeError):
        return False


def _can_record(invoker: discord.Member, target: discord.Member, me: discord.Member):
    """Hierarchy sanity. Returns an error string, or None if it's allowed."""
    guild = invoker.guild
    if target.id == invoker.id:
        return "You can't warn or note yourself."
    if target.id == me.id:
        return "I can't hold a record on myself."
    if target.bot:
        return "Bots don't get conduct records."
    if invoker.id != guild.owner_id and target.top_role >= invoker.top_role:
        return (f"You can't record against {target.mention} — their highest role "
                "is above or equal to yours.")
    return None


def _fmt_entry(e, evidence_count=0, compact=True):
    """One line (list view) or a block (detail view)."""
    icon = "⚠️" if e["kind"] == "warn" else "📗"
    when = f"<t:{int(e['created_at'])}:D>"
    clip = "📎" * min(evidence_count, 3)
    line = f"{icon} **#{e['id']}** · {when} · by <@{e['moderator_id']}> {clip}\n> {e['reason']}"
    if e["cleared_at"]:
        line += (f"\n> ~~cleared~~ <t:{int(e['cleared_at'])}:D> by <@{e['cleared_by']}>"
                 f" — {e['cleared_reason'] or 'no reason given'}")
    return line


def _human_bytes(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0


class Conduct(commands.Cog):
    """Warnings, positive notes, and the evidence behind them."""

    def __init__(self, bot):
        self.bot = bot

    # ── recording ─────────────────────────────────────────────────────────────

    async def _record(self, interaction, member, kind, reason, attachments, notify="default"):
        """notify: 'default' (server settings) | 'both' | 'dm' | 'ping' | 'none'.
        The per-call choice overrides the server toggles in both directions —
        a mod can quietly record a repeat offender's tenth warning, or ping
        someone in a server that normally keeps warnings to DMs."""
        cfg = _cfg(interaction.guild_id)

        if cfg["require_reason"] and not (reason or "").strip():
            return await interaction.response.send_message(
                "A reason is required on this server — a record with no reason "
                "can't be judged later.", ephemeral=True)

        err = _can_record(interaction.user, member, interaction.guild.me)
        if err:
            return await interaction.response.send_message(err, ephemeral=True)

        attachments = [a for a in attachments if a is not None]
        if attachments and not cfg["evidence"]:
            return await interaction.response.send_message(
                "Evidence uploads are turned off on this server.", ephemeral=True)

        # Size checks BEFORE downloading anything.
        per_file_cap = cfg["max_mb"] * 1024 * 1024
        for a in attachments:
            if a.size > per_file_cap:
                return await interaction.response.send_message(
                    f"`{a.filename}` is {_human_bytes(a.size)} — this server's cap is "
                    f"{cfg['max_mb']} MB per file.", ephemeral=True)
        if attachments:
            total_cap = cfg["max_gb"] * 1024 * 1024 * 1024
            used = store.guild_evidence_bytes(interaction.guild_id)
            incoming = sum(a.size for a in attachments)
            if used + incoming > total_cap:
                return await interaction.response.send_message(
                    f"This server's evidence store is full "
                    f"({_human_bytes(used)} of {cfg['max_gb']} GB). Nothing was deleted to "
                    "make room — clear space or raise `conduct_evidence_max_gb`.",
                    ephemeral=True)

        await interaction.response.defer(ephemeral=True)

        entry_id = store.add_entry(
            interaction.guild_id, member.id, kind, reason.strip(),
            interaction.user.id, str(interaction.user))

        saved, failed = [], []
        for a in attachments:
            try:
                saved.append(await store.save_attachment(a, entry_id, interaction.guild_id, member.id))
            except (discord.HTTPException, OSError) as exc:
                failed.append(f"{a.filename} ({exc.__class__.__name__})")

        # Which warning this is FOR THIS MEMBER. Entry ids are a global
        # autoincrement, so "#12" says nothing about the member's history —
        # the ordinal is the number a mod actually reasons with ("third time").
        c = store.counts(interaction.guild_id, member.id)
        nth = _ordinal(c["warns"]) if kind == "warn" else None
        total_ever = c["warns"] + c["cleared"]

        do_dm, do_ping, silent = notify_plan(kind, cfg, notify)

        dmed = False
        if do_dm:
            dmed = await _dm(member, interaction.guild, kind, reason, entry_id, nth)

        # Public notice in the channel the mod used: pings the member and states
        # the reason, so the warning is seen where the behaviour happened and
        # the rest of the room knows it was dealt with (Paul, 8/23). Notes stay
        # private; silent warns skip it.
        public = False
        if do_ping:
            try:
                await interaction.channel.send(
                    f"⚠️ {member.mention} — **warning** ({nth}): {reason}",
                    allowed_mentions=discord.AllowedMentions(users=[member]))
                public = True
            except (discord.Forbidden, discord.HTTPException, AttributeError):
                pass

        # operator confirmation
        word = "Warning" if kind == "warn" else "Note"
        bits = [f"{word} **#{entry_id}** recorded against {member.mention}."]
        if nth:
            bits.append(f"This is their **{nth} standing warning**"
                        + (f" ({total_ever} ever, {c['cleared']} cleared)." if c["cleared"] else "."))
        if do_ping and not public:
            bits.append("⚠️ Couldn't post the public notice here (missing permission).")
        if saved:
            bits.append(f"📎 {len(saved)} file(s) stored.")
        if failed:
            bits.append("⚠️ Failed to store: " + ", ".join(failed))
        if do_dm:
            bits.append("DM delivered." if dmed else "Couldn't DM them (DMs closed).")
        if silent:
            bits.append("Silent — they were not told.")
        await interaction.followup.send(" ".join(bits), ephemeral=True)

        # mod log
        e = discord.Embed(
            title=(f"⚠️ Warning #{entry_id} — {nth} for this member" if kind == "warn"
                   else f"📗 Note #{entry_id}"),
            description=reason, color=WARN_COLOR if kind == "warn" else NOTE_COLOR)
        e.add_field(name="Member", value=f"{member.mention}\n`{member.id}`", inline=True)
        e.add_field(name="Moderator", value=interaction.user.mention, inline=True)
        e.add_field(name="Standing record",
                    value=f"{c['warns']} warning(s) · {c['notes']} note(s)"
                          + (f" · {c['cleared']} cleared" if c["cleared"] else ""), inline=True)
        if interaction.channel is not None:
            e.add_field(name="Where", value=interaction.channel.mention
                        + (" · public notice posted" if public else ""), inline=True)
        if saved:
            e.add_field(
                name=f"Evidence ({len(saved)})",
                value="\n".join(f"`{s['filename']}` · {_human_bytes(s['bytes'])} · "
                                f"`{s['sha256'][:12]}`" for s in saved)[:1024],
                inline=False)
        if silent:
            e.set_footer(text="Silent — the member was not notified")
        await _log(interaction.guild, cfg, e)

    @app_commands.command(name="warn", description="Warn a member and record it, with optional evidence.")
    @app_commands.describe(
        member="Who to warn", reason="What they did — this is the record",
        evidence="Screenshot or file backing this up",
        evidence2="Another file", evidence3="Another file",
        notify="How the member is told — leave empty for this server's default")
    @app_commands.choices(notify=[
        app_commands.Choice(name="DM + ping in this channel", value="both"),
        app_commands.Choice(name="DM only", value="dm"),
        app_commands.Choice(name="Ping in this channel only", value="ping"),
        app_commands.Choice(name="Silent — record only, don't tell them", value="none"),
    ])
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    @app_commands.guild_only()
    async def warn(self, interaction: discord.Interaction, member: discord.Member, reason: str,
                   evidence: discord.Attachment = None, evidence2: discord.Attachment = None,
                   evidence3: discord.Attachment = None,
                   notify: app_commands.Choice[str] = None):
        await self._record(interaction, member, "warn", reason,
                           [evidence, evidence2, evidence3],
                           notify.value if notify else "default")

    @app_commands.command(name="note", description="Record positive or neutral conduct — resolutions, good streaks.")
    @app_commands.describe(
        member="Who this is about", note="What happened",
        evidence="Screenshot or file", silent="Record it without DMing them")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    @app_commands.guild_only()
    async def note(self, interaction: discord.Interaction, member: discord.Member, note: str,
                   evidence: discord.Attachment = None, silent: bool = True):
        await self._record(interaction, member, "note", note, [evidence],
                           "none" if silent else "dm")

    # ── reading ───────────────────────────────────────────────────────────────

    @app_commands.command(name="warnings", description="View a member's conduct record — or your own.")
    @app_commands.describe(member="Whose record (leave empty for your own)",
                           show_cleared="Include entries that were cleared")
    @app_commands.guild_only()
    async def warnings(self, interaction: discord.Interaction,
                       member: discord.Member = None, show_cleared: bool = False):
        target = member or interaction.user
        # Anyone may read their own. Reading someone else's needs the mod perm —
        # enforced here rather than via default_permissions, so the command stays
        # visible to everyone for self-lookup.
        if target.id != interaction.user.id and not interaction.user.guild_permissions.moderate_members:
            return await interaction.response.send_message(
                "You can only view your own record.", ephemeral=True)

        rows = store.list_entries(interaction.guild_id, target.id, include_cleared=show_cleared)
        c = store.counts(interaction.guild_id, target.id)

        e = discord.Embed(
            title=f"Conduct record — {target.display_name}",
            color=WARN_COLOR if c["warns"] else NOTE_COLOR)
        e.set_thumbnail(url=target.display_avatar.url)
        e.description = (f"**{c['warns']}** standing warning(s) · **{c['notes']}** note(s)"
                         + (f" · {c['cleared']} cleared" if c["cleared"] else ""))

        if not rows:
            e.add_field(name="​",
                        value="Nothing on record." if not show_cleared
                        else "Nothing on record, cleared or standing.", inline=False)
        else:
            shown = rows[:PAGE]
            counts_by_entry = {r["id"]: len(store.evidence_for(r["id"])) for r in shown}
            body = "\n\n".join(_fmt_entry(r, counts_by_entry[r["id"]]) for r in shown)
            e.add_field(name=f"Entries ({len(shown)} of {len(rows)})", value=body[:1024], inline=False)
            if len(rows) > PAGE:
                e.set_footer(text=f"{len(rows) - PAGE} older entries not shown · "
                                  "/evidence <id> for one in full")
        await interaction.response.send_message(embed=e, ephemeral=True)

    @app_commands.command(name="evidence", description="Show one record entry in full, with its stored files.")
    @app_commands.describe(entry_id="The #id from /warnings")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    @app_commands.guild_only()
    async def evidence(self, interaction: discord.Interaction, entry_id: int):
        entry = store.get_entry(interaction.guild_id, entry_id)
        if not entry:
            return await interaction.response.send_message(
                f"No entry `#{entry_id}` on this server.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        files_meta = store.evidence_for(entry_id)

        e = discord.Embed(
            title=f"{'⚠️ Warning' if entry['kind'] == 'warn' else '📗 Note'} #{entry_id}",
            description=entry["reason"],
            color=CLEAR_COLOR if entry["cleared_at"] else
            (WARN_COLOR if entry["kind"] == "warn" else NOTE_COLOR))
        e.add_field(name="Member", value=f"<@{entry['user_id']}>\n`{entry['user_id']}`", inline=True)
        e.add_field(name="Moderator", value=f"<@{entry['moderator_id']}>", inline=True)
        e.add_field(name="When", value=f"<t:{int(entry['created_at'])}:F>", inline=True)
        if entry["cleared_at"]:
            e.add_field(name="Cleared",
                        value=f"<t:{int(entry['cleared_at'])}:F> by <@{entry['cleared_by']}>\n"
                              f"{entry['cleared_reason'] or 'no reason given'}", inline=False)

        attach, missing = [], []
        for m in files_meta:
            try:
                attach.append(discord.File(m["path"], filename=m["filename"]))
            except OSError:
                missing.append(m["filename"])
        if files_meta:
            e.add_field(
                name=f"Evidence ({len(files_meta)})",
                value="\n".join(f"`{m['filename']}` · {_human_bytes(m['bytes'])} · "
                                f"sha256 `{(m['sha256'] or '')[:12]}`" for m in files_meta)[:1024],
                inline=False)
        if missing:
            e.add_field(name="⚠️ Missing from disk", value=", ".join(missing)[:1024], inline=False)

        await interaction.followup.send(embed=e, files=attach, ephemeral=True)

    # ── clearing ──────────────────────────────────────────────────────────────

    @app_commands.command(name="clear-warning", description="Clear one entry. It stays on record as cleared.")
    @app_commands.describe(entry_id="The #id from /warnings", reason="Why it's being cleared")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    @app_commands.guild_only()
    async def clear_warning(self, interaction: discord.Interaction, entry_id: int, reason: str):
        entry = store.get_entry(interaction.guild_id, entry_id)
        if not entry:
            return await interaction.response.send_message(
                f"No entry `#{entry_id}` on this server.", ephemeral=True)
        if entry["cleared_at"]:
            return await interaction.response.send_message(
                f"`#{entry_id}` was already cleared <t:{int(entry['cleared_at'])}:R>.", ephemeral=True)

        store.clear_entry(interaction.guild_id, entry_id, interaction.user.id,
                          str(interaction.user), reason)
        await interaction.response.send_message(
            f"Cleared `#{entry_id}`. It stays on record, marked cleared by you.", ephemeral=True)

        cfg = _cfg(interaction.guild_id)
        e = discord.Embed(title=f"🧹 Cleared #{entry_id}", description=reason, color=CLEAR_COLOR)
        e.add_field(name="Member", value=f"<@{entry['user_id']}>", inline=True)
        e.add_field(name="Cleared by", value=interaction.user.mention, inline=True)
        e.add_field(name="Original", value=entry["reason"][:1024], inline=False)
        await _log(interaction.guild, cfg, e)

    @app_commands.command(name="clear-warnings", description="Clear a member's whole standing record (Manage Server).")
    @app_commands.describe(member="Whose record", reason="Why the whole record is being cleared")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def clear_warnings(self, interaction: discord.Interaction,
                             member: discord.Member, reason: str):
        n = store.clear_all(interaction.guild_id, member.id, interaction.user.id,
                            str(interaction.user), reason)
        if not n:
            return await interaction.response.send_message(
                f"{member.mention} has nothing standing to clear.", ephemeral=True)

        await interaction.response.send_message(
            f"Cleared {n} entr{'y' if n == 1 else 'ies'} for {member.mention}. "
            "They stay on record, marked cleared by you.", ephemeral=True)

        cfg = _cfg(interaction.guild_id)
        e = discord.Embed(
            title="🧹 Record cleared in bulk",
            description=reason, color=CLEAR_COLOR)
        e.add_field(name="Member", value=f"{member.mention}\n`{member.id}`", inline=True)
        e.add_field(name="Cleared by", value=interaction.user.mention, inline=True)
        e.add_field(name="Entries", value=str(n), inline=True)
        await _log(interaction.guild, cfg, e)

    # ── erasure ───────────────────────────────────────────────────────────────

    @app_commands.command(
        name="conduct-forget",
        description="Permanently erase a user's record AND evidence files. Cannot be undone.")
    @app_commands.describe(user_id="Discord user ID",
                           everywhere="Erase in every server, not just this one",
                           confirm="Type true to confirm — this really deletes")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def conduct_forget(self, interaction: discord.Interaction, user_id: str,
                             confirm: bool = False, everywhere: bool = False):
        uid = user_id.strip()
        if not uid.isdigit():
            return await interaction.response.send_message(
                "That doesn't look like a user ID.", ephemeral=True)
        if not confirm:
            scope = "EVERY server" if everywhere else "this server"
            return await interaction.response.send_message(
                f"This permanently deletes `{uid}`'s entries **and their evidence files** "
                f"in {scope}. Nothing is recoverable and no cleared-record trail is kept — "
                "this is the erasure path, not the clear path. Re-run with `confirm: True`.",
                ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        res = store.forget_user(uid, None if everywhere else interaction.guild_id)
        await interaction.followup.send(
            f"Erased `{uid}` — {res['entries']} entr(ies), {res['evidence']} evidence row(s), "
            f"{res['files']} file(s) removed from disk"
            + (" across every server." if everywhere else " on this server."), ephemeral=True)

    # ── errors ────────────────────────────────────────────────────────────────

    async def cog_app_command_error(self, interaction: discord.Interaction,
                                    error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            need = ", ".join(error.missing_permissions)
            msg = f"You need **{need}** to use that."
        else:
            msg = f"That didn't work: `{error.__class__.__name__}`"
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass


async def setup(bot):
    await bot.add_cog(Conduct(bot))
