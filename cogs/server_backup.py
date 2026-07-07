"""Server backup / nuke-recovery for peepos-reclaimer — Phase 1: member roster.

Periodically records every human member (uid, name, roles, joined) into a local
SQLite roster, so that after a nuke (mass ban/kick) you know exactly who was
here. `/roster-missing` lists members on record who AREN'T in the server now —
your re-invite candidates.

Snapshots: on startup, every BACKUP_SNAPSHOT_HOURS (default 6), and on demand
via /roster-snapshot. Guild-scoped to ALTGUARD_GUILD_ID. Members-only (skips bots).

Next phases (not built yet): channel/role structure backup, auto-unban+reinvite
on a detected mass-ban.
"""
import io
import os
import json
import time
import sqlite3
import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

log = logging.getLogger("backup")


def _env_int(name, default=0):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


GUILD_ID = _env_int("ALTGUARD_GUILD_ID")
SNAPSHOT_HOURS = _env_int("BACKUP_SNAPSHOT_HOURS", 6)
DB_PATH = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server_backup.db"))


class ServerBackup(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        with self._conn() as c:
            c.execute(
                """CREATE TABLE IF NOT EXISTS roster (
                       uid          TEXT PRIMARY KEY,
                       username     TEXT,
                       display_name TEXT,
                       roles        TEXT,   -- json list of role ids held
                       joined_at    REAL,
                       first_seen   REAL,   -- first time WE recorded them
                       last_seen    REAL    -- most recent snapshot they were present in
                   )"""
            )
            c.execute("CREATE TABLE IF NOT EXISTS snapshots (ts REAL, member_count INTEGER)")
            c.execute("CREATE TABLE IF NOT EXISTS structure (ts REAL PRIMARY KEY, guild_name TEXT, roles TEXT, channels TEXT)")
            # the transaction log: every join/leave/kick/ban between snapshots
            c.execute("""CREATE TABLE IF NOT EXISTS member_events (
                             ts REAL, uid TEXT, username TEXT, display_name TEXT,
                             roles TEXT, kind TEXT, by_uid TEXT)""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_ev_ts ON member_events(ts)")

    def _conn(self):
        c = sqlite3.connect(DB_PATH, timeout=30)
        c.row_factory = sqlite3.Row
        return c

    async def cog_load(self):
        self.auto_snapshot.start()

    async def cog_unload(self):
        self.auto_snapshot.cancel()

    # ----------------------------------------------------------- snapshot
    async def snapshot(self, guild):
        if not guild.chunked:
            try:
                await guild.chunk()  # ensure the full member roster is cached
            except discord.HTTPException:
                pass
        now = time.time()
        n = 0
        with self._conn() as c:
            for m in guild.members:
                if m.bot:
                    continue
                roles = json.dumps([r.id for r in m.roles if not r.is_default()])
                joined = m.joined_at.timestamp() if m.joined_at else None
                c.execute(
                    """INSERT INTO roster(uid, username, display_name, roles, joined_at, first_seen, last_seen)
                       VALUES (?,?,?,?,?,?,?)
                       ON CONFLICT(uid) DO UPDATE SET
                           username=excluded.username, display_name=excluded.display_name,
                           roles=excluded.roles, joined_at=COALESCE(excluded.joined_at, roster.joined_at),
                           last_seen=excluded.last_seen""",
                    (str(m.id), m.name, m.display_name, roles, joined, now, now),
                )
                n += 1
            c.execute("INSERT INTO snapshots(ts, member_count) VALUES (?,?)", (now, n))
        log.info("roster snapshot: %d members recorded", n)
        return n

    def capture_structure(self, guild):
        """Snapshot roles + channels (names, positions, perms, overwrites) so a
        nuke's deletions can be recreated. Keeps the last 10 snapshots."""
        roles = [{"id": str(r.id), "name": r.name, "position": r.position,
                  "color": r.colour.value, "permissions": r.permissions.value,
                  "hoist": r.hoist, "mentionable": r.mentionable,
                  "managed": r.managed, "is_default": r.is_default()} for r in guild.roles]
        channels = []
        for ch in guild.channels:
            ov = []
            for target, perm in ch.overwrites.items():
                allow, deny = perm.pair()
                ov.append({"id": str(target.id),
                           "type": "role" if isinstance(target, discord.Role) else "member",
                           "allow": allow.value, "deny": deny.value})
            channels.append({"id": str(ch.id), "name": ch.name, "type": ch.type.name,
                             "position": ch.position,
                             "parent_id": str(ch.category_id) if ch.category_id else None,
                             "topic": getattr(ch, "topic", None),
                             "nsfw": bool(getattr(ch, "nsfw", False)),
                             "slowmode": getattr(ch, "slowmode_delay", 0) or 0,
                             "overwrites": ov})
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO structure(ts, guild_name, roles, channels) VALUES (?,?,?,?)",
                      (time.time(), guild.name, json.dumps(roles), json.dumps(channels)))
            c.execute("DELETE FROM structure WHERE ts NOT IN (SELECT ts FROM structure ORDER BY ts DESC LIMIT 10)")
        log.info("structure snapshot: %d roles, %d channels", len(roles), len(channels))
        return len(roles), len(channels)

    def _overwrites_from(self, guild, ov_list, role_map):
        """Rebuild a {target: PermissionOverwrite} dict, remapping old role ids to
        the (possibly just-recreated) current roles."""
        result = {}
        for o in ov_list:
            if o["type"] == "role":
                target = role_map.get(o["id"]) or guild.get_role(int(o["id"]))
            else:
                target = guild.get_member(int(o["id"]))
            if target is None:
                continue
            result[target] = discord.PermissionOverwrite.from_pair(
                discord.Permissions(int(o["allow"])), discord.Permissions(int(o["deny"])))
        return result

    def _log_event(self, member, kind, by_uid=None):
        roles = json.dumps([r.id for r in getattr(member, "roles", []) if not r.is_default()])
        with self._conn() as c:
            c.execute("INSERT INTO member_events(ts, uid, username, display_name, roles, kind, by_uid) "
                      "VALUES (?,?,?,?,?,?,?)",
                      (time.time(), str(member.id), member.name, member.display_name, roles, kind, by_uid))

    @commands.Cog.listener()
    async def on_member_join(self, member):
        """Real-time join log — completes the transaction log so a join-then-leave
        within a snapshot gap is still fully captured."""
        if member.guild.id != GUILD_ID or member.bot:
            return
        self._log_event(member, "join")

    @commands.Cog.listener()
    async def on_member_remove(self, member):
        """Real-time departure log — captures every leave the instant it happens,
        with the member's roles and whether it was a voluntary leave / kick / ban."""
        if member.guild.id != GUILD_ID or member.bot:
            return
        kind, by_uid = "leave", None
        try:
            async for e in member.guild.audit_logs(limit=4, action=discord.AuditLogAction.ban):
                if e.target and e.target.id == member.id and (discord.utils.utcnow() - e.created_at).total_seconds() < 10:
                    kind, by_uid = "ban", str(e.user.id)
                    break
            if kind == "leave":
                async for e in member.guild.audit_logs(limit=4, action=discord.AuditLogAction.kick):
                    if e.target and e.target.id == member.id and (discord.utils.utcnow() - e.created_at).total_seconds() < 10:
                        kind, by_uid = "kick", str(e.user.id)
                        break
        except discord.Forbidden:
            pass
        self._log_event(member, kind, by_uid)

    @app_commands.command(name="recent-leaves",
                          description="Recent departures — leaves, kicks, bans, with who did them (admin)")
    @app_commands.describe(hours="how far back to look (default 24)")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def recent_leaves(self, interaction: discord.Interaction, hours: int = 24):
        await interaction.response.defer(ephemeral=True, thinking=True)
        cutoff = time.time() - max(1, hours) * 3600
        with self._conn() as c:
            rows = c.execute("SELECT uid, username, kind, by_uid, ts FROM member_events "
                             "WHERE ts>? AND kind!='join' ORDER BY ts DESC", (cutoff,)).fetchall()
        if not rows:
            await interaction.followup.send(f"No departures in the last {hours}h.", ephemeral=True)
            return
        counts = {}
        for r in rows:
            counts[r["kind"]] = counts.get(r["kind"], 0) + 1
        summary = ", ".join(f"{v} {k}{'s' if v > 1 else ''}" for k, v in counts.items())
        lines = []
        for r in rows[:25]:
            t = time.strftime("%m-%d %H:%M", time.localtime(r["ts"]))
            by = f" by <@{r['by_uid']}>" if r["by_uid"] else ""
            lines.append(f"`{t}` · **{r['kind'].upper()}** · {r['username']} (`{r['uid']}`){by}")
        body = "\n".join(lines)
        if len(rows) > 25:
            body += f"\n… +{len(rows) - 25} more"
        await interaction.followup.send(f"📤 **{len(rows)}** departures in {hours}h — {summary}:\n{body}", ephemeral=True)

    @app_commands.command(name="member-activity",
                          description="Full join/leave/kick/ban log between snapshots (admin)")
    @app_commands.describe(hours="how far back to look (default 24)")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def member_activity(self, interaction: discord.Interaction, hours: int = 24):
        await interaction.response.defer(ephemeral=True, thinking=True)
        cutoff = time.time() - max(1, hours) * 3600
        with self._conn() as c:
            rows = c.execute("SELECT ts, username, kind, by_uid FROM member_events WHERE ts>? ORDER BY ts DESC",
                             (cutoff,)).fetchall()
        if not rows:
            await interaction.followup.send(f"No member activity in the last {hours}h.", ephemeral=True)
            return
        counts = {}
        for r in rows:
            counts[r["kind"]] = counts.get(r["kind"], 0) + 1
        summary = ", ".join(f"{v} {k}{'s' if v > 1 else ''}" for k, v in counts.items())
        emoji = {"join": "📥", "leave": "📤", "kick": "👢", "ban": "🔨"}
        lines = []
        for r in rows[:25]:
            t = time.strftime("%m-%d %H:%M", time.localtime(r["ts"]))
            by = f" by <@{r['by_uid']}>" if r["by_uid"] else ""
            lines.append(f"`{t}` {emoji.get(r['kind'], '•')} **{r['kind']}** · {r['username']}{by}")
        body = "\n".join(lines)
        if len(rows) > 25:
            body += f"\n… +{len(rows) - 25} more"
        await interaction.followup.send(f"📊 **{len(rows)}** events in {hours}h — {summary}:\n{body}", ephemeral=True)

    @tasks.loop(hours=SNAPSHOT_HOURS)
    async def auto_snapshot(self):
        guild = self.bot.get_guild(GUILD_ID)
        if guild:
            try:
                await self.snapshot(guild)
                self.capture_structure(guild)
            except Exception as e:
                log.warning("auto snapshot failed: %s", e)

    @auto_snapshot.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    # ----------------------------------------------------------- commands
    @app_commands.command(name="roster-snapshot", description="Record the member roster right now (admin)")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def roster_snapshot(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        n = await self.snapshot(interaction.guild)
        with self._conn() as c:
            total = c.execute("SELECT COUNT(*) FROM roster").fetchone()[0]
        await interaction.followup.send(
            f"📸 Snapshot saved — **{n}** members present now; **{total}** total on record (all-time).",
            ephemeral=True)

    @app_commands.command(
        name="roster-missing",
        description="Members on record who AREN'T in the server now — your re-invite list (admin)",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def roster_missing(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        if not guild.chunked:
            try:
                await guild.chunk()
            except discord.HTTPException:
                pass
        present = {str(m.id) for m in guild.members}
        with self._conn() as c:
            rows = c.execute(
                "SELECT uid, username, last_seen FROM roster ORDER BY last_seen DESC").fetchall()
        if not rows:
            await interaction.followup.send(
                "No roster on record yet — run `/roster-snapshot` first (auto-snapshots run every "
                f"{SNAPSHOT_HOURS}h).", ephemeral=True)
            return
        missing = [r for r in rows if r["uid"] not in present]
        if not missing:
            await interaction.followup.send(
                "✅ Everyone on record is still in the server — nobody missing.", ephemeral=True)
            return
        lines = [
            f"{r['uid']}\t{r['username']}\t(last present {time.strftime('%Y-%m-%d', time.localtime(r['last_seen']))})"
            for r in missing
        ]
        f = discord.File(io.BytesIO("\n".join(lines).encode()), filename="reinvite-candidates.txt")
        await interaction.followup.send(
            f"📋 **{len(missing)}** members are on record but not in the server right now — your re-invite "
            f"candidates (full list attached).\n-# Includes people who left normally too; after a nuke, the "
            f"ones with a recent *last present* date are your victims.",
            file=f, ephemeral=True)


    @app_commands.command(name="structure-status",
                          description="Show the channel/role backup + what's been deleted since (admin)")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def structure_status(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        with self._conn() as c:
            row = c.execute("SELECT ts, roles, channels FROM structure ORDER BY ts DESC LIMIT 1").fetchone()
        if not row:
            await interaction.followup.send(
                f"No structure backup yet — auto-backups run on startup + every {SNAPSHOT_HOURS}h.", ephemeral=True)
            return
        roles_data = json.loads(row["roles"]); chans_data = json.loads(row["channels"])
        cur_roles = {r.id for r in guild.roles}; cur_chans = {c.id for c in guild.channels}
        gone_r = [r for r in roles_data if int(r["id"]) not in cur_roles and not r["managed"] and not r["is_default"]]
        gone_c = [c for c in chans_data if int(c["id"]) not in cur_chans]
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(row["ts"]))
        msg = f"📦 Latest structure backup: **{when}** — {len(roles_data)} roles, {len(chans_data)} channels.\n"
        if gone_r or gone_c:
            msg += (f"⚠️ **Deleted since the backup:**\n"
                    f"• roles ({len(gone_r)}): {', '.join(r['name'] for r in gone_r[:20]) or '—'}\n"
                    f"• channels ({len(gone_c)}): {', '.join(c['name'] for c in gone_c[:20]) or '—'}\n"
                    f"-# Run `/structure-restore` to recreate them.")
        else:
            msg += "✅ Nothing deleted since the backup — structure intact."
        await interaction.followup.send(msg, ephemeral=True)

    @app_commands.command(name="structure-restore",
                          description="Recreate roles/channels deleted since the last backup (admin)")
    @app_commands.describe(confirm="True = actually recreate; leave blank for a dry-run preview")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def structure_restore(self, interaction: discord.Interaction, confirm: bool = False):
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        with self._conn() as c:
            row = c.execute("SELECT ts, roles, channels FROM structure ORDER BY ts DESC LIMIT 1").fetchone()
        if not row:
            await interaction.followup.send("No structure backup to restore from.", ephemeral=True)
            return
        roles_data = json.loads(row["roles"]); chans_data = json.loads(row["channels"])
        cur_role_names = {r.name for r in guild.roles}
        missing_roles = [r for r in roles_data if not r["managed"] and not r["is_default"]
                         and guild.get_role(int(r["id"])) is None and r["name"] not in cur_role_names]
        cur_chan_keys = {(c.name, c.type.name) for c in guild.channels}
        missing_chans = [c for c in chans_data if guild.get_channel(int(c["id"])) is None
                         and (c["name"], c["type"]) not in cur_chan_keys]
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(row["ts"]))

        if not confirm:
            await interaction.followup.send(
                f"🔎 **Dry run** vs backup from {when}:\n"
                f"• **{len(missing_roles)}** roles would be recreated: {', '.join(r['name'] for r in missing_roles[:15]) or '—'}\n"
                f"• **{len(missing_chans)}** channels would be recreated: {', '.join(c['name'] for c in missing_chans[:15]) or '—'}\n"
                f"-# Run `/structure-restore confirm:True` to do it. Only MISSING items are created — nothing existing is touched.",
                ephemeral=True)
            return

        # build old-role-id -> current Role map (existing + about to be recreated)
        role_map = {}
        for rd in roles_data:
            existing = guild.get_role(int(rd["id"])) or discord.utils.get(guild.roles, name=rd["name"])
            if existing:
                role_map[rd["id"]] = existing
        created_r = 0
        for rd in sorted(missing_roles, key=lambda x: x["position"]):
            try:
                nr = await guild.create_role(
                    name=rd["name"], permissions=discord.Permissions(int(rd["permissions"])),
                    colour=discord.Colour(int(rd["color"])), hoist=rd["hoist"],
                    mentionable=rd["mentionable"], reason="AltGuard structure restore")
                role_map[rd["id"]] = nr; created_r += 1
            except discord.HTTPException:
                pass

        chan_map = {}; created_c = 0
        for cd in sorted([c for c in missing_chans if c["type"] == "category"], key=lambda x: x["position"]):
            try:
                nc = await guild.create_category(
                    name=cd["name"], overwrites=self._overwrites_from(guild, cd["overwrites"], role_map),
                    reason="AltGuard structure restore")
                chan_map[cd["id"]] = nc; created_c += 1
            except discord.HTTPException:
                pass
        for cd in sorted([c for c in missing_chans if c["type"] != "category"], key=lambda x: x["position"]):
            parent = (guild.get_channel(int(cd["parent_id"])) or chan_map.get(cd["parent_id"])) if cd["parent_id"] else None
            ow = self._overwrites_from(guild, cd["overwrites"], role_map)
            try:
                if cd["type"] in ("text", "news"):
                    await guild.create_text_channel(cd["name"], category=parent, overwrites=ow,
                                                    topic=cd.get("topic"), nsfw=cd.get("nsfw", False),
                                                    slowmode_delay=min(cd.get("slowmode", 0) or 0, 21600),
                                                    reason="AltGuard structure restore")
                elif cd["type"] == "voice":
                    await guild.create_voice_channel(cd["name"], category=parent, overwrites=ow,
                                                     reason="AltGuard structure restore")
                elif cd["type"] == "stage_voice":
                    await guild.create_stage_channel(cd["name"], category=parent, overwrites=ow,
                                                     reason="AltGuard structure restore")
                else:
                    continue
                created_c += 1
            except discord.HTTPException:
                pass

        await interaction.followup.send(
            f"✅ Restore complete — recreated **{created_r}** roles and **{created_c}** channels from the {when} backup.\n"
            f"-# Channel order/positions may need a manual tidy. Member-specific overwrites for users who left were skipped.",
            ephemeral=True)

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            msg = "❌ You need the **Administrator** permission to use this."
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)


async def setup(bot):
    await bot.add_cog(ServerBackup(bot))
